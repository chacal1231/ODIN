"""ODIN's inbox: what needs a person, what is in progress and what was resolved in the shift, in one read.

Built from what ODIN already holds (cases, probe incidents, PON outages, analyses, notes and its own health); it
decides nothing. Every item carries a link (#caso=, #incidente=, #olt=) that opens it in place.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, exists
from sqlalchemy.orm import Session
from .db import get_db
from .models import (Analysis, AnalysisReview, Case, CaseNote, Olt, OltOutage, OltUplink, ProbeIncident, ProbeTarget, User, now)

SHIFT = 12 * 3600
LIMIT = 50
KINDS = {'bgp_session': 'Sesión BGP', 'interface': 'Interfaz', 'latency': 'Latencia hacia', 'access': 'Acceso'}
CAUSES = {'los': 'Corte de fibra (LOS)', 'power': 'Falla de energía', 'offline': 'Sin conexión'}
# Health checks grouped into the components shown in the inbox strip.
COMPONENTS = (('probe', 'Sondeador'), ('tracer', 'Trazador'), ('smartolt', 'SmartOLT'), ('analyst', 'Analista'),
              ('events', 'Lectores de eventos'), ('correlation', 'Correlador'), ('jobs', 'Cola de trabajos'))


def item(kind, id, link, severity, title, where='', detail='', at=None, **extra):
    return {'kind': kind, 'id': id, 'link': link, 'severity': severity, 'title': title, 'where': where, 'detail': detail, 'at': at, **extra}


def case_item(case, kind='case', **extra):
    role = case.detail.get('neighbor') or case.detail.get('port') or {}
    detail = (case.detail.get('reading') or {}).get('title', '') if case.kind == 'access' else role.get('summary') or role.get('label') or ''
    impact = case.detail.get('impact') or {}
    return item(kind, case.id, '#caso=' + case.id, case.severity, f'{KINDS.get(case.kind, case.kind)} {case.subject}', case.entity_label,
                detail, case.opened_at, drops=case.drops, impact=impact.get('text'), onus=impact.get('onus', 0),
                confirmed=impact.get('kind') == 'confirmed', **extra)


def incident_item(row, names, kind='incident', **extra):
    return item(kind, row.id, '#incidente=' + row.id, row.severity, names.get(row.target_id, row.target_id), row.address,
                '', row.opened_at, conditions=sorted(row.conditions or {}), **extra)


def outage_item(row, olts, kind='olt_outage', **extra):
    olt = olts.get(row.olt_id)
    return item(kind, row.id, '#olt=' + row.olt_id, 'critical' if row.cause == 'los' else 'warning',
                f'PON {row.board}/{row.port} · {CAUSES.get(row.cause, row.cause)}', olt.name if olt else row.olt_id,
                f'{row.affected} de {row.subscribers} ONUs' + (f' · zona {row.zone}' if row.zone else '') +
                (f' · {len(row.odbs)} splitters' if row.odbs else ''), row.started_at or row.first_seen, onus=row.affected, confirmed=True, **extra)


def components(checks):
    result = []
    for key, label in COMPONENTS:
        mine = [c for c in checks if c['id'].startswith(key)]
        state = 'critical' if any(c['state'] == 'critical' for c in mine) else 'warning' if mine else 'healthy'
        result.append({'id': key, 'label': label, 'state': state, 'titles': [c['title'] for c in mine]})
    return result


def build(db, stamp=None):
    from .cases.summary import alerting
    from .health import evaluate
    stamp = stamp or now();since = stamp - SHIFT
    alerts = alerting(db)
    # Level 1 reports every access outage: its case stands for the OLT's outages, uplinks and reach, alerting or not.
    access = [c for c in db.scalars(select(Case).where(Case.kind == 'access', Case.state == 'active')) if c not in alerts]
    alerts += access
    covered = {c.detail.get('olt_id') for c in alerts if c.kind == 'access'}
    incidents = list(db.scalars(select(ProbeIncident).where(ProbeIncident.closed_at.is_(None)).order_by(ProbeIncident.opened_at)))
    recovered = list(db.scalars(select(ProbeIncident).where(ProbeIncident.closed_at >= since).order_by(ProbeIncident.closed_at.desc()).limit(LIMIT)))
    target_ids = {r.target_id for r in incidents + recovered}
    names = dict(db.execute(select(ProbeTarget.id, ProbeTarget.name).where(ProbeTarget.id.in_(target_ids))).all()) if target_ids else {}
    olts = {o.id: o for o in db.scalars(select(Olt))}
    outages = [o for o in db.scalars(select(OltOutage).where(OltOutage.active.is_(True), OltOutage.cause != 'stale').order_by(OltOutage.first_seen))
               if o.olt_id not in covered]
    ended = list(db.scalars(select(OltOutage).where(OltOutage.ended_at >= since, OltOutage.cause != 'stale').order_by(OltOutage.ended_at.desc()).limit(LIMIT)))
    reviewed = exists().where(AnalysisReview.analysis_id == Analysis.id)
    unreviewed = list(db.scalars(select(Analysis).where(Analysis.kind == 'case', Analysis.status == 'done', Analysis.finished_at >= stamp - 86400,
                                                       ~reviewed).order_by(Analysis.finished_at.desc()).limit(LIMIT)))
    running = list(db.scalars(select(Analysis).where(Analysis.kind == 'case', Analysis.status.in_(['pending', 'running']))))
    closed = list(db.scalars(select(Case).where(Case.closed_at >= since).order_by(Case.closed_at.desc()).limit(LIMIT)))
    case_ids = {a.case_id for a in unreviewed + running} | {c.id for c in closed}
    cases = {c.id: c for c in db.scalars(select(Case).where(Case.id.in_(case_ids)))} if case_ids else {}
    # The latest case analysis of each closed case, and whether the NOC rated it.
    latest = {}
    for analysis in db.scalars(select(Analysis).where(Analysis.case_id.in_([c.id for c in closed] or ['']), Analysis.kind == 'case',
                                                       Analysis.status == 'done').order_by(Analysis.finished_at)):
        latest[analysis.case_id] = analysis
    rated = set(db.scalars(select(AnalysisReview.analysis_id).where(AnalysisReview.analysis_id.in_([a.id for a in latest.values()] or ['']))))
    notes = list(db.execute(select(CaseNote, Case).join(Case, Case.id == CaseNote.case_id).where(
        CaseNote.created_at >= since, CaseNote.text.startswith('Proveedor')).order_by(CaseNote.created_at.desc()).limit(10)).all())
    acknowledgers = {u.id: u.username for u in db.scalars(select(User))}
    checks = evaluate(db, stamp)['checks']
    uplinks_down = [u for u in db.scalars(select(OltUplink).where(OltUplink.admin_status == 'Enabled'))
                    if u.status.lower() == 'down' and olts.get(u.olt_id) and olts[u.olt_id].present and u.olt_id not in covered]

    attention = ([case_item(c) for c in alerts if not c.acknowledged_at]
                 + [incident_item(r, names) for r in incidents if not r.acknowledged_at]
                 + [outage_item(o, olts) for o in outages]
                 + [item('olt', o.id, '#olt=' + o.id, 'critical', 'OLT inalcanzable', o.name, 'SmartOLT no llega a la OLT', o.last_seen)
                    for o in olts.values() if o.present and not o.reachable and o.id not in covered]
                 + [item('olt', u.olt_id + ':' + u.name, '#olt=' + u.olt_id, 'critical', f'Uplink {u.name} caído', olts[u.olt_id].name,
                         u.description, u.last_seen) for u in uplinks_down]
                 + [item('review', a.id, '#caso=' + a.case_id, 'info', 'Valora el diagnóstico de ODIN',
                         cases[a.case_id].entity_label if a.case_id in cases else '', (a.result or {}).get('resumen', '')[:300], a.finished_at,
                         case_title=f'{KINDS.get(cases[a.case_id].kind, "")} {cases[a.case_id].subject}' if a.case_id in cases else '')
                    for a in unreviewed]
                 + [item('health', c['id'], None, c['state'], c['title'], c.get('page', ''), c['detail'], stamp, page=c.get('page'))
                    for c in checks])
    rank = {'critical': 0, 'warning': 1, 'info': 2}
    # The network first, then ODIN's own health (also in the status bar); within each level, confirmed customer impact
    # first and the most customers first, then the oldest problem.
    attention.sort(key=lambda x: (rank.get(x['severity'], 3), x['kind'] == 'health', not x.get('confirmed'), -(x.get('onus') or 0), x['at'] or 0))
    progress = ([case_item(c, acknowledged_by=acknowledgers.get(c.acknowledged_by), acknowledged_at=c.acknowledged_at) for c in alerts if c.acknowledged_at]
                + [incident_item(r, names, acknowledged_by=acknowledgers.get(r.acknowledged_by), acknowledged_at=r.acknowledged_at)
                   for r in incidents if r.acknowledged_at]
                + [item('analysis', a.id, '#caso=' + a.case_id, 'info', 'ODIN está analizando',
                        f'{KINDS.get(cases[a.case_id].kind, "")} {cases[a.case_id].subject} · {cases[a.case_id].entity_label}' if a.case_id in cases else '',
                        a.stage or a.status, a.created_at) for a in running]
                + [item('note', n.id, '#caso=' + c.id, 'info', n.text.split(':', 1)[-1].strip()[:300], f'{KINDS.get(c.kind, c.kind)} {c.subject}',
                        acknowledgers.get(n.author_id, ''), n.created_at) for n, c in notes])
    resolved = ([case_item(c, kind='case_closed', closed_at=c.closed_at, analysis_id=latest[c.id].id if c.id in latest else None,
                           rated=c.id in latest and latest[c.id].id in rated) for c in closed]
                + [incident_item(r, names, kind='incident_recovered', closed_at=r.closed_at) for r in recovered]
                + [outage_item(o, olts, kind='olt_outage_ended', closed_at=o.ended_at) for o in ended])
    resolved.sort(key=lambda x: -(x.get('closed_at') or 0))
    counts = {'Casos': sum(1 for c in alerts if not c.acknowledged_at), 'Incidentes': sum(1 for r in incidents if not r.acknowledged_at),
              'OLTs': len(outages) + sum(1 for o in olts.values() if o.present and not o.reachable) + len(uplinks_down)}
    from .probes.verdict import compute as verdict
    from .probes.service import origin_view
    return {'verdict': verdict(db, stamp=stamp, origin_healthy=origin_view(db, stamp)['state'] == 'healthy'), 'attention': attention[:LIMIT], 'progress': progress[:LIMIT], 'resolved': resolved[:LIMIT], 'counts': counts,
            'health': components(checks), 'shift_hours': SHIFT // 3600, 'at': stamp}


def router(current):
    api = APIRouter(prefix='/api/v1')

    @api.get('/inbox')
    def inbox(user=Depends(current), db: Session = Depends(get_db)):
        result = build(db);db.commit()
        return result

    return api
