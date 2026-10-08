"""Analyst process: runs model jobs one at a time, apart from the main worker.

A run takes minutes; in the main worker it would hold back the correlator and the readers.
"""
import logging
import signal
import threading
from ..db import session
from ..jobs import claim
from ..models import now
from .service import KINDS, execute, state

stop = threading.Event()


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)  # request URLs may carry secrets (Telegram's bot token)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            with session() as db:
                state(db).heartbeat_at = now();db.commit()
                job = claim(db, kinds=KINDS)
                if job:execute(job.id, job.lease_token)
        except Exception:
            logging.error('analyst_cycle_failed')
        stop.wait(2)


if __name__ == '__main__':
    main()
