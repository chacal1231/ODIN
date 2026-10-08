"""Authenticated read-only API; ingestion is an internal adapter contract."""
import base64
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, Field, field_validator
from ..security import audit
from .collector import TOPICS, INTERVALS, stream_name
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_, or_, update, func, text
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import SourceEvent, EventStream, EventPage, EventCollector, Integration, Device, Job, LogSender, SourceIdentity, now
from ..inventory.service import lock_inventory, identity_suggestions, confirm_identity
from .contracts import Source, MAX_TIMESTAMP


def encode_cursor(event):
    return base64.urlsafe_b64encode(json.dumps([event.occurred_at,event.id]).encode()).decode()


class SenderAssignment(BaseModel):
    device_id: str | None = Field(default=None, max_length=36)


class CollectorSettings(BaseModel):
    version: int = Field(ge=1)
    enabled: bool
    timezone: str | None = Field(default=None, max_length=80)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        if value is not None:
            try:ZoneInfo(value)
            except (ZoneInfoNotFoundError,ValueError):raise ValueError('Zona horaria inválida')
        return value


def router(current, admin):
    api=APIRouter(prefix='/api/v1')

    @api.get('/event-sources')
    def sources(user=Depends(current),db:Session=Depends(get_db)):
        result=[];stamp=now()
        for source in TOPICS:
            integration=db.get(Integration,source)
            collectors=list(db.scalars(select(EventCollector).where(EventCollector.source==source)))
            result.append({'source':source,'configured':bool(integration and integration.encrypted_secret and integration.allowed_networks),
                'version':integration.version if integration else None,'enabled':bool(integration and integration.events_enabled),
                'timezone':integration.events_timezone if integration else None,'interval_seconds':INTERVALS[source],
                'collectors':[{**{k:getattr(c,k) for k in ('topic','last_attempt_at','last_success_at','last_error','last_count','next_due')},
                    'stale':not c.last_success_at or c.last_success_at<stamp-max(180,INTERVALS[source]*3)} for c in collectors]})
        return result

    @api.put('/event-sources/{source}')
    def configure(source:str,body:CollectorSettings,user=Depends(admin),db:Session=Depends(get_db)):
        if source not in TOPICS:raise HTTPException(404,'Fuente no compatible')
        integration=db.scalar(select(Integration).where(Integration.kind==source).with_for_update())
        if not integration:raise HTTPException(404,'Configura la fuente en Administración')
        if integration.version!=body.version:raise HTTPException(409,'Actualiza la configuración')
        if body.enabled and (not integration.encrypted_secret or not integration.allowed_networks):raise HTTPException(422,'Faltan credenciales o redes permitidas')
        if source=='librenms' and body.enabled and not body.timezone:raise HTTPException(422,'Define la zona horaria de LibreNMS')
        integration.events_enabled=body.enabled;integration.events_timezone=body.timezone;integration.version+=1
        audit(db,'event_source_configured',user.id,source);db.commit();return {'ok':True}

    @api.post('/event-sources/{source}/retry')
    def retry(source:str,user=Depends(admin),db:Session=Depends(get_db)):
        integration=db.get(Integration,source)
        if source not in TOPICS or not integration:raise HTTPException(404,'Fuente no encontrada')
        if not integration.events_enabled:raise HTTPException(409,'La lectura está pausada')
        # Match worker lock order: jobs before collector rows; never interrupt a live lease.
        db.execute(update(Job).where(Job.id.in_(select(EventCollector.job_id).where(EventCollector.source==source)),Job.status=='pending').values(run_after=now()))
        db.execute(update(EventCollector).where(EventCollector.source==source).values(next_due=now()))
        audit(db,'event_source_retry_requested',user.id,source);db.commit();return {'ok':True}

    @api.get('/event-streams')
    def streams(user=Depends(current),db:Session=Depends(get_db)):
        keys=('id','source','name','version','coverage','reason','window_start','window_end',
              'last_attempt_at','last_success_at','last_event_at','complete_through')
        active={(c.source,c.stream_name) for c in db.scalars(select(EventCollector))}
        return {'observed_at':now(),'streams':[{**{k:getattr(s,k) for k in keys},'active':(s.source,s.name) in active} for s in db.scalars(select(EventStream).order_by(EventStream.source,EventStream.name))]}

    @api.get('/event-streams/{stream_id}/pages')
    def pages(stream_id:str,before:int|None=Query(None,ge=1,le=2147483647),user=Depends(current),db:Session=Depends(get_db)):
        if not db.get(EventStream,stream_id):raise HTTPException(404,'Flujo no encontrado')
        query=select(EventPage).where(EventPage.stream_id==stream_id)
        if before is not None:query=query.where(EventPage.checkpoint_version<before)
        rows=list(db.scalars(query.order_by(EventPage.checkpoint_version.desc()).limit(51)))
        keys=('id','coverage','reason','window_start','window_end','event_count','changed_count','received_at','checkpoint_version')
        return {'items':[{k:getattr(r,k) for k in keys} for r in rows[:50]],'next_before':rows[49].checkpoint_version if len(rows)>50 else None}

    @api.get('/source-events')
    def events(source:Source|None=None,status:str|None=Query(None,pattern='^(open|recovered|info|withdrawn|changes)$'),
               device_id:str|None=Query(None,max_length=36),start:int|None=Query(None,ge=0,le=MAX_TIMESTAMP),end:int|None=Query(None,ge=0,le=MAX_TIMESTAMP),
               cursor:str|None=Query(None,max_length=200),limit:int=Query(50,ge=1,le=100),
               user=Depends(current),db:Session=Depends(get_db)):
        if start is not None and end is not None and start>end:raise HTTPException(422,'Ventana invertida')
        query=select(SourceEvent,EventStream).join(EventStream,SourceEvent.stream_id==EventStream.id)
        if source:query=query.where(EventStream.source==source)
        # 'changes' hides informational logs: only openings, recoveries and withdrawals.
        if status=='changes':query=query.where(SourceEvent.status!='info')
        elif status:query=query.where(SourceEvent.status==status)
        if device_id:query=query.where(SourceEvent.device_id==device_id)
        if start is not None:query=query.where(SourceEvent.occurred_at>=start)
        if end is not None:query=query.where(SourceEvent.occurred_at<=end)
        if cursor:
            try:
                stamp,key=json.loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
                if type(stamp)!=int or stamp<0 or stamp>9223372036854775807 or not isinstance(key,str) or len(key)!=36:raise ValueError()
            except (ValueError,TypeError):raise HTTPException(422,'Cursor inválido')
            query=query.where(or_(SourceEvent.occurred_at<stamp,and_(SourceEvent.occurred_at==stamp,SourceEvent.id<key)))
        rows=db.execute(query.order_by(SourceEvent.occurred_at.desc(),SourceEvent.id.desc()).limit(limit+1)).all()
        keys=('id','external_id','identity_kind','entity_id','device_id','occurred_at','source_updated_at',
              'received_at','last_received_at','kind','status','severity','original_severity','title','recovery_of','reference',
              'condition_key','original_state','time_basis')
        device_ids={e.device_id for e,s in rows if e.device_id}
        device_names={d.id:d.name for d in db.scalars(select(Device).where(Device.id.in_(device_ids)))}
        return {'items':[{**{k:getattr(e,k) for k in keys},'source':s.source,'stream':s.name,'device_name':device_names.get(e.device_id),
                'present_in_latest_snapshot':e.last_received_at >= s.last_success_at if e.time_basis=='snapshot' and s.last_success_at else None} for e,s in rows[:limit]],
                'next_cursor':encode_cursor(rows[limit-1][0]) if len(rows)>limit else None}

    @api.get('/log-senders')
    def senders(user=Depends(current),db:Session=Depends(get_db)):
        rows=list(db.scalars(select(LogSender).order_by(LogSender.name)))
        counts=dict(db.execute(select(SourceEvent.entity_id,func.count()).join(EventStream,EventStream.id==SourceEvent.stream_id)
            .where(EventStream.source=='graylog').group_by(SourceEvent.entity_id)).all())
        matches=identity_suggestions(db,[(r.name,r.address) for r in rows],'graylog')
        return [{'name':r.name,'address':r.address,'first_seen':r.first_seen,'last_seen':r.last_seen,
                 'messages':counts.get(r.name,0),**matches[r.name]} for r in rows]

    @api.put('/log-senders/{name}')
    def assign_sender(name:str,body:SenderAssignment,user=Depends(admin),db:Session=Depends(get_db)):
        lock_inventory(db)
        sender=db.get(LogSender,name)
        if not sender:raise HTTPException(404,'Origen de logs no encontrado')
        try:confirm_identity(db,'graylog',name,sender.address,sender.last_seen,body.device_id,user.id)
        except ValueError as exc:raise HTTPException(422,str(exc))
        db.execute(text('''UPDATE source_events e SET device_id=:device FROM event_streams s
            WHERE e.stream_id=s.id AND s.source='graylog' AND e.entity_id=:name AND e.device_id IS DISTINCT FROM :device'''),
            {'device':body.device_id,'name':name})
        audit(db,'log_sender_assigned' if body.device_id else 'log_sender_unassigned',user.id,name)
        db.commit();return {'ok':True}

    return api
