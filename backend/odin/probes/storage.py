"""Daily raw partitions and weighted 5-minute/hourly aggregates."""
import math
import re
from datetime import datetime,timezone
from sqlalchemy import select, text, delete, func
from sqlalchemy.dialects.postgresql import insert
from ..models import ProbeSeries, ProbeRound, ProbeRollup, ProbeOrigin, ProbeBatch, ProbeIncident, ProbeIncidentEvent


def expected_slots(series,start,end):
    start=max(start,series.started_at)
    end=min(end,series.ended_at or end)
    interval=series.config['interval_seconds']
    first=series.started_at+max(0,math.ceil((start-series.started_at)/interval))*interval
    return max(0,math.ceil((end-first)/interval))


def aggregate_bucket(db,start):
    end=start+300
    series=list(db.scalars(select(ProbeSeries).where(ProbeSeries.started_at<end,
        (ProbeSeries.ended_at.is_(None))|(ProbeSeries.ended_at>start))))
    groups={}
    for row in db.scalars(select(ProbeRound).where(ProbeRound.slot>=start,ProbeRound.slot<end,ProbeRound.status=='valid')):
        groups.setdefault(row.series_id,[]).append(row)
    for item in series:
        rows=groups.get(item.id,[]);values=[x for r in rows for x in r.samples if x is not None]
        data={'series_id':item.id,'bucket':start,'resolution':300,'expected':expected_slots(item,start,end),
            'valid':len(rows),'sent':sum(r.sent for r in rows),'received':sum(r.received for r in rows),
            'rtt_sum':sum(values),'rtt_min':min(values) if values else None,'rtt_max':max(values) if values else None}
        statement=insert(ProbeRollup).values(**data)
        db.execute(statement.on_conflict_do_update(index_elements=['series_id','bucket','resolution'],set_=data))


def aggregate_hour(db,start):
    rows=db.execute(select(ProbeRollup.series_id,func.sum(ProbeRollup.expected),func.sum(ProbeRollup.valid),
        func.sum(ProbeRollup.sent),func.sum(ProbeRollup.received),func.sum(ProbeRollup.rtt_sum),
        func.min(ProbeRollup.rtt_min),func.max(ProbeRollup.rtt_max)).where(
        ProbeRollup.resolution==300,ProbeRollup.bucket>=start,ProbeRollup.bucket<start+3600).group_by(ProbeRollup.series_id)).all()
    for row in rows:
        data=dict(zip(['series_id','expected','valid','sent','received','rtt_sum','rtt_min','rtt_max'],row))
        data.update(bucket=start,resolution=3600)
        statement=insert(ProbeRollup).values(**data)
        db.execute(statement.on_conflict_do_update(index_elements=['series_id','bucket','resolution'],set_=data))


def upkeep(db,stamp):
    if not db.scalar(text('SELECT pg_try_advisory_xact_lock(712004)')):db.rollback();return
    day=stamp//86400*86400
    for start in (day-86400,day,day+86400,day+172800):
        suffix=datetime.fromtimestamp(start,timezone.utc).strftime('%Y%m%d')
        db.execute(text(f'CREATE TABLE IF NOT EXISTS probe_rounds_d{suffix} PARTITION OF probe_rounds FOR VALUES FROM ({start}) TO ({start+86400})'))
    origin=db.scalar(select(ProbeOrigin).where(ProbeOrigin.id=='odin').with_for_update())
    if not origin:origin=ProbeOrigin(id='odin',heartbeat=stamp);db.add(origin);db.flush()
    if origin.rollup_until is None:
        first=db.scalar(select(func.min(ProbeSeries.started_at)))
        origin.rollup_until=(first or stamp)//300*300
    if origin.hourly_until is None:origin.hourly_until=origin.rollup_until//3600*3600
    # Keep transactions short; catch up incrementally after a restart.
    for _ in range(12):
        if origin.rollup_until+300>stamp//300*300-300:break
        aggregate_bucket(db,origin.rollup_until);origin.rollup_until+=300
    for _ in range(2):
        if origin.hourly_until+3600>origin.rollup_until:break
        aggregate_hour(db,origin.hourly_until);origin.hourly_until+=3600
    if not origin.retention_at or origin.retention_at<stamp-3600:
        partitions=db.scalars(text("SELECT child.relname FROM pg_inherits JOIN pg_class parent ON parent.oid=inhparent JOIN pg_class child ON child.oid=inhrelid WHERE parent.relname='probe_rounds'")).all()
        for name in partitions:
            if not re.fullmatch(r'probe_rounds_d\d{8}',name):continue
            start=int(datetime.strptime(name[-8:],'%Y%m%d').replace(tzinfo=timezone.utc).timestamp())
            if start+86400<=min(day-7*86400,origin.rollup_until):db.execute(text(f'DROP TABLE {name}'))
        db.execute(delete(ProbeRollup).where(ProbeRollup.resolution==300,ProbeRollup.bucket<min(stamp-90*86400,origin.hourly_until)))
        db.execute(delete(ProbeRollup).where(ProbeRollup.resolution==3600,ProbeRollup.bucket<stamp-366*86400))
        # Scheduled results live on in probe_rounds; only manual runs are shown from batches.
        finished=ProbeBatch.status.in_(['done','failed','cancelled'])
        db.execute(delete(ProbeBatch).where(finished,ProbeBatch.mode=='scheduled',ProbeBatch.finished_at<stamp-2*86400))
        db.execute(delete(ProbeBatch).where(finished,ProbeBatch.finished_at<stamp-14*86400))
        old=select(ProbeIncident.id).where(ProbeIncident.closed_at<stamp-366*86400)
        db.execute(delete(ProbeIncidentEvent).where(ProbeIncidentEvent.incident_id.in_(old)))
        db.execute(delete(ProbeIncident).where(ProbeIncident.closed_at<stamp-366*86400))
        origin.retention_at=stamp
    db.commit()
