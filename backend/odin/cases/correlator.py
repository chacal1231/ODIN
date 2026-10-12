"""Deterministic correlator: state changes of BGP sessions and interfaces, and latency incidents, become cases with explained evidence.

Rules (version RULE_VERSION):
- A case opens when a session leaves Established or an interface goes down (router logs).
- A latency incident opens (or joins) a latency case: per destination for equipment of the own network,
  per service for external destinations. Its state and severity follow its incidents, which already
  apply persistence and recovery.
- Changes of the same entity and subject join the case; after QUIET seconds stable (up) it closes,
  and a later drop opens a new case. A case still down without changes for STALE seconds is stale.
- Critical: down for SUSTAINED seconds or at least FLAP_CRITICAL drops in the case, or from the first drop
  when the neighbor or the interface is a transit, a transport or an internal link between own routers
  (read from the router port descriptions, see inventory/roles.py).
- Access: one case per OLT with its PON outages, uplinks and reachability from SmartOLT (cases/access.py).
- Impact: every open case carries how many customers it affects and where (cases/impact.py).
- Paths: a latency case gets the traces taken when its incidents opened and recovered and the route changes of its
  checks in the period, before and after, network by network; a case of a router gets the route changes of checks whose
  path crosses it (known only with the inventory).
- Evidence links data ODIN already holds and states why it is linked. Coincidence in time alone is
  never used as evidence: every link shares the entity (equipment) or the subject (peer, interface).
  External services share no equipment with the network; their cases only list, as related, cases of
  the same period, marked as coincidence in time.
"""
import re
from sqlalchemy import select, delete, and_, or_
from ..models import (Case, CaseEvidence, SourceEvent, EventStream, ProbeIncident, ProbeTarget, ProbeTrace, Device,
                      MonitoringDestination, CorrelationState, now)
from .entities import Resolver, Entity
from ..inventory import roles
from . import access
from .access import ACCESS

RULE_VERSION = 4
QUIET = 1800
STALE = 86400
SUSTAINED = 900
FLAP_CRITICAL = 5
MARGIN = 900
CONTEXT_CAP = 150
BATCH = 5000
REFRESH = 600
KINDS = {'bgp': 'bgp_session', 'interface': 'interface'}
LATENCY = 'latency'


def parse(condition):
    """Condition keys written by the Graylog reader: bgp:<source>:<vrf>:<peer> and interface:<source>:<interface>."""
    prefix, _, rest = (condition or '').partition(':')
    if prefix == 'bgp':
        source, vrf, peer = (rest.split(':', 2) + ['', ''])[:3]
        return {'kind': 'bgp_session', 'source': source, 'subject': peer, 'detail': {'vrf': vrf, 'peer': peer}} if peer else None
    if prefix == 'interface':
        source, _, interface = rest.partition(':')
        return {'kind': 'interface', 'source': source, 'subject': interface, 'detail': {'interface': interface}} if interface else None
    return None


def case_key(kind, entity_key, subject):
    return f'{kind}|{entity_key}|{subject}'[:400]


def close(case):
    case.state = 'closed';case.closed_at = case.resolved_at or case.last_activity_at
    if case.kind not in (LATENCY, ACCESS):case.severity = 'critical' if case.drops >= FLAP_CRITICAL else case.severity


def case_entity(resolver, case):
    """The equipment (or external service) a case is about."""
    if case.kind not in (LATENCY, ACCESS):
        return resolver.entity('graylog', case.detail.get('source_name', ''))
    device_id = case.detail.get('device_id')
    if device_id:
        return resolver.by_device(device_id) or Entity(key='device:' + device_id, label=case.entity_label, device_id=device_id)
    return Entity(key=case.entity_key, label=case.entity_label, device_id=None)


def latency_subject(db, incident, target):
    """Own equipment: one case per destination. External: one case per service, all its regions together."""
    destination = db.get(MonitoringDestination, target.destination_id) if target.destination_id else None
    device = db.get(Device, destination.device_id) if destination and destination.device_id else None
    if device:
        return 'device:' + device.id, device.name, destination.name, {'device_id': device.id, 'destination_id': destination.id,
                                                                      'pop_id': device.pop_id or incident.pop_id, 'scope': 'network'}
    service = target.service or (destination.name if destination else target.name)
    return 'service:' + service, service, service, {'service': service, 'scope': 'external'}


def ingest_latency(db, touched):
    """Latency incidents not yet linked to a case, in opening order."""
    linked = select(CaseEvidence.ref).where(CaseEvidence.source == 'probe', CaseEvidence.role == 'trigger')
    rows = db.execute(select(ProbeIncident, ProbeTarget).join(ProbeTarget, ProbeTarget.id == ProbeIncident.target_id)
        .where(ProbeIncident.id.not_in(linked)).order_by(ProbeIncident.opened_at, ProbeIncident.id).limit(BATCH)).all()
    for incident, target in rows:
        entity_key, label, subject, detail = latency_subject(db, incident, target)
        key = case_key(LATENCY, entity_key, subject)
        case = db.scalar(select(Case).where(Case.key == key, Case.state != 'closed'))
        if case and case.last_state == 'up' and incident.opened_at - case.last_activity_at > QUIET:
            close(case);db.flush();case = None
        if case is None:
            case = Case(kind=LATENCY, key=key, entity_key=entity_key, entity_label=label, subject=subject, detail=detail,
                        opened_at=incident.opened_at, last_activity_at=incident.opened_at, last_state='down',
                        last_state_at=incident.opened_at, state='active', rule_version=RULE_VERSION)
            db.add(case);db.flush()
        conditions = ', '.join(f'{k} {v}' for k, v in sorted((incident.conditions or {}).items()))
        db.add(CaseEvidence(case_id=case.id, source='probe', ref=incident.id, role='trigger', strength='entity',
            reason='Incidente de latencia del mismo destino' if detail['scope'] == 'network' else 'Incidente de latencia del mismo servicio',
            occurred_at=incident.opened_at, title=f'{target.name} ({incident.address}): incidente de latencia' + (f' · {conditions}' if conditions else ''),
            data={'target': target.name, 'address': incident.address}, rule_version=RULE_VERSION))
        latency_state(db, case);touched.add(case.id)
    return len(rows) == BATCH


def latency_state(db, case):
    """State, counts and severity of a latency case follow its incidents."""
    db.flush()
    incidents = list(db.scalars(select(ProbeIncident).join(CaseEvidence, and_(CaseEvidence.ref == ProbeIncident.id,
        CaseEvidence.case_id == case.id, CaseEvidence.source == 'probe', CaseEvidence.role == 'trigger'))))
    if not incidents:return
    still = [i for i in incidents if i.closed_at is None]
    case.drops, case.recoveries = len(incidents), len(incidents) - len(still)
    case.last_activity_at = max(max(i.opened_at, i.closed_at or 0) for i in incidents)
    if still:
        case.last_state, case.last_state_at, case.resolved_at = 'down', max(i.opened_at for i in still), None
    else:
        case.last_state = 'up';case.last_state_at = case.resolved_at = max(i.closed_at for i in incidents)
    case.severity = 'critical' if any(i.severity == 'critical' for i in incidents) else 'warning'


def apply(db, event, info, entity, touched):
    key = case_key(info['kind'], entity.key, info['subject'])
    case = db.scalar(select(Case).where(Case.key == key, Case.state != 'closed'))
    if case and case.last_state == 'up' and event.occurred_at - case.last_activity_at > QUIET:
        close(case);db.flush();case = None
    if case is None:
        if event.status != 'open':
            return  # A recovery without a known drop opens nothing.
        case = Case(kind=info['kind'], key=key, entity_key=entity.key, entity_label=entity.label, subject=info['subject'],
                    detail={**info['detail'], 'source_name': info['source']}, opened_at=event.occurred_at,
                    last_activity_at=event.occurred_at, last_state='down', last_state_at=event.occurred_at,
                    state='active', rule_version=RULE_VERSION)
        db.add(case);db.flush()
    if event.status == 'open':
        case.drops += 1
    else:
        case.recoveries += 1;case.resolved_at = max(case.resolved_at or 0, event.occurred_at)
    if event.occurred_at >= case.last_state_at:
        case.last_state = 'down' if event.status == 'open' else 'up';case.last_state_at = event.occurred_at
    case.last_activity_at = max(case.last_activity_at, event.occurred_at)
    if case.state == 'stale':case.state = 'active'
    what = 'la misma sesión BGP' if info['kind'] == 'bgp_session' else 'la misma interfaz'
    db.add(CaseEvidence(case_id=case.id, source='graylog', ref=event.id, role='trigger', strength='entity',
        reason=f'Cambio de estado de {what}', occurred_at=event.occurred_at, title=event.title,
        data={'status': event.status, 'state': event.original_state}, rule_version=RULE_VERSION))
    touched.add(case.id)


def ingest(db, resolver, state, touched):
    """New state changes in reception order; applied in source time order within the batch."""
    rows = db.execute(select(SourceEvent).join(EventStream, EventStream.id == SourceEvent.stream_id).where(
        EventStream.source == 'graylog', SourceEvent.status.in_(['open', 'recovered']),
        or_(SourceEvent.condition_key.like('bgp:%'), SourceEvent.condition_key.like('interface:%')),
        or_(SourceEvent.received_at > state.cursor_received_at,
            and_(SourceEvent.received_at == state.cursor_received_at, SourceEvent.id > state.cursor_id)))
        .order_by(SourceEvent.received_at, SourceEvent.id).limit(BATCH)).scalars().all()
    if not rows:return False
    for event in sorted(rows, key=lambda e: (e.occurred_at, e.received_at, e.id)):
        info = parse(event.condition_key)
        if info:apply(db, event, info, resolver.entity('graylog', info['source']), touched)
    state.cursor_received_at, state.cursor_id = rows[-1].received_at, rows[-1].id
    return len(rows) == BATCH


def lifecycle(db, resolver, stamp, touched):
    # Cases marked for recomputation, closed ones included.
    touched.update(db.scalars(select(Case.id).where(Case.enriched_at.is_(None))))
    for case in db.scalars(select(Case).where(Case.state != 'closed')):
        if case.kind == LATENCY:latency_state(db, case)
        entity = case_entity(resolver, case)
        # Identity can strengthen later (an association gets confirmed): follow it when the new key is free.
        key = case_key(case.kind, entity.key, case.subject)
        # An access case is keyed by its OLT, whatever device the inventory matches it to.
        if case.kind != ACCESS and key != case.key and not db.scalar(select(Case.id).where(Case.key == key, Case.state != 'closed')):
            case.key, case.entity_key, case.entity_label = key, entity.key, entity.label;touched.add(case.id)
        if case.last_state == 'up' and stamp - case.last_activity_at > QUIET:
            close(case);touched.add(case.id);continue
        if case.kind in (LATENCY, ACCESS):
            # An open incident or outage is ongoing by definition: its source keeps reporting it, so it never goes stale.
            if case.state == 'stale':case.state = 'active'
        else:
            if case.last_state == 'down' and stamp - case.last_activity_at > STALE and case.state == 'active':
                case.state = 'stale';touched.add(case.id)
            down_long = case.last_state == 'down' and stamp - case.last_state_at > SUSTAINED
            vital = roles.critical(case.detail.get('neighbor') or case.detail.get('port'))
            case.severity = 'critical' if down_long or case.drops >= FLAP_CRITICAL or vital else 'warning'
        if case.state == 'active' and (not case.enriched_at or case.enriched_at < stamp - REFRESH):touched.add(case.id)


def window(case):
    return case.opened_at - MARGIN, (case.closed_at or case.last_activity_at) + MARGIN


def evidence(case, source, ref, role, strength, reason, occurred_at, title, data=None):
    return CaseEvidence(case_id=case.id, source=source, ref=str(ref)[:300], role=role, strength=strength, reason=reason[:300],
                        occurred_at=occurred_at, title=(title or '')[:500], data=data or {}, rule_version=RULE_VERSION)


def related_logs(db, case, entity, start, end):
    names = entity.names('graylog') or [case.detail.get('source_name', '')]
    rows = db.scalars(select(SourceEvent).join(EventStream, EventStream.id == SourceEvent.stream_id).where(
        EventStream.source == 'graylog', SourceEvent.entity_id.in_(names), SourceEvent.occurred_at >= start,
        SourceEvent.occurred_at <= end).order_by(SourceEvent.occurred_at))
    own = {k for k in [case.detail.get('source_name')] if k}
    found = {'entity': [], 'device': []}
    for event in rows:
        info = parse(event.condition_key)
        if info and info['kind'] == case.kind and info['subject'] == case.subject and info['source'] in own:
            continue  # the case's own state changes are its triggers
        reason, strength = None, None
        if case.kind == 'bgp_session':
            if case.subject in event.title:
                reason, strength = 'Menciona el mismo vecino BGP', 'entity'
            elif event.condition_key and event.condition_key.startswith('bfd:') and case.subject in event.condition_key:
                reason, strength = 'BFD del mismo vecino', 'entity'
        elif case.kind == 'interface' and (f'Interface {case.subject}' in event.title or f'intf:{case.subject}' in event.title or (event.condition_key or '').endswith(':' + case.subject)):
            reason, strength = 'Menciona la misma interfaz', 'entity'
        # Device-wide context only when it can matter to any subject: configuration changes and critical logs.
        # Logs about other sessions or interfaces belong to their own cases, shown as related.
        if not reason and event.kind == 'log_config':
            reason, strength = 'Cambio de configuración del mismo equipo', 'device'
        elif not reason and event.severity == 'critical' and not info:
            reason, strength = 'Log crítico del mismo equipo', 'device'
        if reason:
            found[strength].append(evidence(case, 'graylog', event.id, 'context', strength, reason, event.occurred_at, event.title,
                                            {'status': event.status, 'severity': event.severity}))
    # Over the cap, keep the first and the last logs (how it started and how it ended) and record the total.
    case.detail = {**case.detail, 'context_total': {k: len(v) for k, v in found.items()}}
    half = CONTEXT_CAP // 2
    return [e for items in found.values() for e in (items if len(items) <= CONTEXT_CAP else items[:half] + items[-half:])]


def device_events(db, case, entity, start, end):
    if not entity.device_id:return []
    rows = db.execute(select(SourceEvent, EventStream.source).join(EventStream, EventStream.id == SourceEvent.stream_id).where(
        EventStream.source.in_(['zabbix', 'librenms']), SourceEvent.device_id == entity.device_id, SourceEvent.status != 'info',
        SourceEvent.occurred_at >= start, SourceEvent.occurred_at <= end).order_by(SourceEvent.occurred_at.desc()).limit(CONTEXT_CAP)).all()
    neighbor = case.detail.get('neighbor') or {}
    interface = case.subject if case.kind == 'interface' else neighbor.get('interface') if neighbor.get('device_id') == entity.device_id else None
    result = []
    for event, source in rows:
        # Interface names are matched as whole words: Ethernet1 must not match Ethernet10.
        same = bool(interface) and re.search(r'(?<![\w/])' + re.escape(interface) + r'(?![\w/])', event.title, re.IGNORECASE) is not None
        reason = ('Alerta de la misma interfaz' if case.kind == 'interface' else 'Alerta de la interfaz hacia el vecino (por su subred)') if same else 'Alerta del mismo equipo'
        result.append(evidence(case, source, event.id, 'context', 'entity' if same else 'device', reason, event.occurred_at, event.title,
            {'status': event.status, 'severity': event.severity}))
    return result


def latency(db, case, entity, start, end):
    # A latency case's own incidents are its triggers.
    if not entity.device_id or case.kind == LATENCY:return []
    rows = db.execute(select(ProbeIncident, ProbeTarget.name).join(ProbeTarget, ProbeTarget.id == ProbeIncident.target_id)
        .join(MonitoringDestination, MonitoringDestination.id == ProbeTarget.destination_id).where(
        MonitoringDestination.device_id == entity.device_id, ProbeIncident.opened_at <= end,
        or_(ProbeIncident.closed_at.is_(None), ProbeIncident.closed_at >= start)).limit(CONTEXT_CAP)).all()
    return [evidence(case, 'probe', incident.id, 'impact', 'device', 'Incidente de latencia de un destino asociado al mismo equipo',
                     incident.opened_at, f'{name}: incidente {incident.severity}',
                     {'severity': incident.severity, 'closed_at': incident.closed_at, 'conditions': incident.conditions})
            for incident, name in rows]


TRACE_MOMENTS = {'incident_opened': 'traza al abrirse el incidente', 'incident_recovered': 'traza al recuperarse el incidente'}


def traces(db, case, entity, start, end):
    from ..probes.asnames import lookup, route
    from ..probes.trace import consistent
    window_rows = select(ProbeTrace).where(ProbeTrace.status == 'done', ProbeTrace.finished_at >= start, ProbeTrace.finished_at <= end)
    if case.kind == LATENCY:
        incidents = select(CaseEvidence.ref).where(CaseEvidence.case_id == case.id, CaseEvidence.source == 'probe', CaseEvidence.role == 'trigger')
        checks = select(ProbeIncident.target_id).where(ProbeIncident.id.in_(incidents))
        rows = list(db.scalars(window_rows.where(or_(ProbeTrace.incident_id.in_(incidents),
            and_(ProbeTrace.target_id.in_(checks), ProbeTrace.path_changed.is_(True)))).order_by(ProbeTrace.finished_at).limit(CONTEXT_CAP)))
        role, strength = 'context', 'entity'
    elif entity.device_id:
        crossing = db.scalars(window_rows.where(ProbeTrace.path_changed.is_(True)).order_by(ProbeTrace.finished_at).limit(CONTEXT_CAP * 4))
        rows = [t for t in crossing if any(h.get('device_id') == entity.device_id for h in t.hops)][:CONTEXT_CAP]
        role, strength = 'impact', 'device'
    else:return []
    rows = [t for t in rows if consistent(t.hops)]
    previous = {}
    for t in rows:
        if t.path_changed:
            # The path it was compared with: the latest comparable trace before it.
            previous[t.id] = next((p for p in db.scalars(select(ProbeTrace).where(ProbeTrace.target_id == t.target_id, ProbeTrace.address == t.address,
                ProbeTrace.status == 'done', ProbeTrace.finished_at < t.finished_at, ProbeTrace.fingerprint.is_not(None))
                .order_by(ProbeTrace.finished_at.desc()).limit(5)) if consistent(p.hops)), None)
    names = lookup(db, {h.get('asn') for t in rows + [p for p in previous.values() if p] for h in t.hops})
    targets = dict(db.execute(select(ProbeTarget.id, ProbeTarget.name).where(ProbeTarget.id.in_({t.target_id for t in rows}))).all()) if rows else {}
    result = []
    for t in rows:
        now_path = route(t.hops, names, t.reached or (t.path or {}).get('network_reached'))
        before = previous.get(t.id)
        if t.path_changed and before:
            title = f'{targets.get(t.target_id, t.target_id)}: cambio de ruta · antes {route(before.hops, names)} · ahora {now_path}'
            reason = ('Cambio de ruta de un destino del caso' if case.kind == LATENCY else
                      'Cambio de ruta de un destino cuyo camino pasa por este equipo')
        else:
            title = f'{targets.get(t.target_id, t.target_id)}: {TRACE_MOMENTS.get(t.reason, "traza")} · {now_path}'
            reason = 'Traza del mismo destino en un incidente del caso'
        result.append(evidence(case, 'trace', t.id, role, strength, reason, t.finished_at, title,
                               {'reason': t.reason, 'reached': t.reached, 'path_changed': t.path_changed, 'address': t.address}))
    return result


def enrich(db, resolver, case, stamp, network):
    entity = case_entity(resolver, case)
    start, end = window(case)
    # What the neighbor or the interface is, from the inventory: kept on the case for severity, relations and the analyst.
    if case.kind == 'bgp_session':
        case.detail = {**case.detail, 'neighbor': roles.summary(network.neighbor(case.subject, entity.device_id))}
    elif case.kind == 'interface':
        port = network.port(entity.device_id, case.subject) if entity.device_id else None
        case.detail = {**case.detail, 'port': port and {**port, 'label': roles.LABELS.get(port['role'])}}
    db.execute(delete(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role != 'trigger'))
    for builder in (related_logs, device_events, latency, traces):
        for item in builder(db, case, entity, start, end):
            db.add(item)
    case.enriched_at = stamp


def judge(db, stamp):
    """External cases carry ODIN's current verdict (ours, an exit's or the service's); the last one stays after recovery."""
    cases = [c for c in db.scalars(select(Case).where(Case.kind == LATENCY, Case.state == 'active')) if c.detail.get('scope') == 'external']
    if not cases:return
    from ..probes.verdict import compute, for_service
    from ..probes.service import origin_view
    found = compute(db, stamp=stamp, origin_healthy=origin_view(db, stamp)['state'] == 'healthy')
    for case in cases:
        finding = for_service(found, case.detail.get('service'))
        if not finding:continue
        value = {k: finding[k] for k in ('state', 'title', 'action', 'exit', 'direct') if k in finding}
        change = (finding.get('route_changes') or {}).get(case.detail.get('service'))
        if change:value['route_change'] = change
        if case.detail.get('verdict') != value:case.detail = {**case.detail, 'verdict': value}


def run(db, stamp=None):
    """One correlation pass; returns True when more events are pending."""
    stamp = stamp or now()
    state = db.get(CorrelationState, 'cases')
    if not state:
        state = CorrelationState(id='cases');db.add(state);db.flush()
    resolver = Resolver(db)
    touched = set()
    more = ingest(db, resolver, state, touched)
    more = ingest_latency(db, touched) or more
    access.ingest(db, touched, stamp)
    db.flush()
    lifecycle(db, resolver, stamp, touched)
    db.flush()
    network = roles.Network(db)
    for case in db.scalars(select(Case).where(Case.id.in_(touched))):
        enrich(db, resolver, case, stamp, network)
    judge(db, stamp)
    from .impact import annotate
    annotate(db, list(db.scalars(select(Case).where(Case.state != 'closed'))), network)
    state.last_run_at = stamp;state.last_success_at = stamp;state.last_error = None
    return more
