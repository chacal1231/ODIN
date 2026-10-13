"""What is normal for each PON, by cause and hour of the day, learned from its history (PonSample).

For every PON, cause and local hour, the share of its ONUs that were down is averaged over each day's hour (weighted
by how long each value lasted) and the median over the days is kept: a real outage on a few days does not teach that
being down is normal. A value is trusted up to GAP after its sample (the reader stores one at least hourly); beyond
that the reader was stopped and the time counts as unknown. An hour of a day counts only with COVERAGE of it known.
Recomputed once a day (refresh), so the correlator reads a small table, not 90 days of samples.
cases/access.py uses it once a PON has MIN_DAYS days of an hour; before that, the rest of the OLT is the normal.
"""
import datetime as dt
import statistics
from zoneinfo import ZoneInfo
from sqlalchemy import delete, func, select
from ..config import settings
from ..models import PonBaseline, PonSample

CAUSES = ('los', 'power', 'offline')
MIN_DAYS = 14
GAP = 5400
COVERAGE = 1800
REFRESH = 86400


def hour_of(stamp, zone=None):
    return dt.datetime.fromtimestamp(stamp, zone or ZoneInfo(settings().timezone)).hour


def spans(start, end, zone):
    """(local date, hour, seconds) pieces of [start, end) cut at local hour boundaries."""
    while start < end:
        local = dt.datetime.fromtimestamp(start, zone)
        boundary = int((local.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).timestamp())
        piece = min(end, boundary)
        yield local.date(), local.hour, piece - start
        start = piece


def compute(rows, stamp, zone):
    """{(board, port, cause, hour): (median share, days)} from the samples of one OLT ordered by PON and time."""
    sums = {}  # (board, port, date, hour) -> [known seconds, {cause: share-seconds}]
    rows = list(rows)
    for i, row in enumerate(rows):
        following = rows[i + 1] if i + 1 < len(rows) and (rows[i + 1].board, rows[i + 1].port) == (row.board, row.port) else None
        end = min(following.at if following else stamp, row.at + GAP)
        if row.onus <= 0 or end <= row.at:continue
        for date, hour, seconds in spans(row.at, end, zone):
            slot = sums.setdefault((row.board, row.port, date, hour), [0, dict.fromkeys(CAUSES, 0.0)])
            slot[0] += seconds
            for cause in CAUSES:slot[1][cause] += seconds * (getattr(row, cause) or 0) / row.onus
    daily = {}  # (board, port, cause, hour) -> [share of each day]
    for (board, port, _, hour), (known, shares) in sums.items():
        if known < COVERAGE:continue
        for cause in CAUSES:daily.setdefault((board, port, cause, hour), []).append(shares[cause] / known)
    return {key: (statistics.median(values), len(values)) for key, values in daily.items()}


def refresh(db, stamp, force=False):
    """Recompute every PON's normal once a day."""
    last = db.scalar(select(func.max(PonBaseline.computed_at)))
    if not force and last and stamp - last < REFRESH:return False
    zone = ZoneInfo(settings().timezone)
    olts = list(db.scalars(select(PonSample.olt_id).distinct()))
    db.execute(delete(PonBaseline))
    for olt_id in olts:
        rows = db.scalars(select(PonSample).where(PonSample.olt_id == olt_id).order_by(PonSample.board, PonSample.port, PonSample.at))
        for (board, port, cause, hour), (rate, days) in compute(rows, stamp, zone).items():
            db.add(PonBaseline(olt_id=olt_id, board=board, port=port, cause=cause, hour=hour, rate=rate, days=days, computed_at=stamp))
    return True


def normal(db, olt_id, stamp):
    """{(board, port, cause): (rate, days)} for the local hour of `stamp`, only where there are MIN_DAYS days."""
    hour = hour_of(stamp)
    return {(b.board, b.port, b.cause): (b.rate, b.days) for b in db.scalars(select(PonBaseline).where(
        PonBaseline.olt_id == olt_id, PonBaseline.hour == hour, PonBaseline.days >= MIN_DAYS))}
