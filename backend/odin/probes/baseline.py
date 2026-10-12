"""Adaptive thresholds: each check's normal behaviour from its own history, and the thresholds it proposes.

Baseline: the p95 of every 5-minute window of the last 7 days (every address of the check, same protocol), leaving out
windows of an incident or a maintenance. Its median is the base and the median absolute deviation (MAD) its spread, so
isolated spikes do not drag it. Each threshold is the largest of a ratio, a margin and a spread term over the base.
Brakes: an applied threshold moves at most 25 % per step (a slow degradation keeps alerting instead of becoming
normal), a category ceiling, and loss is never adapted (a healthy path loses nothing; a lossy base is flagged).
Timing: recomputed daily with all the history available; a proposal needs 24 h (one evening peak), automatic
application 7 days with 80 % coverage and happens at most weekly. A flapping check gets extra persistence for a day.
Thresholds are overlaid on the series configuration by the detector: applying them never restarts a series.
"""
import logging
import math
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, delete, func, or_, tuple_
from ..config import settings
from ..db import session
from ..jobs import enqueue, heartbeat, finish
from ..models import ProbeTarget, ProbeSeries, ProbeRound, ProbeIncident, ProbeMaintenance, ProbeBaseline, Job, now, uid
from ..security import audit

WINDOW = 300
HISTORY = 7 * 86400
PROPOSAL_DAYS = 1
READY_DAYS = 7
COVERAGE = 0.8
RECOMPUTE = 86400
APPLY_EVERY = 7 * 86400
SLACK = 600  # an hourly job must not drift a daily or weekly step by one run
MAX_STEP = 0.25
# (warning ratio, warning margin ms, critical ratio, critical margin ms); HTTPS times include TLS and the response.
PROFILES = {'icmp': (1.5, 20, 2.5, 50), 'tcp': (1.5, 20, 2.5, 50), 'https': (2, 50, 4, 150)}
SPREAD = (5, 10)  # normalised MADs over the base for warning and critical
JITTER = (3, 5, 6, 15)
CEILINGS = {'juegos': 150}  # critical latency a category never needs above
PEAK, VALLEY = range(19, 23), range(3, 6)
FLAP_WINDOW, FLAP_SHORT, FLAP_COUNT, FLAP_EXTRA = 86400, 900, 4, 2
KEEP = 366 * 86400
THRESHOLDS = ('rtt_warning', 'rtt_critical', 'jitter_warning', 'jitter_critical')


def p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def summarize(rounds, interval, stamp, zone='UTC'):
    """rounds: (address, slot, samples) of valid rounds. None without a single usable window."""
    groups = {}
    for address, slot, samples in rounds:
        g = groups.setdefault((address, slot // WINDOW), {'slot': slot // WINDOW * WINDOW, 'rounds': 0, 'samples': [], 'steps': [], 'sent': 0})
        values = [x for x in samples if x is not None]
        g['rounds'] += 1;g['sent'] += len(samples);g['samples'] += values
        # Jitter pairs stay within one round and break at a lost packet, as in the detector.
        g['steps'] += [abs(b - a) for a, b in zip(samples, samples[1:]) if a is not None and b is not None]
    minimum = max(1, WINDOW // interval // 2)
    windows = [g for g in groups.values() if g['rounds'] >= minimum and g['samples']]
    if not windows:return None
    values = [p95(w['samples']) for w in windows]
    base = statistics.median(values)
    mad = statistics.median(abs(v - base) for v in values)
    jitters = [sum(w['steps']) / len(w['steps']) for w in windows if w['steps']]
    sent = sum(g['sent'] for g in groups.values());received = sum(len(g['samples']) for g in groups.values())
    tz = ZoneInfo(zone)
    hours = [datetime.fromtimestamp(w['slot'], tz).hour for w in windows]
    peak = [v for v, h in zip(values, hours) if h in PEAK];valley = [v for v, h in zip(values, hours) if h in VALLEY]
    first = min(w['slot'] for w in windows)
    # Coverage per moment, not per address: a hostname rotating addresses is still covered.
    spanned = {w['slot'] for w in windows}
    expected = max(1, (stamp - first) // WINDOW)
    return {'base_p95': round(base, 2), 'mad': round(mad, 2), 'jitter_base': round(statistics.median(jitters), 2) if jitters else None,
            'loss_percent': round(100 * (sent - received) / sent, 2) if sent else None,
            'peak_p95': round(statistics.median(peak), 2) if peak else None, 'valley_p95': round(statistics.median(valley), 2) if valley else None,
            'addresses': len({a for a, _ in groups}), 'windows': len(windows), 'first': first,
            'days': round(min(READY_DAYS, (stamp - first) / 86400), 2), 'coverage': round(min(1, len(spanned) / expected), 3)}


def propose(metrics, protocol, categories, applied, loss_warning):
    """Thresholds for a baseline; `applied` limits each step. Returns (proposal, flags)."""
    wr, wf, cr, cf = PROFILES[protocol]
    base, spread = metrics['base_p95'], 1.4826 * metrics['mad']
    warning = max(base * wr, base + wf, base + SPREAD[0] * spread)
    critical = max(base * cr, base + cf, base + SPREAD[1] * spread)
    flags = []
    ceiling = min((CEILINGS[c] for c in categories if c in CEILINGS), default=None)
    if ceiling and critical > ceiling:
        # A ceiling below the warning would alert all the time: the destination itself is slow, report it.
        if ceiling > warning:critical = ceiling
        else:flags.append('above_ceiling')
    proposal = {'rtt_warning': warning, 'rtt_critical': critical}
    if metrics['jitter_base'] is not None:
        jb = metrics['jitter_base'];jr, jf, jcr, jcf = JITTER
        proposal.update(jitter_warning=max(jb * jr, jb + jf), jitter_critical=max(jb * jcr, jb + jcf))
    for key, value in list(proposal.items()):
        previous = (applied or {}).get(key)
        if previous:
            limited = min(max(value, previous * (1 - MAX_STEP)), previous * (1 + MAX_STEP))
            if limited != value and 'step_limited' not in flags:flags.append('step_limited')
            proposal[key] = limited
    for low, high in (('rtt_warning', 'rtt_critical'), ('jitter_warning', 'jitter_critical')):
        if low in proposal and proposal[high] <= proposal[low]:proposal[high] = proposal[low] * 1.25
    if metrics['loss_percent'] is not None and metrics['loss_percent'] >= loss_warning:flags.append('lossy')
    return {k: round(v, 1) for k, v in proposal.items()}, flags


def overlay(target, config, slot):
    """The configuration the detector evaluates: the series' own, with the applied adaptive thresholds and damping."""
    if target.threshold_mode == 'manual':return config
    adaptive = target.adaptive or {};result = dict(config);protocol = config.get('protocol', 'icmp')
    thresholds = adaptive.get('thresholds') if adaptive.get('protocol') == protocol else None
    if thresholds and thresholds.get('rtt_warning') is not None:
        result.update(rtt_warning=thresholds['rtt_warning'], rtt_critical=thresholds['rtt_critical'])
    if thresholds and thresholds.get('jitter_warning') is not None and protocol == 'icmp' and config['packets'] >= 2:
        result.update(jitter_warning=thresholds['jitter_warning'], jitter_critical=thresholds['jitter_critical'])
    damping = adaptive.get('damping')
    if damping and damping['until'] > slot:result['persistence'] = max(result['persistence'], damping['persistence'])
    return result


def compute(db, target, stamp):
    since = stamp - HISTORY
    series = {s.id: s.address for s in db.scalars(select(ProbeSeries).where(ProbeSeries.target_id == target.id,
        or_(ProbeSeries.ended_at.is_(None), ProbeSeries.ended_at > since))) if (s.config or {}).get('protocol', 'icmp') == target.protocol}
    # Faults and maintenance are not normal behaviour.
    incidents = [(i.address, i.opened_at, i.closed_at or stamp) for i in db.scalars(select(ProbeIncident).where(
        ProbeIncident.target_id == target.id, or_(ProbeIncident.closed_at.is_(None), ProbeIncident.closed_at > since)))]
    maintenance = [(m.starts_at, m.ends_at) for m in db.scalars(select(ProbeMaintenance).where(ProbeMaintenance.target_id == target.id,
        ProbeMaintenance.cancelled.is_(False), ProbeMaintenance.ends_at > since))]
    rows = db.execute(select(ProbeRound.series_id, ProbeRound.slot, ProbeRound.samples).where(ProbeRound.series_id.in_(list(series)),
        ProbeRound.status == 'valid', ProbeRound.slot > since, ProbeRound.slot <= stamp)
        .execution_options(yield_per=5000)) if series else []
    rounds = ((series[sid], slot, samples) for sid, slot, samples in rows
              if not any(a == series[sid] and o <= slot <= c for a, o, c in incidents) and not any(s <= slot < e for s, e in maintenance))
    metrics = summarize(rounds, target.effective['interval_seconds'], stamp, settings().timezone)
    row = ProbeBaseline(id=uid(), target_id=target.id, computed_at=stamp, protocol=target.protocol, status='insufficient',
                        days=0, coverage=0, windows=0, metrics=metrics or {}, proposal={}, flags=[])
    if metrics:
        row.days, row.coverage, row.windows = metrics['days'], metrics['coverage'], metrics['windows']
        if metrics['days'] >= PROPOSAL_DAYS:
            applied = (target.adaptive or {}).get('thresholds') if (target.adaptive or {}).get('protocol') == target.protocol else None
            row.proposal, row.flags = propose(metrics, target.protocol, target.categories or [], applied, target.effective['loss_warning'])
            row.status = 'ready' if metrics['days'] >= READY_DAYS and metrics['coverage'] >= COVERAGE else 'provisional'
    db.add(row)
    return row


def apply(db, target, baseline, actor=None, stamp=None):
    if baseline.status == 'insufficient' or not baseline.proposal or baseline.target_id != target.id:
        raise ValueError('La línea base no tiene umbrales propuestos')
    stamp = stamp or now()
    target.adaptive = {**(target.adaptive or {}), 'thresholds': dict(baseline.proposal), 'protocol': baseline.protocol,
                       'baseline_id': baseline.id, 'applied_at': stamp, 'applied_by': actor or 'auto'}
    baseline.applied_at = stamp;baseline.applied_by = actor or 'auto'
    audit(db, 'probe_thresholds_applied', actor, target.id)


def damp(target, short_incidents, stamp):
    adaptive = dict(target.adaptive or {})
    if short_incidents >= FLAP_COUNT:
        adaptive['damping'] = {'persistence': min(10, target.effective['persistence'] + FLAP_EXTRA), 'until': stamp + FLAP_WINDOW,
                               'short_incidents': short_incidents}
    elif adaptive.get('damping') and adaptive['damping']['until'] <= stamp:
        del adaptive['damping']
    if adaptive != (target.adaptive or {}):target.adaptive = adaptive


def latest(db, ids):
    newest = select(ProbeBaseline.target_id, func.max(ProbeBaseline.computed_at)).where(
        ProbeBaseline.target_id.in_(list(ids))).group_by(ProbeBaseline.target_id)
    rows = db.scalars(select(ProbeBaseline).where(tuple_(ProbeBaseline.target_id, ProbeBaseline.computed_at).in_(newest)))
    return {row.target_id: row for row in rows}


def run(db, stamp, force=False, beat=lambda: None):
    ids = list(db.scalars(select(ProbeTarget.id).where(ProbeTarget.enabled.is_(True), ProbeTarget.archived.is_(False))))
    counts = {'computed': 0, 'applied': 0, 'damped': 0}
    for target_id in ids:
        target = db.scalar(select(ProbeTarget).where(ProbeTarget.id == target_id).with_for_update())
        if not target or not target.enabled or target.archived:db.commit();continue
        current = latest(db, [target.id]).get(target.id)
        if force or not current or current.computed_at <= stamp - RECOMPUTE + SLACK:
            current = compute(db, target, stamp);counts['computed'] += 1
        applied_at = (target.adaptive or {}).get('applied_at')
        if (target.threshold_mode == 'auto' and current.status == 'ready' and not current.applied_at
                and (not applied_at or applied_at <= stamp - APPLY_EVERY + SLACK)):
            apply(db, target, current, stamp=stamp);counts['applied'] += 1
        short = db.scalar(select(func.count()).select_from(ProbeIncident).where(ProbeIncident.target_id == target.id,
            ProbeIncident.closed_at > stamp - FLAP_WINDOW, ProbeIncident.closed_at - ProbeIncident.opened_at < FLAP_SHORT))
        damp(target, short, stamp)
        counts['damped'] += bool((target.adaptive or {}).get('damping'))
        db.commit();beat()
    db.execute(delete(ProbeBaseline).where(ProbeBaseline.computed_at < stamp - KEEP, ProbeBaseline.applied_at.is_(None)))
    db.commit()
    return counts


def schedule(db):
    """Hourly: daily baselines, weekly automatic steps and flap damping run in the same pass."""
    enqueue(db, 'probe_baselines', {}, 'probe_baselines:%d' % (now() // 3600))
    db.commit()


def request(db):
    """On demand: recompute every baseline with the history available now."""
    return enqueue(db, 'probe_baselines', {'force': True}, 'probe_baselines:manual:' + uid())


def execute(job_id, token):
    with session() as db:
        job = db.get(Job, job_id);force = bool((job.payload or {}).get('force')) if job else False
        try:
            counts = run(db, now(), force, lambda: heartbeat(db, job_id, token))
            finish(db, job_id, token, True, counts)
        except Exception:
            logging.exception('probe_baselines_failed');db.rollback()
            finish(db, job_id, token, False, {'error': 'probe_baselines_failed'})
