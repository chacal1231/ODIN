"""Verdict for external services: is it ours, the exit's, or the service's? The first answer a level-1 NOC needs.

Each external check leaves the network through an exit, read from its latest coherent trace: the hand-off port when
the inventory knows it, otherwise the first autonomous system after the ISP's own (ODIN_ASN, or the one the traces
start in). When the exit is the destination's own network (direct peering), the service and the exit are the same.
Degraded services (any check in warning or critical) are then compared with the healthy ones:
- own: degraded through several exits at once, or half of every service: the cause is before the exits (the own
  network or the probes' origin);
- exit: most services leaving through one exit degrade while the others do not: that transit or peer;
- service: one service degrades while others through its same exit are fine: the service itself;
- undetermined: no trace, or no other service through the same exit to compare with.
It is a reading of what ODIN measures from one vantage point, never a confirmed root cause; a route change seen by the
tracer is reported beside it.
"""
from collections import Counter
from sqlalchemy import select
from ..config import settings
from ..models import ProbeTarget, ProbeTrace, now
from .trace import consistent

RECENT = 2 * 3600
OWN_SPREAD = 2        # exits with most of their services degraded at once
OWN_SHARE = 0.5       # or this share of every service
EXIT_MIN = 2          # services degraded through one exit to blame it
EXIT_SHARE = 0.5      # of the services through it
AFFECTED = ('warning', 'critical')
# Names for the networks seen as exits; catalog providers name the rest.
NAMES = {7195: 'EdgeUno', 174: 'Cogent', 3356: 'Lumen', 1299: 'Arelion', 6939: 'Hurricane Electric', 3257: 'GTT', 6762: 'Sparkle',
         12956: 'Telxius', 23520: 'Columbus Networks', 52320: 'GlobeNet', 15169: 'Google', 13335: 'Cloudflare', 32934: 'Meta',
         20940: 'Akamai', 16625: 'Akamai', 16509: 'Amazon', 14618: 'Amazon', 8075: 'Microsoft', 6507: 'Riot', 32590: 'Valve',
         2906: 'Netflix', 40027: 'Netflix', 19551: 'Imperva', 209242: 'Cloudflare', 54113: 'Fastly', 3549: 'Lumen', 6461: 'Zayo'}
LABELS = {'own': 'Problema propio o del origen', 'exit': 'Falla en una salida', 'service': 'Problema del servicio',
          'undetermined': 'Sin veredicto todavía', 'normal': 'Servicios externos normales', 'origin': 'El sondeador no mide'}
ACTIONS = {
    'own': 'Revisar la red propia: PE de borde, enlaces de salida, DNS y la conexión del servidor ODIN. Escalar a N2 de red.',
    'exit': 'Abrir ticket con {exit} y avisar en el grupo de proveedores. Si hay otra salida, N2 puede desviar el tráfico.',
    'direct': 'Revisar la interconexión directa con {exit} (puerto, IXP o PNI). Si está bien, la falla es de {exit}: informar al cliente.',
    'service': 'No es la red: informar al cliente que {service} presenta fallas. No escalar.',
    'undetermined': 'Vigilar. Si se suman servicios o llega la traza, ODIN actualiza el veredicto.',
}


def own_asn(paths):
    """ODIN_ASN, or the autonomous system most traces start in."""
    if settings().asn:return settings().asn
    first = Counter(next((h['asn'] for h in hops if h.get('asn')), None) for hops in paths)
    first.pop(None, None)
    return first.most_common(1)[0][0] if first else None


def exit_of(trace, own, names):
    """Where a path leaves the network: hand-off port, or first foreign AS after the own one."""
    handoff = (trace.path or {}).get('handoff')
    if handoff:
        return {'key': 'port:%s:%s' % (handoff['device_id'], handoff['interface']), 'label': handoff.get('label') or handoff['interface'],
                'asn': None}
    started = False
    for hop in trace.hops:
        asn = hop.get('asn')
        if asn == own:started = True
        elif asn and (started or own is None):
            return {'key': 'as:%d' % asn, 'label': names.get(asn) or 'AS%d' % asn, 'asn': asn}
    return None


def comparable(trace):
    return trace.status == 'done' and consistent(trace.hops) and (trace.reached or (trace.path or {}).get('network_reached'))


def paths(db, ids, stamp):
    """The two latest comparable traces of each check, newest first."""
    rows = db.scalars(select(ProbeTrace).where(ProbeTrace.target_id.in_(list(ids)), ProbeTrace.status == 'done',
                                               ProbeTrace.finished_at > stamp - RECENT).order_by(ProbeTrace.finished_at.desc()))
    result = {}
    for row in rows:
        if comparable(row) and len(result.setdefault(row.target_id, [])) < 2:result[row.target_id].append(row)
    return result


def compute(db, views=None, stamp=None, origin_healthy=True):
    """`views`: target views of the checks (from target_views); external ones are read."""
    stamp = stamp or now()
    if views is None:
        from .service import target_views
        views = target_views(db, list(db.scalars(select(ProbeTarget).where(ProbeTarget.archived.is_(False), ProbeTarget.enabled.is_(True)))), stamp)
    checks = [v for v in views if v['enabled'] and not v['archived'] and 'red_propia' not in (v['categories'] or [])]
    result = {'state': 'normal', 'findings': [], 'exits': [], 'own_asn': None, 'at': stamp}
    if not origin_healthy:
        return {**result, 'state': 'origin', 'title': LABELS['origin'], 'action': 'Revisar la salud del sondeador antes de leer servicios externos.'}
    traces = paths(db, [c['id'] for c in checks], stamp)
    own = own_asn([t[0].hops for t in traces.values()])
    names = {**{(c['catalog_metadata'] or {}).get('asn_reference'): c['service'] for c in checks if c['service']}, **NAMES}
    service_of = {c['id']: c['service'] or c['name'] for c in checks}
    exits, route_changes, degraded, healthy = {}, {}, set(), set()
    for check in checks:
        service = service_of[check['id']]
        if check['state'] in AFFECTED:degraded.add(service)
        elif check['state'] == 'healthy':healthy.add(service)
        else:continue
        latest = traces.get(check['id'], [])
        found = exit_of(latest[0], own, names) if latest else None
        if not found:continue
        row = exits.setdefault(found['key'], {**found, 'services': set(), 'degraded': set(),
                                              'direct': set()})
        row['services'].add(service)
        if check['state'] in AFFECTED:row['degraded'].add(service)
        if found['asn'] and found['asn'] == (check['catalog_metadata'] or {}).get('asn_reference'):row['direct'].add(service)
        if len(latest) == 2 and latest[0].fingerprint != latest[1].fingerprint and check['state'] in AFFECTED:
            before = exit_of(latest[1], own, names)
            if before and before['key'] != found['key']:
                route_changes[service] = {'before': before['label'], 'after': found['label'], 'at': latest[0].finished_at}
    total = degraded | healthy  # a service with one degraded check counts as degraded
    result.update(own_asn=own, exits=sorted(({'key': k, 'label': e['label'], 'asn': e['asn'], 'services': len(e['services']),
                                              'degraded': sorted(e['degraded'])} for k, e in exits.items()), key=lambda e: -e['services']),
                  degraded=sorted(degraded), services=len(total))
    if not degraded:
        return {**result, 'title': LABELS['normal'], 'action': ''}
    bad = {k: e for k, e in exits.items() if len(e['degraded']) >= EXIT_MIN and len(e['degraded']) / len(e['services']) >= EXIT_SHARE}
    findings, explained = [], set()
    if len(bad) >= OWN_SPREAD or (len(total) and len(degraded) / len(total) >= OWN_SHARE and len(degraded) >= 3):
        findings.append({'state': 'own', 'services': sorted(degraded), 'exits': sorted(e['label'] for e in bad.values()),
                         'title': f'{LABELS["own"]}: {len(degraded)} de {len(total)} servicios degradados'
                                  + (f' por {len(bad)} salidas distintas' if len(bad) >= OWN_SPREAD else ''),
                         'action': ACTIONS['own']})
        explained = set(degraded)
    else:
        for e in bad.values():
            # Every service through it is the exit's own network: a direct interconnection, so it is that provider or the link to it.
            direct = e['direct'] == e['services']
            findings.append({'state': 'exit', 'exit': e['label'], 'asn': e['asn'], 'direct': direct, 'services': sorted(e['degraded']),
                             'title': f'{LABELS["exit"]}: {len(e["degraded"])} de {len(e["services"])} servicios que salen por {e["label"]}'
                                      + (' (interconexión directa)' if direct else '') + ' están degradados',
                             'action': ACTIONS['direct' if direct else 'exit'].format(exit=e['label'])})
            explained |= e['degraded']
    for service in sorted(degraded - explained):
        through = [e for e in exits.values() if service in e['services']]
        peers = {s for e in through for s in e['services'] - e['degraded'] if s != service}
        if through and peers:
            labels = ', '.join(sorted({e['label'] for e in through}))
            findings.append({'state': 'service', 'service': service, 'exit': labels, 'services': [service],
                             'title': f'{LABELS["service"]} {service}: {len(peers)} servicios más por la misma salida ({labels}) están normales',
                             'action': ACTIONS['service'].format(service=service)})
        else:
            findings.append({'state': 'undetermined', 'service': service, 'services': [service],
                             'title': f'{service} degradado: ' + ('sin traza reciente para saber por dónde sale' if not through else
                                                                 'ningún otro servicio sale por el mismo lugar para comparar'),
                             'action': ACTIONS['undetermined']})
    for finding in findings:
        changes = {s: route_changes[s] for s in finding['services'] if s in route_changes}
        if changes:finding['route_changes'] = changes
    order = ['own', 'exit', 'service', 'undetermined']
    findings.sort(key=lambda f: order.index(f['state']))
    return {**result, 'state': findings[0]['state'], 'title': findings[0]['title'], 'action': findings[0]['action'], 'findings': findings}


def for_service(verdict, service):
    """The finding that explains one service, for its case."""
    return next((f for f in verdict.get('findings', []) if service in f.get('services', [])), None)
