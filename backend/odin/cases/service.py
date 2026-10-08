"""Correlator scheduling on the job queue: every minute, sooner while history is pending."""
import logging
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from ..db import session
from ..models import CorrelationState, Job, now, uid
from ..jobs import enqueue
from .correlator import run

INTERVAL = 60


def schedule(db):
    stamp = now()
    db.execute(insert(CorrelationState).values(id='cases', next_due=stamp).on_conflict_do_nothing(index_elements=['id']))
    state = db.scalar(select(CorrelationState).where(CorrelationState.id == 'cases').with_for_update())
    previous = db.get(Job, state.job_id) if state.job_id else None
    if previous and previous.status in ('pending', 'running'):
        db.commit();return
    if state.next_due <= stamp:
        state.job_id = enqueue(db, 'correlate', {}, 'correlate:' + uid()).id
    db.commit()


def execute(job_id, token):
    with session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status != 'running' or job.lease_token != token or job.lease_until <= now():return
        # One pass at a time even if a lease expires during a long first run.
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(712010)')):
            job.status = 'done';job.lease_token = None;job.lease_until = None;job.result = {'status': 'skipped'};db.commit();return
        error = None;more = False
        try:
            with db.begin_nested():more = run(db)
        except Exception:
            logging.exception('correlation_failed');error = 'correlation_failed'
        if not error:
            try:
                with db.begin_nested():
                    from ..analyst.service import auto_analyze
                    auto_analyze(db)
            except Exception:
                logging.exception('auto_analysis_failed')
        state = db.get(CorrelationState, 'cases');stamp = now()
        state.last_run_at = stamp
        if error:state.last_error = error
        state.next_due = stamp + (2 if more and not error else INTERVAL)
        job.status = 'done';job.result = {'status': 'failed' if error else 'done', 'error': error, 'more': more}
        job.lease_token = None;job.lease_until = None
        db.commit()
