import ipaddress
import math
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator, field_validator
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import (Pop, ProbeProfile, ProbeProfileVersion, ProbeTarget, ProbeBatch, ProbeSeries,
    ProbeRound, ProbeRollup, ProbeIncident, ProbeIncidentEvent, ProbeMaintenance, ProbeTrace, MonitoringDestination, Device, InventoryLink, CaseEvidence, now, uid)
from ..security import audit, rate_limit
from .config import TransportConfig, measurement_config, ProbeConfig, DEFAULT_PROFILE_ID, DEFAULT_VERSION_ID, host, safe_ip, seed_defaults
from .resolver import resolve, ProbeError
from .service import lock, effective, capacity, changed, next_slot, origin_view, target_views, snapshot
from .catalog import CATEGORIES, CATEGORY_IDS
from .organization import ensure_destination, overview as monitoring_overview
from .detector import window_metrics, record_configuration
from . import trace


class TargetBody(BaseModel):
    destination_id: str | None = None
    essential: bool = True
    name: str = Field(min_length=1,max_length=120)
    address: str = Field(min_length=1,max_length=253)
    family: str = Field(default='ipv4',pattern='^(ipv4|ipv6)$')
    protocol: str = Field(default='icmp',pattern='^(icmp|tcp|https)$')
    check_config: TransportConfig = Field(default_factory=TransportConfig)
    dns_policy: str = Field(default='approved_ips',pattern='^(approved_ips|public_dns)$')
    pop_id: str | None = None
    description: str = Field(default='',max_length=500)
    categories: list[str] = Field(default_factory=list,max_length=16)
    service: str = Field(default='',max_length=120)
    profile_version_id: str = DEFAULT_VERSION_ID
    overrides: dict = Field(default_factory=dict)
    approved_ips: list[str] = Field(default_factory=list,max_length=16)
    enabled: bool = False
    version: int | None = None

    @field_validator('categories')
    @classmethod
    def valid_categories(cls,value):
        if set(value)-CATEGORY_IDS:raise ValueError('Categoría no válida')
        return list(dict.fromkeys(value))

    @field_validator('service')
    @classmethod
    def clean_service(cls,value):
        return value.strip()

    @model_validator(mode='after')
    def valid(self):
        self.name=self.name.strip()
        if not self.name:raise ValueError('Nombre vacío')
        self.address=host(self.address)
        try:
            literal=ipaddress.ip_address(self.address)
        except ValueError:literal=None
        if literal:
            self.approved_ips=[safe_ip(self.address,self.family)]
        else:
            self.approved_ips=sorted({safe_ip(value,self.family) for value in self.approved_ips})
            if not self.approved_ips and self.dns_policy!='public_dns':raise ValueError('Resuelve y autoriza las IPs del nombre DNS')
        if self.dns_policy=='public_dns' and any(not ipaddress.ip_address(ip).is_global for ip in self.approved_ips):
            raise ValueError('DNS público solo permite IPs públicas')
        return self


class DestinationBody(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    scope: str = Field(pattern='^(network|external)$')
    device_id: str | None = None
    link_id: str | None = None
    version: int


class ResolveBody(BaseModel):
    address: str = Field(min_length=1,max_length=253)
    family: str = Field(default='ipv4',pattern='^(ipv4|ipv6)$')


class ProfileBody(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    config: ProbeConfig
    version: int | None = None
    apply_to_targets: bool = False


class VersionBody(BaseModel):
    version: int


class MaintenanceBody(BaseModel):
    starts_at: int
    ends_at: int
    reason: str = Field(min_length=1,max_length=300)

    @model_validator(mode='after')
    def valid(self):
        if self.ends_at<=self.starts_at or self.ends_at-self.starts_at>7*86400 or self.starts_at>now()+30*86400 or self.ends_at<=now():
            raise ValueError('Ventana futura, máximo 7 días, programada hasta 30 días')
        if not self.reason.strip():raise ValueError('Indica motivo de mantenimiento')
        return self


class NoteBody(BaseModel):
    text: str = Field(min_length=1,max_length=2000)


def target_or_404(db,key):
    row=db.get(ProbeTarget,key)
    if not row:raise HTTPException(404,'Destino no encontrado')
    return row


def incident_view(row,names):
    return {**{key:getattr(row,key) for key in ('id','target_id','address','pop_id','opened_at','closed_at','severity',
        'conditions','profile','acknowledged_at','acknowledged_by')},'target_name':names.get(row.target_id,row.target_id)}


TRACE_FIELDS=('id','target_id','address','protocol','port','reason','incident_id','slot','status','finished_at','error_code',
    'reached','path','path_changed')


def trace_view(row,hops=False):
    view={key:getattr(row,key) for key in TRACE_FIELDS}
    view['hop_count']=len(row.hops or [])
    if hops:view['hops']=row.hops
    return view


def save_target(db,body,user,row=None):
    lock(db)
    if row and (row.archived or row.version!=body.version):raise HTTPException(409,'El destino cambió. Actualiza la página.')
    if row and 'destination_id' in body.model_fields_set and body.destination_id!=row.destination_id:
        raise HTTPException(422,'No se puede mover una comprobación con historial a otro destino')
    if body.destination_id and not db.get(MonitoringDestination,body.destination_id):
        raise HTTPException(422,'Destino lógico no encontrado')
    profile=db.get(ProbeProfileVersion,body.profile_version_id)
    if not profile:raise HTTPException(422,'Perfil no encontrado')
    if body.pop_id:
        pop=db.get(Pop,body.pop_id)
        if not pop or pop.archived:raise HTTPException(422,'POP no disponible')
    try:
        config=effective(profile.config,body.overrides)
        protocol=body.protocol if not row or 'protocol' in body.model_fields_set else row.protocol
        check=body.check_config.model_dump() if not row or 'check_config' in body.model_fields_set else row.check_config
        measurement_config(config,protocol,check)
        if row and row.protocol!=protocol and db.scalar(select(ProbeIncident.id).where(ProbeIncident.target_id==row.id,ProbeIncident.closed_at.is_(None)).limit(1)):
            raise HTTPException(409,'Hay incidentes activos. Crea otro destino para un protocolo distinto.')
    except ValueError as exc:raise HTTPException(422,str(exc))
    if not row and db.scalar(select(func.count()).select_from(ProbeTarget).where(ProbeTarget.archived.is_(False)))>=200:
        raise HTTPException(422,'Límite de 200 destinos registrados')
    is_new=row is None
    if is_new:row=ProbeTarget(id=uid(),effective=config);db.add(row)
    old=None if is_new else (row.address,row.family,row.approved_ips,row.effective,row.enabled,row.protocol,row.check_config,row.dns_policy)
    for key,value in body.model_dump(exclude={'version'}).items():
        if not is_new and key in ('categories','service','protocol','check_config','dns_policy','destination_id','essential') and key not in body.model_fields_set:continue
        setattr(row,key,value)
    row.effective=config
    if is_new:row.version=1;row.epoch=1;row.next_due=next_slot(row,now())
    else:
        row.version+=1
        if old!=(row.address,row.family,row.approved_ips,row.effective,row.enabled,row.protocol,row.check_config,row.dns_policy):changed(db,row)
    try:
        ensure_destination(db,row)
        db.flush();capacity(db);audit(db,'probe_target_saved',user.id,row.id);db.commit()
    except IntegrityError:db.rollback();raise HTTPException(409,'Ya existe un destino con ese nombre')
    except ValueError as exc:db.rollback();raise HTTPException(422,str(exc))
    return target_views(db,[row])[0]


def router(current,admin):
    api=APIRouter(prefix='/api/v1')

    @api.get('/monitoring')
    def monitoring(user=Depends(current),db:Session=Depends(get_db)):
        from ..health import evaluate
        from ..cases.summary import summary
        return {**monitoring_overview(db),'odin_health':evaluate(db),'cases':summary(db)}

    @api.put('/monitoring/destinations/{destination_id}')
    def edit_destination(destination_id:str,body:DestinationBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock(db)
        row=db.get(MonitoringDestination,destination_id)
        if not row:raise HTTPException(404,'Destino no encontrado')
        if row.version!=body.version:raise HTTPException(409,'El destino cambió. Actualiza la página.')
        if not body.name.strip():raise HTTPException(422,'Nombre vacío')
        if body.device_id and body.link_id:raise HTTPException(422,'Asocia un equipo o un enlace')
        if body.scope=='external' and (body.device_id or body.link_id):raise HTTPException(422,'Los servicios externos no se asocian a equipos o enlaces propios')
        for key,model in ((body.device_id,Device),(body.link_id,InventoryLink)):
            if key:
                item=db.get(model,key)
                if not item or item.archived:raise HTTPException(422,'Equipo o enlace no disponible')
        row.name=body.name.strip();row.scope=body.scope;row.device_id=body.device_id;row.link_id=body.link_id;row.version+=1
        audit(db,'monitoring_destination_saved',user.id,row.id);db.commit()
        return {'id':row.id,'version':row.version}

    @api.get('/probe-profiles')
    def profiles(user=Depends(current),db:Session=Depends(get_db)):
        rows=list(db.scalars(select(ProbeProfile).where(ProbeProfile.archived.is_(False)).order_by(ProbeProfile.id != DEFAULT_PROFILE_ID,ProbeProfile.name)))
        versions=list(db.scalars(select(ProbeProfileVersion).order_by(ProbeProfileVersion.number.desc())))
        counts=dict(db.execute(select(ProbeTarget.profile_version_id,func.count()).where(ProbeTarget.archived.is_(False))
                               .group_by(ProbeTarget.profile_version_id)).all())
        return [{'id':p.id,'name':p.name,'version':p.version,'versions':[{'id':v.id,'number':v.number,
            'config':v.config,'targets':counts.get(v.id,0)} for v in versions if v.profile_id==p.id]} for p in rows]

    def save_profile(db,body,user,row=None):
        lock(db)
        if row and row.version!=body.version:raise HTTPException(409,'El perfil cambió. Actualiza la página.')
        if not body.name.strip():raise HTTPException(422,'Nombre vacío')
        if row:row.version+=1;row.name=body.name.strip()
        else:row=ProbeProfile(name=body.name.strip(),version=1);db.add(row)
        try:
            db.flush()
            version=ProbeProfileVersion(profile_id=row.id,number=row.version,config=body.config.model_dump())
            db.add(version);db.flush()
            if body.apply_to_targets:
                old_ids=select(ProbeProfileVersion.id).where(ProbeProfileVersion.profile_id==row.id)
                for target in db.scalars(select(ProbeTarget).where(ProbeTarget.profile_version_id.in_(old_ids),ProbeTarget.archived.is_(False)).order_by(ProbeTarget.id).with_for_update()):
                    target.effective=effective(version.config,target.overrides);target.profile_version_id=version.id
                    measurement_config(target.effective,target.protocol,target.check_config)
                    target.version+=1;changed(db,target)
            db.flush();capacity(db)
            audit(db,'probe_profile_saved',user.id,row.id);db.commit()
        except IntegrityError:db.rollback();raise HTTPException(409,'Ya existe un perfil con ese nombre')
        except ValueError as exc:db.rollback();raise HTTPException(422,str(exc))
        return {'id':row.id,'version_id':version.id,'version':row.version}

    @api.post('/probe-profiles',status_code=201)
    def create_profile(body:ProfileBody,user=Depends(admin),db:Session=Depends(get_db)):
        return save_profile(db,body,user)

    @api.put('/probe-profiles/{profile_id}')
    def edit_profile(profile_id:str,body:ProfileBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock(db);row=db.get(ProbeProfile,profile_id)
        if not row:raise HTTPException(404,'Perfil no encontrado')
        return save_profile(db,body,user,row)

    @api.get('/probe-status')
    def probe_status(user=Depends(current),db:Session=Depends(get_db)):
        return origin_view(db)

    @api.post('/targets/resolve')
    def resolve_target(body:ResolveBody,user=Depends(admin),db:Session=Depends(get_db)):
        rate_limit(db,'probe-resolve:'+user.id,20)
        try:return {'addresses':resolve(body.address,body.family)}
        except ProbeError as exc:raise HTTPException(422,exc.code)
        except ValueError as exc:raise HTTPException(422,str(exc))

    @api.get('/target-categories')
    def category_catalog(user=Depends(current)):
        return CATEGORIES

    @api.get('/targets')
    def targets(user=Depends(current),db:Session=Depends(get_db)):
        return target_views(db,list(db.scalars(select(ProbeTarget).where(ProbeTarget.archived.is_(False)).order_by(ProbeTarget.name).limit(200))))

    @api.post('/targets',status_code=201)
    def create_target(body:TargetBody,user=Depends(admin),db:Session=Depends(get_db)):
        return save_target(db,body,user)

    @api.put('/targets/{target_id}')
    def edit_target(target_id:str,body:TargetBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock(db);row=db.scalar(select(ProbeTarget).where(ProbeTarget.id==target_id).with_for_update())
        if not row:raise HTTPException(404,'Destino no encontrado')
        return save_target(db,body,user,row)

    @api.post('/targets/{target_id}/archive')
    def archive_target(target_id:str,body:VersionBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock(db);row=db.scalar(select(ProbeTarget).where(ProbeTarget.id==target_id).with_for_update())
        if not row or row.version!=body.version:raise HTTPException(409,'Actualiza el destino')
        row.archived=True;row.enabled=False;row.version+=1;changed(db,row,'target_archived')
        audit(db,'probe_target_archived',user.id,row.id);db.commit();return {'ok':True}

    @api.post('/targets/{target_id}/probe-runs',status_code=202)
    def test_target(target_id:str,user=Depends(admin),db:Session=Depends(get_db)):
        rate_limit(db,'probe-test:'+user.id,10)
        row=db.scalar(select(ProbeTarget).where(ProbeTarget.id==target_id).with_for_update())
        if not row or row.archived:raise HTTPException(404,'Destino no disponible')
        existing=db.scalar(select(ProbeBatch).where(ProbeBatch.target_id==target_id,ProbeBatch.mode=='manual',((ProbeBatch.status.in_(['pending','running']))|((ProbeBatch.slot==now())&(ProbeBatch.epoch==row.epoch)))))
        if existing:return {'id':existing.id,'status':existing.status}
        batch=ProbeBatch(target_id=target_id,epoch=row.epoch,slot=now(),mode='manual',config=snapshot(row))
        db.add(batch);db.flush();audit(db,'probe_test_requested',user.id,target_id);db.commit()
        return {'id':batch.id,'status':batch.status}

    @api.get('/targets/{target_id}/probe-runs')
    def runs(target_id:str,user=Depends(current),db:Session=Depends(get_db)):
        target_or_404(db,target_id)
        return [{key:getattr(row,key) for key in ('id','mode','status','slot','finished_at','error_code','result')} for row in db.scalars(
            select(ProbeBatch).where(ProbeBatch.target_id==target_id).order_by(ProbeBatch.slot.desc()).limit(20))]

    @api.get('/targets/{target_id}/traces')
    def traces(target_id:str,user=Depends(current),db:Session=Depends(get_db)):
        target_or_404(db,target_id)
        return [trace_view(row) for row in db.scalars(select(ProbeTrace).where(ProbeTrace.target_id==target_id)
            .order_by(ProbeTrace.slot.desc(),ProbeTrace.id).limit(50))]

    @api.post('/targets/{target_id}/traces',status_code=202)
    def request_trace(target_id:str,user=Depends(current),db:Session=Depends(get_db)):
        # Operators diagnose too; the address is always one the check already measures.
        rate_limit(db,'probe-trace:'+user.id,10)
        row=db.scalar(select(ProbeTarget).where(ProbeTarget.id==target_id).with_for_update())
        if not row or row.archived:raise HTTPException(404,'Destino no disponible')
        existing=db.scalar(select(ProbeTrace).where(ProbeTrace.target_id==target_id,ProbeTrace.reason=='manual',ProbeTrace.status.in_(['pending','running'])))
        if existing:db.commit();return trace_view(existing)
        key=trace.request(db,row,'manual',now(),actor_id=user.id)
        if not key:raise HTTPException(422,'El destino aún no tiene direcciones resueltas')
        audit(db,'probe_trace_requested',user.id,target_id);db.commit()
        return trace_view(db.get(ProbeTrace,key))

    @api.get('/traces/{trace_id}')
    def trace_detail(trace_id:str,user=Depends(current),db:Session=Depends(get_db)):
        row=db.get(ProbeTrace,trace_id)
        if not row:raise HTTPException(404,'Traza no encontrada')
        return trace_view(row,hops=True)

    @api.get('/targets/{target_id}/measurements')
    def measurements(target_id:str,hours:int=Query(1,ge=1,le=8760),user=Depends(current),db:Session=Depends(get_db)):
        target=target_or_404(db,target_id);stamp=now();start=stamp-hours*3600
        resolution=0 if hours<=6 else 300 if hours<=72 else 3600
        series=list(db.scalars(select(ProbeSeries).where(ProbeSeries.target_id==target_id,ProbeSeries.started_at<=stamp,
            (ProbeSeries.ended_at.is_(None))|(ProbeSeries.ended_at>=start)).order_by(ProbeSeries.started_at.desc()).limit(21)))
        result=[]
        for item in series[:20]:
            end=min(stamp,item.ended_at or stamp)
            points=[]
            if resolution==0:
                rows=list(db.scalars(select(ProbeRound).where(ProbeRound.series_id==item.id,ProbeRound.slot>=start-item.config['window_seconds']).order_by(ProbeRound.slot)))
                by_slot={r.slot:r for r in rows};interval=item.config['interval_seconds']
                first=item.started_at+max(0,math.ceil((start-item.started_at)/interval))*interval
                for slot in range(first,int(end)+1,interval):
                    row=by_slot.get(slot)
                    valid=bool(row and row.status=='valid')
                    metrics=window_metrics(rows,item.config,slot,item.started_at) if valid else {}
                    points.append({'time':slot,'rtt':metrics.get('p95_ms'),'loss':metrics.get('loss_percent'),'failure':metrics.get('failure_percent'),
                                   'coverage':metrics.get('coverage',0),'status':'valid' if valid else 'no_data'})
            else:
                rows={r.bucket:r for r in db.scalars(select(ProbeRollup).where(ProbeRollup.series_id==item.id,
                    ProbeRollup.resolution==resolution,ProbeRollup.bucket>=start//resolution*resolution,ProbeRollup.bucket<=end))}
                for bucket in range(max(start,item.started_at)//resolution*resolution,int(end)//resolution*resolution+1,resolution):
                    row=rows.get(bucket)
                    points.append({'time':bucket,'rtt':row.rtt_sum/row.received if row and row.received else None,
                        'rtt_min':row.rtt_min if row else None,'rtt_max':row.rtt_max if row else None,
                        'loss':100*(row.sent-row.received)/row.sent if row and row.sent and item.config.get('protocol','icmp')=='icmp' else None,
                        'failure':100*(row.sent-row.received)/row.sent if row and row.sent and item.config.get('protocol','icmp')!='icmp' else None,
                        'coverage':min(1,row.valid/row.expected) if row and row.expected else 0,
                        'status':'valid' if row and row.valid else 'no_data'})
            result.append({'id':item.id,'address':item.address,'started_at':item.started_at,'ended_at':item.ended_at,
                           'config':item.config,'points':points})
        return {'series':result,'truncated':len(series)>20,'resolution_seconds':resolution,'rtt_metric':'p95_window' if resolution==0 else 'mean',
                'origin':'ODIN','start':start,'end':stamp}

    @api.get('/targets/{target_id}/maintenance')
    def maintenance(target_id:str,user=Depends(current),db:Session=Depends(get_db)):
        target_or_404(db,target_id)
        return [{key:getattr(row,key) for key in ('id','starts_at','ends_at','reason','cancelled','cancelled_at')} for row in db.scalars(
            select(ProbeMaintenance).where(ProbeMaintenance.target_id==target_id).order_by(ProbeMaintenance.starts_at.desc()).limit(30))]

    @api.post('/targets/{target_id}/maintenance',status_code=201)
    def add_maintenance(target_id:str,body:MaintenanceBody,user=Depends(admin),db:Session=Depends(get_db)):
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.id==target_id).with_for_update())
        if not target or target.archived:raise HTTPException(404,'Destino no disponible')
        row=ProbeMaintenance(target_id=target_id,actor_id=user.id,**body.model_dump());db.add(row)
        record_configuration(db,target,'maintenance_scheduled',now());audit(db,'probe_maintenance_created',user.id,target_id)
        db.commit();return {'id':row.id}

    @api.post('/maintenance/{maintenance_id}/cancel')
    def cancel_maintenance(maintenance_id:str,user=Depends(admin),db:Session=Depends(get_db)):
        row=db.get(ProbeMaintenance,maintenance_id)
        if not row:raise HTTPException(404,'Mantenimiento no encontrado')
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.id==row.target_id).with_for_update())
        record_configuration(db,target,'maintenance_cancelled',now())
        row.cancelled=True;row.cancelled_at=row.cancelled_at or now();audit(db,'probe_maintenance_cancelled',user.id,row.target_id);db.commit();return {'ok':True}

    @api.get('/incidents')
    def incidents(state:str=Query('active',pattern='^(active|recovered|all)$'),target_id:str|None=None,
                  after:str|None=None,user=Depends(current),db:Session=Depends(get_db)):
        query=select(ProbeIncident)
        if state=='active':query=query.where(ProbeIncident.closed_at.is_(None))
        if state=='recovered':query=query.where(ProbeIncident.closed_at.is_not(None))
        if target_id:query=query.where(ProbeIncident.target_id==target_id)
        if after:
            try:stamp,key=after.split(':',1);stamp=int(stamp)
            except ValueError:raise HTTPException(422,'Cursor inválido')
            query=query.where(or_(ProbeIncident.opened_at<stamp,(ProbeIncident.opened_at==stamp)&(ProbeIncident.id<key)))
        rows=list(db.scalars(query.order_by(ProbeIncident.opened_at.desc(),ProbeIncident.id.desc()).limit(101)))
        more=len(rows)>100;rows=rows[:100]
        names=dict(db.execute(select(ProbeTarget.id,ProbeTarget.name).where(ProbeTarget.id.in_({r.target_id for r in rows}))).all())
        return {'items':[incident_view(row,names) for row in rows],
                'next_cursor':str(rows[-1].opened_at)+':'+rows[-1].id if more else None}

    @api.get('/incidents/{incident_id}')
    def incident(incident_id:str,user=Depends(current),db:Session=Depends(get_db)):
        row=db.get(ProbeIncident,incident_id)
        if not row:raise HTTPException(404,'Incidente no encontrado')
        target=target_or_404(db,row.target_id)
        events=list(db.scalars(select(ProbeIncidentEvent).where(ProbeIncidentEvent.incident_id==row.id).order_by(ProbeIncidentEvent.created_at.desc(),ProbeIncidentEvent.id).limit(201)))
        case_id=db.scalar(select(CaseEvidence.case_id).where(CaseEvidence.source=='probe',CaseEvidence.role=='trigger',CaseEvidence.ref==row.id))
        linked=[trace_view(t) for t in db.scalars(select(ProbeTrace).where(ProbeTrace.incident_id==row.id).order_by(ProbeTrace.slot))]
        return {**incident_view(row,{target.id:target.name}),'target':target_views(db,[target])[0],'case_id':case_id,'traces':linked,
                'events':[{'id':e.id,'kind':e.kind,'at':e.created_at,'actor_id':e.actor_id,'data':e.data} for e in events[:200]],'events_truncated':len(events)>200}

    @api.post('/incidents/{incident_id}/acknowledgments')
    def acknowledge(incident_id:str,user=Depends(current),db:Session=Depends(get_db)):
        row=db.scalar(select(ProbeIncident).where(ProbeIncident.id==incident_id).with_for_update())
        if not row:raise HTTPException(404,'Incidente no encontrado')
        if not row.acknowledged_at:
            row.acknowledged_at=now();row.acknowledged_by=user.id
            db.add(ProbeIncidentEvent(incident_id=row.id,kind='acknowledged',actor_id=user.id))
            audit(db,'probe_incident_acknowledged',user.id,row.id)
        db.commit();return {'ok':True}

    @api.post('/incidents/{incident_id}/observations',status_code=201)
    def observe(incident_id:str,body:NoteBody,user=Depends(current),db:Session=Depends(get_db)):
        rate_limit(db,'probe-note:'+user.id,30)
        if not db.get(ProbeIncident,incident_id):raise HTTPException(404,'Incidente no encontrado')
        if not body.text.strip():raise HTTPException(422,'Escribe una observación')
        db.add(ProbeIncidentEvent(incident_id=incident_id,kind='observation',actor_id=user.id,data={'text':body.text.strip()}))
        db.commit();return {'ok':True}

    @api.get('/overview')
    @api.get('/probe-overview')
    def overview(user=Depends(current),db:Session=Depends(get_db)):
        organized=monitoring_overview(db)
        views=[{**check,'pop_id':destination['pop_id'] if destination['scope']=='network' else None}
               for destination in organized['destinations'] for check in destination['checks']]
        enabled=[v for v in views if v['enabled']]
        measured=[v for v in enabled if v['state'] in ('healthy','warning','critical')]
        pops={}
        for view in enabled:
            if view['pop_id']:pops.setdefault(view['pop_id'],[]).append(view)
        pop_states={key:{'targets':len(group),'with_data':sum(v['state'] in ('healthy','warning','critical') for v in group),
            'severity':'critical' if any(v['incident_severity']=='critical' for v in group) else 'warning' if any(v['incident_severity']=='warning' for v in group) else None,
            'state':'no_data' if any(v['state']=='no_data' for v in group) else 'maintenance' if any(v['state']=='maintenance' for v in group) else 'warming' if any(v['state']=='warming' for v in group) else 'measured'} for key,group in pops.items()}
        return {'stage':'E3','origin':origin_view(db),'targets':len(enabled),'with_data':len(measured),
                'coverage':len(measured)/len(enabled) if enabled else None,
                'incidents':db.scalar(select(func.count()).select_from(ProbeIncident).where(ProbeIncident.closed_at.is_(None))),
                'pops':pop_states}

    return api
