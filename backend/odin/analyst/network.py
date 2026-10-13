"""Measurements for the chat: latency to the destinations ODIN probes (D) and the state of access (O).

Events alone cannot answer «what latency do we have to the games?»: a destination without incidents would not
appear. These lines give the numbers of the period from ODIN's own stores, grouped so a category reads in one line
and each destination in another. Latency is the mean of the period measured from ODIN's server; for HTTPS and TCP
checks it is the response time of the check (it includes the connection) and they count failures, not packet loss.
"""
from collections import Counter
from sqlalchemy import select, func, or_
from ..models import (MonitoringDestination, Olt, OltOutage, OltPonPort, OltUplink, Onu, ProbeIncident, ProbeRollup, ProbeSeries, ProbeState,
                      ProbeTarget)
from .packs import Line, stamp_text, span_text

MAX_DESTINATIONS = 40
MAX_OUTAGES = 20
# probes/detector.py
STATES = {'healthy': 'normal', 'warning': 'degradado', 'critical': 'crítico', 'warming': 'calentando', 'maintenance': 'en mantenimiento',
          'no_data': 'sin datos'}
CAUSES = {'los': 'corte de fibra (LOS)', 'power': 'falla de energía', 'offline': 'sin conexión'}


def ms(value):
    return f'{value:.1f} ms'.replace('.', ',') if value is not None else '—'


def pct(value):
    return f'{value:.1f} %'.replace('.', ',') if value is not None else '—'


def chosen(targets, plan):
    """The destinations the plan names (by category, service or name); all of them when it names none."""
    cats, services, names = set(plan.get('categorias', [])), set(plan.get('servicios', [])), set(plan.get('destinos', []))
    if not (cats or services or names):return targets
    return [t for t in targets if t.name in names or t.service in services or cats & set(t.categories or [])]


def period_stats(db, ids, start, end):
    """Per destination over the period: sent, received, sum and max of RTT, from 5-minute or hourly aggregates."""
    resolution = 300 if end - start <= 2 * 86400 else 3600
    rows = db.execute(select(ProbeSeries.target_id, func.sum(ProbeRollup.sent), func.sum(ProbeRollup.received), func.sum(ProbeRollup.rtt_sum),
                             func.max(ProbeRollup.rtt_max), func.sum(ProbeRollup.valid))
                      .join(ProbeSeries, ProbeSeries.id == ProbeRollup.series_id)
                      .where(ProbeSeries.target_id.in_(ids), ProbeRollup.resolution == resolution,
                             ProbeRollup.bucket >= start // resolution * resolution, ProbeRollup.bucket <= end)
                      .group_by(ProbeSeries.target_id)).all() if ids else []
    return {t: {'sent': s or 0, 'received': r or 0, 'mean': (total / r) if r else None, 'max': top, 'valid': v or 0}
            for t, s, r, total, top, v in rows}


def latency_lines(db, start, end, plan, priority=0, detail=True, scope=None):
    """`scope` (chat.pop_scope): only the destinations of that POP or of its equipment."""
    query = select(ProbeTarget).where(ProbeTarget.enabled.is_(True)).order_by(ProbeTarget.name)
    if scope:
        query = query.outerjoin(MonitoringDestination, MonitoringDestination.id == ProbeTarget.destination_id).where(
            or_(ProbeTarget.pop_id == scope['pop'], MonitoringDestination.device_id.in_(scope['devices'])))
    targets = list(db.scalars(query))
    targets = chosen(targets, plan)
    if not targets:return []
    ids = [t.id for t in targets]
    stats = period_stats(db, ids, start, end)
    current = {}
    for state in db.scalars(select(ProbeState).where(ProbeState.target_id.in_(ids))):current[state.target_id] = state.metrics or {}
    incidents = Counter(db.scalars(select(ProbeIncident.target_id).where(ProbeIncident.target_id.in_(ids), ProbeIncident.opened_at <= end,
                                                                           (ProbeIncident.closed_at.is_(None)) | (ProbeIncident.closed_at >= start))))
    lines, n = [], 0

    def failure(t):
        s = stats.get(t.id)
        return 100 * (s['sent'] - s['received']) / s['sent'] if s and s['sent'] else None

    def label(t):
        return 'pérdida' if t.protocol == 'icmp' else 'fallos'

    # One line per category the destinations belong to.
    names = set(plan.get('categorias', [])) or {c for t in targets for c in (t.categories or [])}
    for category in sorted(names):
        group = [t for t in targets if category in (t.categories or [])]
        measured = [t for t in group if stats.get(t.id, {}).get('mean') is not None]
        if not group:continue
        n += 1
        means = [stats[t.id]['mean'] for t in measured]
        worst = max(measured, key=lambda t: stats[t.id]['mean'], default=None)
        lossy = [(t, failure(t)) for t in group if (failure(t) or 0) > 0]
        states = Counter(STATES.get(current.get(t.id, {}).get('state'), 'sin datos') for t in group)
        text = (f'D{n} · categoría {category} · {len(group)} destinos · período: ' +
                (f'latencia media {ms(sum(means) / len(means))} (entre {ms(min(means))} y {ms(max(means))}), peor {worst.name} con {ms(stats[worst.id]["mean"])}'
                 if measured else 'sin mediciones en el período') +
                (f' · con pérdida o fallos: ' + ', '.join(f'{t.name} {pct(v)}' for t, v in sorted(lossy, key=lambda x: -x[1])[:5]) if lossy else
                 ' · sin pérdida ni fallos' if measured else '') +
                ' · ahora: ' + ', '.join(f'{state}: {count}' for state, count in states.most_common()) +
                (f' · {sum(incidents[t.id] for t in group)} incidentes en el período' if any(incidents[t.id] for t in group) else ''))
        lines.append(Line(f'D{n}', start, priority, text, {'kind': 'probe', 'source': 'probe', 'ids': [t.id for t in group][:200], 'count': len(group)}))
    if not detail:return lines
    # One line per destination, the most affected first when they do not all fit.
    ranked = sorted(targets, key=lambda t: (-incidents[t.id], -(failure(t) or 0), -(stats.get(t.id, {}).get('mean') or 0)))[:MAX_DESTINATIONS]
    for t in sorted(ranked, key=lambda t: t.name):
        n += 1
        s = stats.get(t.id)
        now_ = current.get(t.id, {})
        kind = 'ICMP' if t.protocol == 'icmp' else f'{t.protocol.upper()} (tiempo de respuesta del chequeo)'
        text = (f'D{n} · destino {t.name}' + (f' ({t.service})' if t.service else '') + f' · {", ".join(t.categories or []) or "sin categoría"} · {kind} · {t.address} · ' +
                (f'período: latencia media {ms(s["mean"])}, máxima {ms(s["max"])}, {label(t)} {pct(failure(t))}' if s and s['mean'] is not None else
                 'período: sin mediciones') +
                f' · ahora: {STATES.get(now_.get("state"), "sin datos")}' + (f', p95 {ms(now_.get("p95_ms"))}' if now_.get('p95_ms') is not None else '') +
                (f' · {incidents[t.id]} incidentes en el período' if incidents[t.id] else ''))
        lines.append(Line(f'D{n}', start, priority, text, {'kind': 'probe', 'source': 'probe', 'ids': [t.id], 'count': 1}))
    if len(targets) > MAX_DESTINATIONS:
        lines.append(Line('D0', start, priority, f'Hay {len(targets) - MAX_DESTINATIONS} destinos más de esta selección que no se muestran uno a uno '
                          '(se muestran los más afectados).', {'kind': 'none', 'ids': [], 'count': 0}))
    return lines


def access_lines(db, start, end, plan, priority=0):
    from ..olts.api import down, pon_down
    olts = list(db.scalars(select(Olt).where(Olt.present.is_(True)).order_by(Olt.name)))
    if plan.get('olts'):olts = [o for o in olts if o.name in plan['olts']]
    lines, n = [], 0
    for olt in olts:
        pons = list(db.scalars(select(OltPonPort).where(OltPonPort.olt_id == olt.id)))
        uplinks = list(db.scalars(select(OltUplink).where(OltUplink.olt_id == olt.id)))
        onus = Counter(db.execute(select(Onu.status, Onu.signal).where(Onu.olt_id == olt.id, Onu.present.is_(True))).all())
        status = Counter();signal = Counter()
        for (st, sig), count in onus.items():status[st or 'sin estado'] += count;signal[sig or 'sin lectura'] += count
        down_pons = [f'{p.board}/{p.port}' for p in pons if pon_down(p)]
        down_links = [u.name for u in uplinks if down(u.admin_status, u.status)]
        n += 1
        text = (f'O{n} · OLT {olt.name} ({olt.hardware or "sin modelo"}, {olt.ip or "sin IP"}) · ahora: ' +
                ('alcanzable' if olt.reachable else 'SmartOLT no la alcanza') +
                f' · ONUs en línea {sum(p.online for p in pons)} de {sum(p.onus for p in pons)}' +
                f' · ONUs por estado: ' + ', '.join(f'{s} {c}' for s, c in status.most_common()) +
                f' · señal: ' + ', '.join(f'{s} {c}' for s, c in signal.most_common()) +
                f' · PON caídos: {", ".join(down_pons) if down_pons else "ninguno"} de {sum(1 for p in pons if p.onus)} con ONUs' +
                f' · uplinks caídos: {", ".join(down_links) if down_links else "ninguno"}' +
                (f' · temperatura {olt.temperature:.0f} °C' if olt.temperature is not None else '') +
                (f' · uptime {span_text(olt.uptime_seconds)}' if olt.uptime_seconds else ''))
        lines.append(Line(f'O{n}', start, priority, text, {'kind': 'smartolt', 'source': 'smartolt', 'ids': [olt.id], 'count': 1}))
    # PON outages of the period, ended ones included.
    names = {o.id: o.name for o in olts}
    outages = list(db.scalars(select(OltOutage).where(OltOutage.olt_id.in_(list(names) or ['']), OltOutage.cause != 'stale',
                                                       func.coalesce(OltOutage.started_at, OltOutage.first_seen) <= end,
                                                       (OltOutage.ended_at.is_(None)) | (OltOutage.ended_at >= start))
                          .order_by(OltOutage.affected.desc()).limit(MAX_OUTAGES + 1)))
    for o in outages[:MAX_OUTAGES]:
        n += 1
        began = o.started_at or o.first_seen
        lines.append(Line(f'O{n}', began, priority,
            f'O{n} · OLT {names[o.olt_id]} · PON {o.board}/{o.port}: {CAUSES.get(o.cause, o.cause)} · {o.affected} de {o.subscribers} ONUs' +
            (f' · zona {o.zone}' if o.zone else '') + (f' · splitters {", ".join(o.odbs[:5])}' if o.odbs else '') +
            f' · desde {stamp_text(began)} ' + (f'hasta {stamp_text(o.ended_at)}' if o.ended_at else '(sigue activa)'),
            {'kind': 'smartolt', 'source': 'smartolt', 'ids': [o.id], 'count': 1}))
    if len(outages) > MAX_OUTAGES:
        lines.append(Line('O0', start, priority, 'Hay más caídas de PON en el período que no se muestran (se muestran las de más ONUs afectadas).',
                          {'kind': 'none', 'ids': [], 'count': 0}))
    if olts and not outages:
        n += 1
        lines.append(Line(f'O{n}', start, priority, f'O{n} · SmartOLT no registra caídas de PON en el período.', {'kind': 'none', 'ids': [], 'count': 0}))
    return lines
