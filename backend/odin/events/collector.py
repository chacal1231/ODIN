"""Durable, bounded collectors. Network reads never hold database locks."""
import json
import time
from sqlalchemy import select, text, func
from sqlalchemy.dialects.postgresql import insert
from ..db import session
from ..models import Integration,EventCollector,EventStream,SourceEvent,Job,LogSender,now,uid
from ..jobs import enqueue,heartbeat
from ..security import decrypt
from ..inventory.connectors import ConnectorError
from .contracts import EventPageInput
from .service import ingest_page,IngestionConflict,fingerprint
from .connectors import ZabbixEvents,LibreEvents,PAGE_SIZE
from .graylog import Graylog, window, SETTLE

TOPICS={'zabbix':('events',),'librenms':('alerts','alert_history'),'graylog':('logs',)}
INTERVALS={'zabbix':30,'librenms':60,'graylog':60}


def stream_name(integration,topic):
    # Changing the source server or time interpretation must not reuse its IDs/checkpoints.
    identity={'endpoint':integration.endpoint.rstrip('/'),'timezone':integration.events_timezone if integration.kind=='librenms' else None}
    return topic+':'+fingerprint(identity)[:12]


def schedule(db):
    stamp=now()
    for source in TOPICS:
        integration=db.scalar(select(Integration).where(Integration.kind==source).with_for_update())
        if not integration:continue
        for topic in TOPICS[source]:
            key=source+':'+topic
            name=stream_name(integration,topic)
            db.execute(insert(EventCollector).values(id=key,source=source,topic=topic,stream_name=name,
                config_version=integration.version,next_due=stamp).on_conflict_do_nothing(index_elements=['id']))
            row=db.scalar(select(EventCollector).where(EventCollector.id==key).with_for_update())
            if row.config_version!=integration.version:
                if row.stream_name!=name:
                    row.last_attempt_at=None;row.last_success_at=None;row.last_count=0
                row.stream_name=name;row.config_version=integration.version;row.next_due=stamp;row.failure_count=0;row.last_error=None
            if not integration.events_enabled:continue
            if source=='librenms' and not integration.events_timezone:
                row.last_error='timezone_required';continue
            if not integration.encrypted_secret or not integration.allowed_networks:
                row.last_error='source_not_configured';continue
            previous=db.get(Job,row.job_id) if row.job_id else None
            if previous and previous.status in ('pending','running'):continue
            if previous and previous.status=='failed' and not row.last_error:
                row.last_error='attempts_exhausted';row.next_due=max(row.next_due,stamp+60)
            if row.next_due>stamp:continue
            job=enqueue(db,'event_poll',{'collector_id':key,'config_version':integration.version},'event-poll:'+uid())
            row.job_id=job.id
    db.commit()


def progress_for(stream,source,topic,stamp):
    if source=='graylog':
        if not stream or not stream.cursor:
            return {'bootstrap':True,'start':stamp-SETTLE,'end':stamp-SETTLE,'offset':0}
        return window(json.loads(stream.cursor),stamp)
    if stream and stream.cursor:
        progress=json.loads(stream.cursor)
        if progress.pop('caught_up',False):
            if source=='zabbix':
                return {'phase':'history','last':progress['last_event_id'],'high_water':progress['last_event_id'],
                    'incremental':True,'start':max(0,progress['end']-300),'end':stamp}
            progress['end']=stamp
        return progress
    if source=='zabbix':return {'phase':'history','last':0,'start':max(0,(stream.complete_through if stream and stream.complete_through else stamp-86400)-300),'end':stamp}
    return {'last':0,'start':0 if topic=='alert_history' else stamp,'end':stamp}


def link_recoveries(db,stream,events):
    seen={}
    for event in events:
        previous=seen.get(event.condition_key)
        if previous is None and stream:
            previous=db.scalar(select(SourceEvent).where(SourceEvent.stream_id==stream.id,
                SourceEvent.condition_key==event.condition_key,SourceEvent.external_id<event.external_id)
                .order_by(SourceEvent.external_id.desc()).limit(1))
        if event.status=='recovered' and previous and previous.status=='open':event.recovery_of=previous.external_id
        seen[event.condition_key]=event


def link_log_recoveries(db,stream,events):
    """Log IDs carry no order: pair a recovery with the latest earlier state of its condition by time."""
    seen={}
    for event in events:
        if not event.condition_key:continue
        previous=seen.get(event.condition_key)
        if previous is None and stream:
            previous=db.scalar(select(SourceEvent).where(SourceEvent.stream_id==stream.id,
                SourceEvent.condition_key==event.condition_key,SourceEvent.occurred_at<=event.occurred_at,
                SourceEvent.external_id!=event.external_id).order_by(SourceEvent.occurred_at.desc(),SourceEvent.received_at.desc()).limit(1))
        if event.status=='recovered' and previous and previous.status=='open':event.recovery_of=previous.external_id
        seen[event.condition_key]=event


def record_senders(db,senders):
    for name,data in senders.items():
        db.execute(insert(LogSender).values(name=name,address=data['address'],first_seen=data['last_seen'],last_seen=data['last_seen'])
            .on_conflict_do_update(index_elements=['name'],set_={'address':data['address'],'last_seen':data['last_seen']},
                where=LogSender.last_seen<=data['last_seen']))


def valid_job(job,token):
    return job and job.status=='running' and job.lease_token==token and job.lease_until and job.lease_until>now()


def execute(job_id,token):
    with session() as db:
        job=db.get(Job,job_id)
        if not valid_job(job,token):return
        row=db.get(EventCollector,job.payload['collector_id']);integration=db.get(Integration,row.source)
        version=job.payload['config_version']
        if not integration.events_enabled or integration.version!=version:
            job.status='done';job.lease_token=None;job.lease_until=None;job.result={'status':'cancelled','error':'configuration_changed'};db.commit();return
        source,topic,name=row.source,row.topic,row.stream_name
        endpoint=integration.endpoint;secret=decrypt(integration.encrypted_secret) if integration.encrypted_secret else ''
        networks=integration.allowed_networks;zone=integration.events_timezone
        stream=db.scalar(select(EventStream).where(EventStream.source==source,EventStream.name==name))
        expected=stream.version if stream else 0;cursor=stream.cursor if stream else None
        progress=progress_for(stream,source,topic,now())
        if stream and topic=='alert_history' and progress['start']==0:
            progress['start']=db.scalar(select(func.min(SourceEvent.occurred_at)).where(SourceEvent.stream_id==stream.id)) or 0
        known=[];existing={}
        if stream and source=='zabbix' and progress['phase']=='reconcile':
            known=list(db.scalars(select(SourceEvent).where(SourceEvent.stream_id==stream.id,
                SourceEvent.status=='open',SourceEvent.id>progress['last']).order_by(SourceEvent.id).limit(PAGE_SIZE)))
        if stream and topic=='alerts':existing={e.external_id:e for e in db.scalars(select(SourceEvent).where(SourceEvent.stream_id==stream.id))}
    def renew():
        with session() as db:
            if not heartbeat(db,job_id,token):raise ConnectorError('lease_expired')
    result=None;error=None;observed=time.time_ns()
    try:
        if source=='zabbix':result=ZabbixEvents(endpoint,secret,networks,renew).read(progress,known)
        elif source=='graylog':result=Graylog(endpoint,secret,networks,renew).read(progress)
        else:
            if not zone:raise ConnectorError('timezone_required')
            adapter=LibreEvents(endpoint,secret,networks,renew,zone)
            result=adapter.snapshots(observed,existing) if topic=='alerts' else adapter.history(progress)
    except ConnectorError as exc:error=exc.code
    except Exception:error='invalid_response'
    with session() as db:
        job=db.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if not valid_job(job,token):return
        integration=db.scalar(select(Integration).where(Integration.kind==source).with_for_update())
        row=db.scalar(select(EventCollector).where(EventCollector.id==source+':'+topic).with_for_update())
        if integration.version!=version or not integration.events_enabled:
            job.status='done';job.result={'status':'cancelled','error':'configuration_changed'}
            job.lease_token=None;job.lease_until=None;db.commit();return
        if not error and topic=='alert_history':
            stream=db.scalar(select(EventStream).where(EventStream.source==source,EventStream.name==name))
            link_recoveries(db,stream,result.events)
        if not error and source=='graylog':
            stream=db.scalar(select(EventStream).where(EventStream.source==source,EventStream.name==name))
            link_log_recoveries(db,stream,result.events)
        page=EventPageInput(request_key=job.id+':'+str(job.attempts),expected_version=expected,
            window_start=progress['start'],window_end=progress['end'],cursor=cursor,limit=500,
            next_cursor=cursor if error else json.dumps(result.progress,sort_keys=True) if result.progress else None,
            coverage='unavailable' if error else result.coverage,reason=error or result.reason,
            events=[] if error else result.events)
        try:
            receipt=ingest_page(db,source,name,page)
        except IngestionConflict as exc:
            error=exc.code;receipt=None
        if receipt and source=='graylog':record_senders(db,result.senders)
        if receipt:
            db.execute(text('''UPDATE source_events e SET device_id=i.device_id FROM source_identities i, event_streams s
                WHERE e.stream_id=s.id AND s.source=:source AND s.name=:name
                AND i.source=s.source AND i.external_id=e.entity_id AND e.device_id IS DISTINCT FROM i.device_id'''),
                {'source':source,'name':name})
        stamp=now();row.last_attempt_at=stamp;row.last_error=error
        if error:
            row.failure_count+=1
            delay=min(900,INTERVALS[source]*2**min(row.failure_count-1,5))
            row.next_due=stamp+delay
            job.status='pending' if job.attempts<4 else 'failed';job.run_after=row.next_due
        else:
            row.failure_count=0;row.last_success_at=stamp;row.last_count=len(result.events)
            continuation=result.progress is not None and not result.progress.get('caught_up')
            row.next_due=stamp+(2 if continuation else INTERVALS[source]);job.status='done'
        job.result={'status':job.status,'error':error,'page_id':receipt['page_id'] if receipt else None}
        job.lease_token=None;job.lease_until=None;db.commit()
