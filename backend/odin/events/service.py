"""At-least-once ingestion: events, receipt, outbox job and cursor share a transaction."""
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from pydantic import TypeAdapter
from ..models import EventStream, SourceEvent, EventPage, SourceIdentity, Job, now, uid
from ..jobs import enqueue, finish
from ..db import session
from .contracts import Source, EventPageInput


class IngestionConflict(ValueError):
    def __init__(self,message,code):
        super().__init__(message);self.code=code


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def ingest_page(db, source, name, page: EventPageInput):
    """Caller commits. A rejected page rolls back even if the caller catches the error.

    Source event IDs are scoped by stream. Revisions use source_updated_at, never
    reception order. Fingerprint identities retain their documented ambiguity.
    """
    TypeAdapter(Source).validate_python(source)
    if not isinstance(name,str) or not 1 <= len(name) <= 80:
        raise ValueError('Nombre de flujo inválido')
    page=EventPageInput.model_validate(page.model_dump())
    digest=fingerprint(page.model_dump())
    stamp=now()
    with db.begin_nested():
        db.execute(insert(EventStream).values(id=uid(),source=source,name=name)
            .on_conflict_do_nothing(index_elements=['source','name']))
        stream=db.scalar(select(EventStream).where(EventStream.source==source,EventStream.name==name).with_for_update())
        receipt=db.scalar(select(EventPage).where(EventPage.stream_id==stream.id,EventPage.request_key==page.request_key))
        if receipt:
            if receipt.content_hash!=digest:
                raise IngestionConflict('La clave de página ya existe con otro contenido','page_key_reused')
            return {'page_id':receipt.id,'changed':receipt.changed_count,'version':receipt.checkpoint_version,'replayed':True}
        if stream.version!=page.expected_version or stream.cursor!=page.cursor:
            raise IngestionConflict('Checkpoint obsoleto; vuelve a leer el flujo','checkpoint_stale')
        changed=[]
        for record in page.events:
            values=record.model_dump();content_hash=fingerprint(values)
            event=db.scalar(select(SourceEvent).where(SourceEvent.stream_id==stream.id,SourceEvent.external_id==record.external_id))
            incoming_order=record.observation_sequence if record.time_basis=='snapshot' else record.source_updated_at
            stored_order=(event.observation_sequence if event.time_basis=='snapshot' else event.source_updated_at) if event else -1
            if event and event.time_basis!=record.time_basis:
                raise IngestionConflict('No se puede cambiar la base temporal','time_basis_changed')
            if event and incoming_order < stored_order:
                continue
            if event and incoming_order==stored_order:
                if event.content_hash!=content_hash:
                    raise IngestionConflict('Revisión contradictoria con la misma fecha de origen','revision_conflict')
                event.last_received_at=stamp
                continue
            if event and (event.occurred_at!=record.occurred_at or event.identity_kind!=record.identity_kind):
                raise IngestionConflict('La identidad del evento no puede cambiar','identity_changed')
            if event and record.time_basis=='snapshot' and all(getattr(event,k)==v for k,v in values.items() if k!='observation_sequence'):
                event.observation_sequence=record.observation_sequence;event.last_received_at=stamp;event.content_hash=content_hash
                continue
            if not event:
                event=SourceEvent(id=uid(),stream_id=stream.id,received_at=stamp)
                db.add(event)
            elif event.entity_id != record.entity_id:
                event.device_id=None
            for key,value in values.items():setattr(event,key,value)
            event.last_received_at=stamp;event.content_hash=content_hash
            changed.append(event.id)
        db.flush()
        stream.version+=1
        stream.coverage=page.coverage;stream.reason=page.reason
        stream.window_start=page.window_start;stream.window_end=page.window_end;stream.last_attempt_at=stamp
        if page.coverage in ('complete','partial'):
            stream.cursor=page.next_cursor;stream.last_success_at=stamp
        if page.coverage=='complete':
            # Never bridge a known gap by silently moving the completeness watermark.
            if stream.complete_through is None or page.window_start<=stream.complete_through:
                stream.complete_through=max(stream.complete_through or 0,page.window_end)
        if page.events:
            stream.last_event_at=max(stream.last_event_at or 0,max(e.occurred_at for e in page.events))
        receipt=EventPage(id=uid(),stream_id=stream.id,request_key=page.request_key,content_hash=digest,
            coverage=page.coverage,reason=page.reason,window_start=page.window_start,window_end=page.window_end,
            event_count=len(page.events),changed_count=len(changed),received_at=stamp,checkpoint_version=stream.version)
        db.add(receipt)
        if changed:
            enqueue(db,'event_index',{'event_ids':changed},'event-page:'+receipt.id)
        db.flush()
        return {'page_id':receipt.id,'changed':len(changed),'version':stream.version,'replayed':False}


def execute_index(job_id, lease_token):
    """Resolve exact inventory identities only; correlation/incident creation belongs to E4.2+."""
    with session() as db:
        job=db.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if not job or job.kind!='event_index' or job.status!='running' or job.lease_token!=lease_token or not job.lease_until or job.lease_until<=now():
            return False
        count=0
        for event in db.scalars(select(SourceEvent).where(SourceEvent.id.in_(job.payload['event_ids'])).with_for_update()):
            stream=db.get(EventStream,event.stream_id)
            identity=db.scalar(select(SourceIdentity).where(SourceIdentity.source==stream.source,SourceIdentity.external_id==event.entity_id)) if event.entity_id else None
            event.device_id=identity.device_id if identity else None
            count+=1
        if job.lease_until<=now():
            db.rollback();return False
        return finish(db,job_id,lease_token,True,{'indexed':count})
