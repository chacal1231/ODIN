"""Correlated cases: queue, detail with explained evidence, acknowledgement and notes."""
import ipaddress
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Case, CaseEvidence, CaseNote, CorrelationState, LogSender, SourceIdentity, User, Device, now
from ..security import audit
from .entities import Resolver
from .correlator import window, case_entity, RULE_VERSION, LATENCY

TRIGGER_LIMIT = 300


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AlertSettings(BaseModel):
    alerts_enabled: bool


def view(case):
    return {k: getattr(case, k) for k in ('id', 'kind', 'entity_key', 'entity_label', 'subject', 'detail', 'opened_at',
        'last_activity_at', 'last_state', 'last_state_at', 'resolved_at', 'closed_at', 'state', 'severity', 'drops',
        'recoveries', 'acknowledged_at', 'rule_version')}


def same_link(a, b):
    """Both ends of one point-to-point link: different addresses in the same /30 (IPv4) or /126 (IPv6)."""
    try:
        x, y = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    if x == y or x.version != y.version:
        return False
    prefix = 30 if x.version == 4 else 126
    return ipaddress.ip_network(f'{x}/{prefix}', strict=False) == ipaddress.ip_network(f'{y}/{prefix}', strict=False)


def addresses_by_entity(db, resolver):
    """Every address any source reports for an entity, to find the other end of a session."""
    result = {}
    for row in db.scalars(select(LogSender)):result.setdefault(row.address, resolver.entity('graylog', row.name).key)
    for identity in db.scalars(select(SourceIdentity)):
        entity = resolver.by_device(identity.device_id)
        for address in identity.addresses:
            if entity:result.setdefault(address, entity.key)
    return result


def pop_keys(db, case):
    """Entity keys of the other inventory devices in the case's POP."""
    device = db.get(Device, case.entity_key.removeprefix('device:')) if case.entity_key.startswith('device:') else None
    pop = case.detail.get('pop_id') or (device.pop_id if device else None)
    if not pop:return set()
    return {'device:' + d for d in db.scalars(select(Device.id).where(Device.pop_id == pop, Device.archived.is_(False)))} - {case.entity_key}


def related_cases(db, case, resolver):
    """Same equipment or POP in the same period, or the other end of the same session (the peer address belongs to that equipment).

    An external service shares no equipment with the network: its related cases are those of the same period,
    marked as coincidence in time only.
    """
    start, end = window(case)
    owner = addresses_by_entity(db, resolver)
    mine = {a for a, key in owner.items() if key == case.entity_key}
    same_pop = pop_keys(db, case)
    external = case.kind == LATENCY and case.detail.get('scope') == 'external'
    mine_link = case.detail.get('neighbor') or {}
    overlapping = list(db.scalars(select(Case).where(Case.id != case.id, Case.opened_at <= end,
        func.coalesce(Case.closed_at, Case.last_activity_at) >= start).order_by(Case.opened_at).limit(200)))
    related = []
    for other in overlapping:
        if case.kind == 'bgp_session' and other.kind == 'bgp_session' and other.entity_key != case.entity_key and (
                (owner.get(case.subject) == other.entity_key and other.subject in mine)):
            related.append({**view(other), 'relation': 'Otro extremo de la misma sesión BGP'})
        elif case.kind == 'bgp_session' and other.kind == 'bgp_session' and other.entity_key != case.entity_key and same_link(case.subject, other.subject):
            related.append({**view(other), 'relation': 'Otro extremo de la misma sesión BGP (vecinos en el mismo enlace punto a punto)'})
        elif mine_link.get('interface') and (other.detail.get('neighbor') or {}).get('interface') == mine_link['interface'] and other.entity_key == case.entity_key:
            related.append({**view(other), 'relation': 'Misma interfaz: otra sesión del mismo enlace'})
        elif mine_link.get('remote_device_id') and (other.detail.get('neighbor') or {}).get('remote_device_id') == mine_link['remote_device_id']:
            related.append({**view(other), 'relation': f'Otra ruta hacia {mine_link["remote_device_name"]} en el mismo período'})
        elif other.entity_key == case.entity_key:
            related.append({**view(other), 'relation': 'Mismo equipo en el mismo período'})
        elif other.entity_key in same_pop:
            related.append({**view(other), 'relation': 'Otro equipo del mismo POP en el mismo período'})
        elif external:
            related.append({**view(other), 'relation': 'Mismo período, solo coincidencia temporal'})
    return related


def isolation(db, case, related):
    """For an internal link: how many of the known links to the far router had drops in the same period."""
    far = (case.detail.get('neighbor') or {}).get('remote_device_id')
    if not far:return None
    from ..inventory.roles import Network
    links = Network(db).links_to(far)
    down = {(n['device_id'], n['interface']) for n in [case.detail['neighbor']] + [(r['detail'] or {}).get('neighbor') or {} for r in related]
            if n.get('remote_device_id') == far and n.get('interface')}
    name = case.detail['neighbor']['remote_device_name']
    if not links:return None
    if down >= links:
        return f'Todas las rutas conocidas hacia {name} ({len(links)}) tuvieron caídas en el mismo período: el equipo pudo quedar aislado.'
    return f'{len(down & links)} de {len(links)} rutas conocidas hacia {name} tuvieron caídas en el mismo período.'


def router(current, admin):
    api = APIRouter(prefix='/api/v1/cases')

    @api.get('')
    def cases(state: str = Query('open', pattern='^(open|alerting|active|stale|closed|all)$'), kind: str | None = Query(None, pattern='^(bgp_session|interface|latency)$'),
              before: str | None = Query(None, max_length=60), limit: int = Query(50, ge=1, le=100),
              user=Depends(current), db: Session = Depends(get_db)):
        query = select(Case)
        if state == 'open':query = query.where(Case.state != 'closed')
        elif state == 'alerting':
            from .summary import alerting
            query = query.where(Case.id.in_([c.id for c in alerting(db)] or ['']))
        elif state != 'all':query = query.where(Case.state == state)
        if kind:query = query.where(Case.kind == kind)
        if before:
            try:stamp, key = before.split(':', 1);stamp = int(stamp)
            except ValueError:raise HTTPException(422, 'Cursor inválido')
            query = query.where(or_(Case.last_activity_at < stamp, and_(Case.last_activity_at == stamp, Case.id < key)))
        rows = list(db.scalars(query.order_by(Case.last_activity_at.desc(), Case.id.desc()).limit(limit + 1)))
        counts = dict(db.execute(select(Case.state, func.count()).group_by(Case.state)).all())
        from .summary import alerting
        counts['alerting'] = len(alerting(db))
        state_row = db.get(CorrelationState, 'cases')
        return {'items': [view(c) for c in rows[:limit]], 'counts': counts,
                'next': f'{rows[limit - 1].last_activity_at}:{rows[limit - 1].id}' if len(rows) > limit else None,
                'correlator': {k: getattr(state_row, k) if state_row else None for k in ('last_run_at', 'last_success_at', 'last_error', 'alerts_enabled')},
                'rule_version': RULE_VERSION}

    @api.get('/calibration')
    def calibration(user=Depends(current), db: Session = Depends(get_db)):
        from .summary import calibration
        return calibration(db)

    @api.get('/{case_id}')
    def detail(case_id: str, user=Depends(current), db: Session = Depends(get_db)):
        case = db.get(Case, case_id)
        if not case:raise HTTPException(404, 'Caso no encontrado')
        resolver = Resolver(db)
        entity = case_entity(resolver, case)
        triggers_total = db.scalar(select(func.count()).select_from(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role == 'trigger'))
        triggers = list(db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role == 'trigger')
                                   .order_by(CaseEvidence.occurred_at.desc()).limit(TRIGGER_LIMIT)))
        others = list(db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role != 'trigger')))
        evidence = sorted(triggers + others, key=lambda e: (e.occurred_at, e.id))
        start, end = window(case)
        related = related_cases(db, case, resolver)
        notes = db.execute(select(CaseNote, User.username).join(User, User.id == CaseNote.author_id, isouter=True)
                           .where(CaseNote.case_id == case.id).order_by(CaseNote.created_at)).all()
        return {**view(case), 'identity': resolver.explain(entity), 'entity_confirmed': entity.device_id is not None,
                'triggers_total': triggers_total, 'window': {'start': start, 'end': end},
                'evidence': [{k: getattr(e, k) for k in ('id', 'source', 'ref', 'role', 'strength', 'reason', 'occurred_at', 'title', 'data')} for e in evidence],
                'related': related[:20], 'notes': [{'id': n.id, 'text': n.text, 'created_at': n.created_at, 'author': name} for n, name in notes],
                'neighbor': case.detail.get('neighbor'), 'port': case.detail.get('port'), 'isolation': isolation(db, case, related)}

    @api.post('/{case_id}/acknowledge')
    def acknowledge(case_id: str, user=Depends(current), db: Session = Depends(get_db)):
        case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
        if not case:raise HTTPException(404, 'Caso no encontrado')
        if not case.acknowledged_at:
            case.acknowledged_at = now();case.acknowledged_by = user.id;audit(db, 'case_acknowledged', user.id, case_id)
        db.commit();return {'ok': True}

    @api.post('/{case_id}/notes', status_code=201)
    def note(case_id: str, body: NoteBody, user=Depends(current), db: Session = Depends(get_db)):
        if not db.get(Case, case_id):raise HTTPException(404, 'Caso no encontrado')
        db.add(CaseNote(case_id=case_id, author_id=user.id, text=body.text.strip()))
        audit(db, 'case_note_added', user.id, case_id);db.commit();return {'ok': True}

    @api.put('/settings/alerts')
    def alerts(body: AlertSettings, user=Depends(admin), db: Session = Depends(get_db)):
        state = db.get(CorrelationState, 'cases')
        if not state:state = CorrelationState(id='cases');db.add(state)
        state.alerts_enabled = body.alerts_enabled
        audit(db, 'case_alerts_enabled' if body.alerts_enabled else 'case_alerts_disabled', user.id, 'cases')
        db.commit();return {'ok': True}

    return api
