from concurrent.futures import ThreadPoolExecutor
from odin.db import session
from odin.jobs import enqueue, claim, finish, heartbeat
from odin.models import Job, now


def test_idempotency_and_lease_fencing():
    with session() as db:
        first=enqueue(db,'healthcheck',{},'same')
        assert enqueue(db,'healthcheck',{},'same').id == first.id
        db.commit()
        job=claim(db)
        old=job.lease_token
        job.lease_until=now()-1
        db.commit()
        again=claim(db)
        assert again.id == job.id and again.lease_token != old
        assert not finish(db,job.id,old,True)
        assert heartbeat(db,job.id,again.lease_token)
        assert finish(db,job.id,again.lease_token,True,{'ok':True})
        assert claim(db) is None


def test_concurrent_workers_and_retry_limit():
    with session() as db:
        for i in range(4):
            enqueue(db,'healthcheck',{},str(i))
        db.commit()
    def take():
        with session() as db:
            job=claim(db)
            return job.id if job else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(lambda _:take(),range(4)))
    assert len(set(ids)) == 4 and None not in ids
    with session() as db:
        job=db.get(Job,ids[0])
        job.attempts=4
        job.lease_until=now()-1
        db.commit()
        claim(db)
        db.refresh(job)
        assert job.status=='failed'
