import math
from sqlalchemy import select, text
from ..models import (ProbeTarget, ProbeBatch, ProbeSeries, ProbeState, ProbeMaintenance,
                      ProbeIncident, ProbeOrigin, now, uid)
from ..config import settings
from .config import ProbeConfig, measurement_config
from .transport import duration
from .detector import record_configuration


def lock(db):
    # Shared with inventory writes: POP archival and target assignment serialize.
    db.execute(text('SELECT pg_advisory_xact_lock(712002)'))


def slot_at(target, stamp):
    interval=target.effective['interval_seconds']
    offset=int(target.id.replace('-','')[:8],16)%interval
    return stamp-(stamp-offset)%interval


def next_slot(target,stamp):
    return slot_at(target,stamp)+target.effective['interval_seconds']


def capacity(db):
    targets=list(db.scalars(select(ProbeTarget).where(ProbeTarget.enabled.is_(True),ProbeTarget.archived.is_(False))))
    total=sum(max(1,len(t.resolved_ips or t.approved_ips)) for t in targets)
    cost=0
    for target in targets:
        count=max(1,len(target.resolved_ips or target.approved_ips))
        config=snapshot(target)
        budget=duration(config,count)
        if target.protocol!='icmp' and budget+5>=min(75,config['interval_seconds']*.8):
            raise ValueError('La ronda supera el intervalo. Reduce intentos o aumenta el intervalo.')
        # One fping process measures every address of a check within the same time; TCP/HTTPS budget already counts them.
        cost+=budget/config['interval_seconds']
    if total>settings().probe_max_addresses or cost>settings().probe_concurrency*0.7:
        raise ValueError('Se supera la capacidad del sondeador. Reduce destinos, paquetes o aumenta el intervalo.')


def effective(config,overrides):
    unknown=set(overrides)-set(ProbeConfig.model_fields)
    if unknown:raise ValueError('Parámetro de sondeo desconocido')
    return ProbeConfig(**{**config,**overrides}).model_dump()


def changed(db,target,kind='configuration_changed'):
    stamp=now()
    target.epoch+=1;target.changed_at=stamp;target.lease_token=None;target.lease_until=None
    target.last_error=None;target.next_due=next_slot(target,stamp)
    for series in db.scalars(select(ProbeSeries).where(ProbeSeries.target_id==target.id,ProbeSeries.ended_at.is_(None))):
        series.ended_at=stamp
    record_configuration(db,target,kind,stamp)


def maintenance_at(db,target_id,stamp):
    return db.scalar(select(ProbeMaintenance.id).where(ProbeMaintenance.target_id==target_id,
        ProbeMaintenance.cancelled.is_(False),ProbeMaintenance.starts_at<=stamp,ProbeMaintenance.ends_at>stamp).limit(1)) is not None


def claim(db,stamp=None):
    stamp=stamp or now()
    # Abandoned rounds are retained as invalid, never retried as historical pings.
    for batch in db.scalars(select(ProbeBatch).where(ProbeBatch.status=='running',ProbeBatch.lease_until<stamp).with_for_update(skip_locked=True).limit(50)):
        batch.status='failed';batch.error_code='probe_interrupted';batch.finished_at=stamp
    db.commit()
    batch=db.scalar(select(ProbeBatch).join(ProbeTarget,ProbeTarget.id==ProbeBatch.target_id).where(ProbeBatch.mode=='manual',ProbeBatch.status=='pending', (ProbeTarget.lease_until.is_(None))|(ProbeTarget.lease_until<=stamp))
                    .order_by(ProbeBatch.slot).with_for_update(skip_locked=True,of=ProbeTarget).limit(1))
    if batch:
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.id==batch.target_id).with_for_update())
        if target.archived or target.epoch!=batch.epoch:
            batch.status='cancelled';batch.error_code='configuration_changed';batch.finished_at=stamp;db.commit();return {'skipped':True}
        if target.lease_until and target.lease_until>stamp:
            db.commit();return None
    else:
        target=db.scalar(select(ProbeTarget).where(ProbeTarget.enabled.is_(True),ProbeTarget.archived.is_(False),
            ProbeTarget.next_due<=stamp).order_by(ProbeTarget.next_due).with_for_update(skip_locked=True).limit(1))
        if not target:db.commit();return None
        slot=slot_at(target,stamp)
        target.next_due=slot+target.effective['interval_seconds']
        if stamp-slot>5 or (target.lease_until and target.lease_until>stamp):
            db.commit();return {'skipped':True}
        existing=db.scalar(select(ProbeBatch.id).where(ProbeBatch.target_id==target.id,ProbeBatch.epoch==target.epoch,
            ProbeBatch.slot==slot,ProbeBatch.mode=='scheduled'))
        if existing:db.commit();return {'skipped':True}
        batch=ProbeBatch(target_id=target.id,epoch=target.epoch,slot=slot,mode='scheduled',config=snapshot(target))
        db.add(batch)
    token=uid();batch.token=token;batch.status='running';batch.lease_until=stamp+90
    target.lease_token=token;target.lease_until=stamp+90
    db.commit()
    return {'id':batch.id,'token':token,'target_id':target.id,'address':target.address,'family':target.family,
            'approved_ips':list(target.approved_ips),'dns_policy':target.dns_policy,'epoch':target.epoch,'config':batch.config}


def origin_view(db,stamp=None):
    stamp=stamp or now();origin=db.get(ProbeOrigin,'odin')
    if not origin:return {'state':'no_data','heartbeat':None,'error_code':'probe_not_started'}
    return {'state':'healthy' if origin.heartbeat>=stamp-30 and not origin.error_code else 'no_data',
            'heartbeat':origin.heartbeat,'error_code':origin.error_code if origin.heartbeat>=stamp-30 else 'probe_stale',
            'version':origin.fping_version}


def target_views(db, targets, stamp=None):
    stamp=stamp or now();origin=origin_view(db,stamp)
    ids=[t.id for t in targets]
    states={};series={}
    for s in db.scalars(select(ProbeSeries).where(ProbeSeries.target_id.in_(ids),ProbeSeries.ended_at.is_(None))):
        series[s.id]=s
    for state in db.scalars(select(ProbeState).where(ProbeState.target_id.in_(ids))):
        if state.series_id in series:states.setdefault(state.target_id,[]).append(state)
    incidents={}
    for i in db.scalars(select(ProbeIncident).where(ProbeIncident.target_id.in_(ids),ProbeIncident.closed_at.is_(None))):
        incidents.setdefault(i.target_id,[]).append(i)
    maintenance_ids=set(db.scalars(select(ProbeMaintenance.target_id).where(ProbeMaintenance.target_id.in_(ids),
        ProbeMaintenance.cancelled.is_(False),ProbeMaintenance.starts_at<=stamp,ProbeMaintenance.ends_at>stamp)))
    from .baseline import latest
    baselines=latest(db,ids) if ids else {}
    result=[]
    for target in targets:
        measurements=[]
        for state in states.get(target.id,[]):
            fresh=bool(state.last_slot and state.last_slot>=stamp-target.effective['interval_seconds']*2)
            view={**state.metrics,'address':state.address,'series_id':state.series_id,'last_slot':state.last_slot,'conditions':state.conditions}
            if not fresh or origin['state']!='healthy':view['state']='no_data'
            measurements.append(view)
        if target.archived or not target.enabled:status='pending_validation' if target.validation.get('status')=='failed' else 'paused'
        elif target.id in maintenance_ids:status='maintenance'
        elif origin['state']!='healthy' or target.last_error or not measurements or any(m['state']=='no_data' for m in measurements):status='no_data'
        elif any(m['state']=='critical' for m in measurements):status='critical'
        elif any(m['state']=='warning' for m in measurements):status='warning'
        elif any(m['state'] in ('warming','maintenance') for m in measurements):status='warming'
        else:status='healthy'
        active=incidents.get(target.id,[])
        result.append({**{key:getattr(target,key) for key in ('id','name','address','family','pop_id','description',
            'destination_id','essential','protocol','check_config','dns_policy','catalog_metadata','validation','categories','service','profile_version_id','overrides','effective','approved_ips','enabled','archived','version','epoch','resolved_ips','resolved_at','last_error','threshold_mode','adaptive')},
            'baseline':baseline_view(baselines.get(target.id)),
            'state':status,'measurements':measurements,'active_incidents':len(active),
            'incident_severity':'critical' if any(i.severity=='critical' for i in active) else 'warning' if active else None})
    return result


def baseline_view(row):
    if not row:return None
    return {key:getattr(row,key) for key in ('id','computed_at','protocol','status','days','coverage','windows','metrics','proposal','flags',
                                             'applied_at','applied_by')}


def snapshot(target):
    return measurement_config(target.effective,target.protocol or 'icmp',target.check_config)
