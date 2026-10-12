"""Which cases alert in the status bar, and the calibration that supports that choice.

A case alerts while it is active (a case without changes for a day is «stale»: it stays listed in Cases, but a
permanent red count in the status bar would only teach people to ignore it) and:
- sessions and interfaces: its neighbor or interface is a transit, a transport or an internal link
  (read from the router port descriptions), or the drop is sustained (critical by the correlator rules);
- latency of the own network: critical;
- access (OLT and PON): critical by the access rules (OLT unreachable, uplink down, a common fiber cut or many customers);
- external services: when ODIN's verdict blames the own network or one exit (probes/verdict.py); a service that
  fails alone through a healthy exit is shown, not alerted. Without a verdict, when EXTERNAL_TOGETHER or more
  services are degraded at once.
Alerts stay off (shadow mode) until an administrator enables them.
"""
import datetime as dt
from collections import Counter
from sqlalchemy import select, func, or_
from ..models import Analysis, AnalysisReview, Case, CorrelationState, now
from ..inventory.roles import critical
from . import correlator

EXTERNAL_TOGETHER = 3
DAYS = 7
GROUPS = {'vital': 'Tránsito, transporte o enlace interno', 'sustained': 'Otras caídas sostenidas', 'latency': 'Latencia crítica de red propia',
          'external': 'Servicios externos: falla propia o de una salida', 'access': 'Caídas de acceso críticas (OLT, uplink o corte de fibra)'}


def external(case):
    return case.kind == 'latency' and case.detail.get('scope') == 'external'


def group(case, together):
    """Why a case alerts (a key of GROUPS), or None when it does not. `together`: distinct external services overlapping it."""
    if case.kind in ('bgp_session', 'interface'):
        if critical(case.detail.get('neighbor') or case.detail.get('port')):return 'vital'
        return 'sustained' if case.severity == 'critical' else None
    if case.kind == 'access':
        return 'access' if case.severity == 'critical' else None
    if external(case):
        verdict = (case.detail.get('verdict') or {}).get('state')
        if verdict in ('own', 'exit'):return 'external'
        if verdict == 'service':return None
        return 'external' if together >= EXTERNAL_TOGETHER else None
    return 'latency' if case.severity == 'critical' else None


def overlapping_services(cases):
    """For each external case, the distinct external services degraded at some moment of its period."""
    rows = [c for c in cases if external(c)]
    end = lambda c: c.closed_at or c.last_activity_at
    return {c.id: len({o.subject for o in rows if o.opened_at <= end(c) and end(o) >= c.opened_at}) for c in rows}


def alerting(db):
    """Active cases that alert now."""
    rows = list(db.scalars(select(Case).where(Case.state == 'active')))
    together = overlapping_services(rows)
    return [c for c in rows if group(c, together.get(c.id, 0))]


def summary(db):
    state = db.get(CorrelationState, 'cases')
    if not state or not state.alerts_enabled:
        return {'alerts_enabled': False}
    rows = alerting(db)
    return {'alerts_enabled': True, 'active': len(rows), 'critical': sum(c.severity == 'critical' for c in rows),
            'unacknowledged': sum(c.acknowledged_at is None for c in rows)}


def calibration(db, zone='America/Bogota'):
    """Cases opened per day over the last DAYS days, by whether and why they would alert; NOC reviews; thresholds in use."""
    from zoneinfo import ZoneInfo
    since = now() - DAYS * 86400
    rows = list(db.scalars(select(Case).where(Case.opened_at >= since)))
    # External cases still open before the window also count as simultaneous.
    context = rows + list(db.scalars(select(Case).where(Case.opened_at < since, Case.kind == 'latency',
        or_(Case.closed_at.is_(None), Case.closed_at >= since))))
    together = overlapping_services(context)
    days = {}
    for case in rows:
        day = dt.datetime.fromtimestamp(case.opened_at, ZoneInfo(zone)).strftime('%Y-%m-%d')
        reason = group(case, together.get(case.id, 0))
        days.setdefault(day, Counter())[reason or ('quiet_external' if external(case) else 'quiet')] += 1
    noisy = Counter(f'{c.subject} · {c.entity_label}' for c in rows if not group(c, together.get(c.id, 0))).most_common(8)
    reviews = dict(db.execute(select(AnalysisReview.verdict, func.count()).group_by(AnalysisReview.verdict)).all())
    analysed = db.scalar(select(func.count()).select_from(Analysis).where(Analysis.kind == 'case', Analysis.status == 'done'))
    return {'days': [{'day': d, **counts, 'alerts': sum(counts[g] for g in GROUPS)} for d, counts in sorted(days.items())],
            'groups': GROUPS, 'noisy': [{'subject': s, 'cases': n} for s, n in noisy],
            'reviews': {'analyses': analysed, **reviews}, 'alerts_enabled': bool((db.get(CorrelationState, 'cases') or CorrelationState()).alerts_enabled),
            'thresholds': {'quiet_close_minutes': correlator.QUIET // 60, 'stale_hours': correlator.STALE // 3600,
                           'sustained_minutes': correlator.SUSTAINED // 60, 'flap_critical': correlator.FLAP_CRITICAL,
                           'external_together': EXTERNAL_TOGETHER}}
