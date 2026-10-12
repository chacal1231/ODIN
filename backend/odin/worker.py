import logging
import signal
import threading
from sqlalchemy import delete
from .db import session
from .jobs import claim, finish
from .models import RateBucket, Challenge, AuthSession, now
from .inventory.service import schedule, execute
from .events.service import execute_index
from .events.collector import schedule as schedule_events, execute as execute_events
from .cases.service import schedule as schedule_cases, execute as execute_cases
from .analyst.service import KINDS as ANALYST_KINDS
from .olts.service import schedule as schedule_olts, execute as execute_olts
from .probes.baseline import schedule as schedule_baselines, execute as execute_baselines

stop = threading.Event()


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)  # request URLs may carry secrets (Telegram's bot token)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            with session() as db:
                db.execute(delete(RateBucket).where(RateBucket.expires_at < now()))
                db.execute(delete(Challenge).where(Challenge.expires_at < now()))
                db.execute(delete(AuthSession).where(AuthSession.expires_at < now()))
                db.commit()
                schedule(db)
                schedule_events(db)
                schedule_cases(db)
                schedule_olts(db)
                schedule_baselines(db)
                # Model runs take minutes: the analyst process owns them.
                job = claim(db, exclude=ANALYST_KINDS)
                if job:
                    if job.kind == 'inventory':
                        execute(job.id, job.lease_token)
                    elif job.kind == 'event_poll':
                        execute_events(job.id, job.lease_token)
                    elif job.kind == 'correlate':
                        execute_cases(job.id, job.lease_token)
                    elif job.kind == 'smartolt_poll':
                        execute_olts(job.id, job.lease_token)
                    elif job.kind == 'probe_baselines':
                        execute_baselines(job.id, job.lease_token)
                    elif job.kind == 'event_index':
                        execute_index(job.id, job.lease_token)
                    elif job.kind == 'healthcheck':
                        finish(db, job.id, job.lease_token, True, {'status': 'ok'})
                    else:
                        finish(db, job.id, job.lease_token, False, {'error': 'unsupported_job'})
        except Exception:
            logging.error('worker_cycle_failed')
        stop.wait(2)


if __name__ == '__main__':
    main()
