import math
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import (Pop, Device, SourceIdentity, InventoryInterface, Assignment, Candidate, InventoryLink, Integration, SyncRun, now)
from ..security import audit, rate_limit
from ..config import settings
from .service import lock_inventory, queue_run


class PopBody(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    description: str = Field(default='',max_length=2000)
    latitude: float | None = Field(default=None,ge=-90,le=90)
    longitude: float | None = Field(default=None,ge=-180,le=180)
    version: int | None = None

    @model_validator(mode='after')
    def valid(self):
        self.name=self.name.strip()
        if not self.name or (self.latitude is None)!=(self.longitude is None):
            raise ValueError('Nombre y coordenadas inválidos')
        return self


class DeviceBody(BaseModel):
    pop_id: str | None = None
    name: str | None = Field(default=None,min_length=1,max_length=200)
    version: int


class VersionBody(BaseModel):
    version: int


class ResolveBody(BaseModel):
    action: str = Field(pattern='^(merge|dismiss)$')
    keep_id: str | None = None
    left_version: int
    right_version: int
    allow_same_source: bool = False


class ManualResolveBody(BaseModel):
    left_id: str
    right_id: str
    keep_id: str
    left_version: int
    right_version: int
    allow_same_source: bool = False


class LinkBody(BaseModel):
    left_id: str
    right_id: str
    left_port_id: str
    right_port_id: str
    vlans: list[Annotated[int, Field(ge=1,le=4094,strict=True)]] = Field(default_factory=list,max_length=4094)
    description: str = Field(default='',max_length=300)


def pop_view(row,count=0):
    return {'id':row.id,'name':row.name,'description':row.description,'latitude':row.latitude,
            'longitude':row.longitude,'archived':row.archived,'version':row.version,'device_count':count}


def identity_view(row,integrations):
    source=integrations.get(row.source)
    fresh=bool(source and source.synced_at and source.synced_at+1800>=now() and not source.last_error)
    return {'id':row.id,'source':row.source,'external_id':row.external_id,'name':row.name,
            'addresses':row.addresses,'details':row.details,'last_seen':row.last_seen,
            'present':row.present,'fresh':fresh}


def run_view(row):
    return {key:getattr(row,key) for key in ('id','source','mode','status','created_at','finished_at','count','error_code')}


def reconciliation_views(db, ids):
    devices=list(db.scalars(select(Device).where(Device.id.in_(ids))))
    identities={device.id:[] for device in devices}
    for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.device_id.in_(ids))):
        identities[identity.device_id].append({'source':identity.source,'external_id':identity.external_id,
            'name':identity.name,'addresses':identity.addresses,'present':identity.present})
    counts=dict(db.execute(select(SourceIdentity.device_id,func.count(InventoryInterface.id))
        .join(InventoryInterface,InventoryInterface.identity_id==SourceIdentity.id)
        .where(SourceIdentity.device_id.in_(ids),SourceIdentity.present.is_(True),
               InventoryInterface.present.is_(True),InventoryInterface.kind=='port')
        .group_by(SourceIdentity.device_id)).all())
    pops=dict(db.execute(select(Pop.id,Pop.name).where(Pop.id.in_({d.pop_id for d in devices if d.pop_id}))).all())
    return {d.id:{'id':d.id,'name':d.name,'pop_id':d.pop_id,'pop_name':pops.get(d.pop_id),'version':d.version,
                  'sources':identities[d.id],'port_count':counts.get(d.id,0)} for d in devices}


def router(current,admin):
    api=APIRouter(prefix='/api/v1')

    @api.get('/inventory/map-config')
    def map_config(user=Depends(current)):
        return {'tile_url':settings().map_tile_url}

    @api.get('/pops')
    def pops(user=Depends(current),db:Session=Depends(get_db)):
        rows=db.execute(select(Pop,func.count(Device.id)).outerjoin(Device,(Device.pop_id==Pop.id)&Device.archived.is_(False))
                        .where(Pop.archived.is_(False)).group_by(Pop.id).order_by(Pop.name).limit(1000)).all()
        return [pop_view(pop,count) for pop,count in rows]

    @api.post('/pops',status_code=201)
    def create_pop(body:PopBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db)
        row=Pop(**body.model_dump(exclude={'version'}))
        db.add(row)
        try:
            db.flush();audit(db,'pop_created',user.id,row.id);db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'Ya existe un POP con ese nombre')
        return pop_view(row)

    @api.put('/pops/{pop_id}')
    def edit_pop(pop_id:str,body:PopBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db)
        row=db.get(Pop,pop_id)
        if not row or row.archived:
            raise HTTPException(404,'POP no encontrado')
        if body.version!=row.version:
            raise HTTPException(409,'El POP cambió. Actualiza la página.')
        for key,value in body.model_dump(exclude={'version'}).items():
            setattr(row,key,value)
        row.version+=1
        try:
            audit(db,'pop_updated',user.id,row.id);db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'Ya existe un POP con ese nombre')
        return pop_view(row)

    @api.post('/pops/{pop_id}/archive')
    def archive_pop(pop_id:str,body:VersionBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db);row=db.get(Pop,pop_id)
        if not row or row.archived:
            raise HTTPException(404,'POP no encontrado')
        if row.version!=body.version:
            raise HTTPException(409,'El POP cambió. Actualiza la página.')
        if db.scalar(select(Device.id).where(Device.pop_id==pop_id,Device.archived.is_(False)).limit(1)):
            raise HTTPException(409,'Reasigna los equipos antes de archivar el POP')
        from ..models import ProbeTarget
        if db.scalar(select(ProbeTarget.id).where(ProbeTarget.pop_id==pop_id,ProbeTarget.archived.is_(False)).limit(1)):
            raise HTTPException(409,'Reasigna los destinos antes de archivar el POP')
        row.archived=True;row.version+=1;audit(db,'pop_archived',user.id,pop_id);db.commit()
        return {'ok':True}

    @api.get('/devices')
    def devices(q:str=Query('',max_length=100),pop_id:str|None=None,unassigned:bool=False,
                after:str|None=None,limit:int=Query(50,ge=1,le=100),user=Depends(current),db:Session=Depends(get_db)):
        query=select(Device).where(Device.archived.is_(False))
        if q:
            query=query.where(Device.name.ilike('%'+q.replace('%','\\%').replace('_','\\_')+'%'))
        if pop_id:query=query.where(Device.pop_id==pop_id)
        if unassigned:query=query.where(Device.pop_id.is_(None))
        if after:query=query.where(Device.id>after)
        rows=list(db.scalars(query.order_by(Device.id).limit(limit+1)))
        more=len(rows)>limit;rows=rows[:limit]
        integrations={x.kind:x for x in db.scalars(select(Integration))}
        identities={d.id:[] for d in rows}
        for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.device_id.in_(identities))):
            identities[identity.device_id].append(identity_view(identity,integrations))
        counts=dict(db.execute(select(SourceIdentity.device_id,func.count(InventoryInterface.id))
            .join(InventoryInterface,InventoryInterface.identity_id==SourceIdentity.id)
            .where(SourceIdentity.device_id.in_(identities),SourceIdentity.present.is_(True),
                   InventoryInterface.present.is_(True),InventoryInterface.kind=='port')
            .group_by(SourceIdentity.device_id)).all())
        return {'items':[{'id':d.id,'name':d.name,'pop_id':d.pop_id,'version':d.version,'sources':identities[d.id],'port_count':counts.get(d.id,0)} for d in rows],
                'next_cursor':rows[-1].id if more else None}

    @api.get('/devices/{device_id}/ports')
    def device_ports(device_id:str,q:str=Query('',max_length=100),after:str|None=None,
                     user=Depends(current),db:Session=Depends(get_db)):
        device=db.get(Device,device_id)
        if not device or device.archived:raise HTTPException(404,'Equipo no disponible')
        query=select(InventoryInterface,SourceIdentity.source).join(SourceIdentity,InventoryInterface.identity_id==SourceIdentity.id).where(
            SourceIdentity.device_id==device_id,SourceIdentity.present.is_(True),
            InventoryInterface.present.is_(True),InventoryInterface.kind=='port')
        if q:query=query.where(InventoryInterface.name.ilike('%'+q.replace('%','\\%').replace('_','\\_')+'%'))
        if after:query=query.where(InventoryInterface.id>after)
        rows=db.execute(query.order_by(InventoryInterface.id).limit(101)).all()
        more=len(rows)>100;rows=rows[:100]
        return {'items':[{'id':port.id,'name':port.name,'source':source,'last_seen':port.last_seen} for port,source in rows],
                'next_cursor':rows[-1][0].id if more else None}

    @api.get('/devices/{device_id}')
    def device_detail(device_id:str,user=Depends(current),db:Session=Depends(get_db)):
        row=db.get(Device,device_id)
        if not row:raise HTTPException(404,'Equipo no encontrado')
        identities=list(db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==device_id)))
        integrations={x.kind:x for x in db.scalars(select(Integration))}
        ports=list(db.scalars(select(InventoryInterface).where(InventoryInterface.identity_id.in_([x.id for x in identities])).order_by(InventoryInterface.name).limit(1001)))
        history=list(db.scalars(select(Assignment).where(Assignment.device_id==device_id).order_by(Assignment.created_at.desc()).limit(100)))
        summary=reconciliation_views(db,[row.id])[row.id]
        return {**summary,'id':row.id,'name':row.name,'pop_id':row.pop_id,'archived':row.archived,'merged_into':row.merged_into,'version':row.version,
            'sources':[identity_view(x,integrations) for x in identities],
            'interfaces':[{'id':x.id,'source_identity':x.identity_id,'name':x.name,'kind':x.kind,'present':x.present,'last_seen':x.last_seen,'addresses':x.addresses} for x in ports[:1000]],
            'interfaces_truncated':len(ports)>1000,
            'history':[{'from':x.previous_pop_id,'to':x.pop_id,'at':x.created_at,'actor':x.actor_id,'reason':x.reason} for x in history]}

    @api.put('/devices/{device_id}')
    def edit_device(device_id:str,body:DeviceBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db);row=db.get(Device,device_id)
        if not row or row.archived:raise HTTPException(404,'Equipo no encontrado')
        if row.version!=body.version:raise HTTPException(409,'El equipo cambió. Actualiza la página.')
        if body.pop_id:
            pop=db.get(Pop,body.pop_id)
            if not pop or pop.archived:raise HTTPException(422,'POP no disponible')
        if row.pop_id!=body.pop_id:
            db.add(Assignment(device_id=row.id,previous_pop_id=row.pop_id,pop_id=body.pop_id,actor_id=user.id))
            row.pop_id=body.pop_id
        if body.name is not None:
            if not body.name.strip():raise HTTPException(422,'Nombre vacío')
            row.name=body.name.strip()
        row.version+=1;audit(db,'device_updated',user.id,row.id);db.commit()
        return {'ok':True}

    @api.post('/devices/{device_id}/archive')
    def archive_device(device_id:str,body:VersionBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db);row=db.get(Device,device_id)
        if not row or row.archived:raise HTTPException(404,'Equipo no encontrado')
        if row.version!=body.version:raise HTTPException(409,'El equipo cambió')
        row.archived=True;row.version+=1
        db.execute(update(InventoryLink).where(or_(InventoryLink.left_id==row.id,InventoryLink.right_id==row.id)).values(archived=True))
        db.execute(update(Candidate).where(Candidate.status=='pending',or_(Candidate.left_id==row.id,Candidate.right_id==row.id)).values(status='obsolete'))
        audit(db,'device_archived',user.id,row.id);db.commit();return {'ok':True}

    @api.get('/inventory/reconciliation')
    def candidates(user=Depends(current),db:Session=Depends(get_db)):
        rows=list(db.scalars(select(Candidate).where(Candidate.status=='pending').order_by(Candidate.id).limit(100)))
        views=reconciliation_views(db,{key for row in rows for key in (row.left_id,row.right_id)})
        return [{'id':row.id,'left':views[row.left_id],'right':views[row.right_id],'evidence':row.evidence} for row in rows]

    @api.post('/inventory/reconciliation/manual')
    def manual_resolve(body:ManualResolveBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db)
        if body.left_id==body.right_id:raise HTTPException(422,'Selecciona dos registros distintos')
        devices=[db.get(Device,key) for key in (body.left_id,body.right_id)]
        if any(not device or device.archived for device in devices):raise HTTPException(409,'Un equipo ya no está disponible')
        pair=sorted((body.left_id,body.right_id))
        row=db.scalar(select(Candidate).where(Candidate.left_id==pair[0],Candidate.right_id==pair[1]))
        if row and row.status=='merged':raise HTTPException(409,'La unión ya se realizó')
        if not row:
            row=Candidate(left_id=pair[0],right_id=pair[1],evidence=[])
            db.add(row)
        row.status='pending'
        row.evidence=sorted(set(row.evidence+['manual: registros seleccionados por el operador']))
        db.flush()
        versions={body.left_id:body.left_version,body.right_id:body.right_version}
        return resolve(row.id,ResolveBody(action='merge',keep_id=body.keep_id,
            left_version=versions[pair[0]],right_version=versions[pair[1]],allow_same_source=body.allow_same_source),user,db)

    @api.post('/inventory/reconciliation/{candidate_id}')
    def resolve(candidate_id:str,body:ResolveBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db);row=db.get(Candidate,candidate_id)
        if not row or row.status!='pending':raise HTTPException(409,'Coincidencia ya resuelta')
        left,right=db.get(Device,row.left_id),db.get(Device,row.right_id)
        if left.archived or right.archived or left.version!=body.left_version or right.version!=body.right_version:
            raise HTTPException(409,'Los equipos cambiaron. Revisa de nuevo.')
        if body.action=='merge':
            if body.keep_id not in (left.id,right.id):raise HTTPException(422,'Elige el equipo a conservar')
            if left.pop_id and right.pop_id and left.pop_id!=right.pop_id:
                raise HTTPException(409,'Los equipos pertenecen a POPs distintos. Revisa sus asignaciones.')
            keep,other=(left,right) if body.keep_id==left.id else (right,left)
            a={x.source for x in db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==keep.id))}
            b={x.source for x in db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==other.id))}
            if a & b and not body.allow_same_source:
                raise HTTPException(409,'Confirma que los registros de la misma fuente pertenecen al mismo equipo físico.')
            if a & b:
                audit(db,'reconciliation_same_source_confirmed',user.id,row.id)
            if not keep.pop_id and other.pop_id:
                db.add(Assignment(device_id=keep.id,previous_pop_id=None,pop_id=other.pop_id,actor_id=user.id,reason='confirmed_merge'))
                keep.pop_id=other.pop_id
            db.execute(update(SourceIdentity).where(SourceIdentity.device_id==other.id).values(device_id=keep.id))
            for link in db.scalars(select(InventoryLink).where(or_(InventoryLink.left_id==other.id,InventoryLink.right_id==other.id))):
                if link.left_id==other.id:link.left_id=keep.id
                if link.right_id==other.id:link.right_id=keep.id
                if link.left_id==link.right_id:link.archived=True
            db.execute(update(Candidate).where(Candidate.status=='pending',Candidate.id!=row.id,or_(Candidate.left_id==other.id,Candidate.right_id==other.id)).values(status='obsolete'))
            other.archived=True;other.merged_into=keep.id;other.version+=1;keep.version+=1
            row.status='merged'
        else:row.status='dismissed'
        row.resolved_at=now();row.resolved_by=user.id;audit(db,'reconciliation_'+row.status,user.id,row.id);db.commit()
        return {'ok':True,'device_id':body.keep_id if body.action=='merge' else None}

    @api.get('/inventory/links')
    def links(user=Depends(current),db:Session=Depends(get_db)):
        rows=list(db.scalars(select(InventoryLink).where(InventoryLink.archived.is_(False)).limit(500)))
        ids={key for row in rows for key in (row.left_id,row.right_id)}
        devices={d.id:d for d in db.scalars(select(Device).where(Device.id.in_(ids)))}
        pops={p.id:p for p in db.scalars(select(Pop).where(Pop.id.in_({d.pop_id for d in devices.values() if d.pop_id})))}
        port_ids={key for row in rows for key in (row.left_port_id,row.right_port_id) if key}
        ports={p.id:p for p in db.scalars(select(InventoryInterface).where(InventoryInterface.id.in_(port_ids)))}
        result=[]
        for row in rows:
            left,right=devices[row.left_id],devices[row.right_id]
            a,b=pops.get(left.pop_id),pops.get(right.pop_id)
            points=None
            if not a or not b:reason='unassigned'
            elif a.latitude is None or b.latitude is None:reason='missing_coordinates'
            elif a.id==b.id or (a.latitude,a.longitude)==(b.latitude,b.longitude):reason='same_location'
            else:
                reason=None;points=[[a.latitude,a.longitude],[b.latitude,b.longitude]]
            data={key:getattr(row,key) for key in ('id','left_id','right_id','left_port_id','right_port_id','vlans','description')}
            data.update(left_name=left.name,right_name=right.name,map_points=points,map_reason=reason,
                        left_pop_name=a.name if a else None,right_pop_name=b.name if b else None)
            for side in ('left','right'):
                port=ports.get(getattr(row,side+'_port_id'))
                data[side+'_port_name']=port.name if port else None
                data[side+'_port_present']=port.present if port else None
            result.append(data)
        return result

    @api.post('/inventory/links',status_code=201)
    def create_link(body:LinkBody,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db)
        if body.left_id==body.right_id:raise HTTPException(422,'Selecciona dos equipos distintos')
        for key in (body.left_id,body.right_id):
            d=db.get(Device,key)
            if not d or d.archived:raise HTTPException(422,'Equipo no disponible')
        for device_id,port_id in ((body.left_id,body.left_port_id),(body.right_id,body.right_port_id)):
            port=db.get(InventoryInterface,port_id)
            identity=db.get(SourceIdentity,port.identity_id) if port else None
            if not port or not identity or identity.device_id!=device_id or not identity.present or not port.present or port.kind!='port':
                raise HTTPException(422,'El puerto no pertenece al equipo o ya no está disponible. Actualiza el inventario.')
            occupied=db.scalar(select(InventoryLink.id).where(InventoryLink.archived.is_(False),or_(
                InventoryLink.left_port_id==port_id,InventoryLink.right_port_id==port_id)).limit(1))
            if occupied:raise HTTPException(409,'El puerto ya tiene un enlace físico activo.')
        body.vlans=sorted(set(body.vlans))
        row=InventoryLink(**body.model_dump(),actor_id=user.id);db.add(row);db.flush()
        audit(db,'link_created',user.id,row.id);db.commit();return {'id':row.id}

    @api.post('/inventory/links/{link_id}/archive')
    def archive_link(link_id:str,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db);row=db.get(InventoryLink,link_id)
        if not row:raise HTTPException(404,'Enlace no encontrado')
        row.archived=True;audit(db,'link_archived',user.id,link_id);db.commit();return {'ok':True}

    @api.get('/inventory/runs')
    def runs(user=Depends(current),db:Session=Depends(get_db)):
        return [run_view(x) for x in db.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(50))]

    @api.post('/admin/integrations/{kind}/{action}',status_code=202)
    def request_run(kind:str,action:str,user=Depends(admin),db:Session=Depends(get_db)):
        # Graylog provides logs, not inventory: it only supports the connection test.
        if (kind,action) not in {('zabbix','test'),('zabbix','sync'),('librenms','test'),('librenms','sync'),('graylog','test'),('llama_server','test'),('deepseek','test'),('telegram','test')}:
            raise HTTPException(404,'Acción no disponible')
        rate_limit(db,'inventory:'+user.id,20)
        row=db.get(Integration,kind)
        if not row or not row.encrypted_secret:raise HTTPException(422,'Configura URL y token')
        # A model API is reached by public address only; the other sources by their allowed networks.
        if not row.allowed_networks and kind!='deepseek':raise HTTPException(422,'Configura las redes permitidas del servidor')
        run=queue_run(db,kind,action,user.id);db.commit();return run_view(run)

    return api
