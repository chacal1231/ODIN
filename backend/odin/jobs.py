import random
from sqlalchemy import select, or_, and_, update
from sqlalchemy.dialects.postgresql import insert
from .models import Job, now, uid

MAX_ATTEMPTS = 4


def enqueue(db, kind, payload, key):
    job_id = uid()
    db.execute(insert(Job).values(id=job_id, kind=kind, payload=payload, idempotency_key=key,
                                 status='pending', run_after=now(), attempts=0)
               .on_conflict_do_nothing(index_elements=['idempotency_key']))
    return db.scalar(select(Job).where(Job.idempotency_key == key))


def claim(db, kinds=None, exclude=()):
    """Next due job; `kinds`/`exclude` let a dedicated process (the analyst) own slow job kinds."""
    stamp = now()
    scope = [Job.kind.in_(kinds)] if kinds else [Job.kind.not_in(exclude)] if exclude else []
    db.execute(update(Job).where(Job.status == 'running', Job.lease_until <= stamp,
                                 Job.attempts >= MAX_ATTEMPTS, *scope).values(status='failed', lease_token=None))
    job = db.scalar(select(Job).where(Job.attempts < MAX_ATTEMPTS, *scope, or_(
        and_(Job.status == 'pending', Job.run_after <= stamp),
        and_(Job.status == 'running', Job.lease_until <= stamp)
    )).order_by(Job.run_after, Job.id).with_for_update(skip_locked=True).limit(1))
    if job:
        job.status = 'running'
        job.attempts += 1
        job.lease_until = stamp + 60
        job.lease_token = uid()
    db.commit()
    return job


def heartbeat(db, job_id, lease_token):
    result = db.execute(update(Job).where(Job.id == job_id, Job.status == 'running',
        Job.lease_token == lease_token, Job.lease_until > now()).values(lease_until=now() + 60))
    db.commit()
    return result.rowcount == 1


def finish(db, job_id, lease_token, success, result=None):
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.status != 'running' or job.lease_token != lease_token or job.lease_until <= now():
        db.rollback()
        return False
    job.status = 'done' if success else ('failed' if job.attempts >= MAX_ATTEMPTS else 'pending')
    job.result = result
    job.run_after = now() + min(300, 2 ** job.attempts) + random.randint(0, 3)
    job.lease_token = None
    job.lease_until = None
    db.commit()
    return True
