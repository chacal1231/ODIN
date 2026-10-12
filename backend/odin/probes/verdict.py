"""Verdict for external services: is it ours, the exit's, or the service's? The first answer a level-1 NOC needs.

Each external check leaves the network through an exit, read from its latest coherent trace: the hand-off port when
the inventory knows it, otherwise the first organization after the ISP's own (ODIN_ASN, or the one the traces start
in). Exits are organizations, not systems (asnames.py): EdgeUno's two systems are one exit. When the exit is the
destination's own organization (direct peering), the service and the exit are the same.
Actions are written for level 1, who watches and reports: level 2 deals with providers.
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
from . import asnames

RECENT = 2 * 3600
OWN_SPREAD = 2        # exits with most of their services degraded at once
OWN_SHARE = 0.5       # or this share of every service
EXIT_MIN = 2          # services degraded through one exit to blame it
EXIT_SHARE = 0.5      # of the services through it
AFFECTED = ('warning', 'critical')
LABELS = {'own': 'Problema propio o del origen', 'exit': 'Falla en una salida', 'service': 'Problema del servicio',
          'undetermined': 'Sin veredicto todavía', 'normal': 'Servicios externos normales', 'origin': 'El sondeador no mide'}
ACTIONS = {
    'own': 'Informar caída propia: afecta a {services}. La atiende N2 de red (borde, enlaces de salida, DNS o el servidor ODIN).',
    'exit': 'Informar falla en la salida {exit}: afecta a {services}. N2 la gestiona con el proveedor.',
    'direct': 'Informar falla hacia {exit} (interconexión directa): afecta a {services}. N2 revisa la interconexión y, si está bien, '
              'la gestiona con {exit}.',
    'service': 'Informar que {service} presenta fallas propias: la red está bien. No requiere N2.',
    'undetermined': 'Vigilar. Si se suman servicios o llega la traza, ODIN actualiza el veredicto.',
}


def listing(services, limit=5):
    return ', '.join(services[:limit]) + (f' y {len(services) - limit} más' if len(services) > limit else '')


def own_asn(paths):
    """ODIN_ASN, or the autonomous system most traces start in."""
    if settings().asn:return settings().asn
    first = Counter(next((h['asn'] for h in hops if h.get('asn')), None) for hops in paths)
    first.pop(None, None)
    return first.most_common(1)[0][0] if first else None


def exit_of(trace, own_org, info):
    """Where a path leaves the network: hand-off port, or first foreign organization after the own one."""
    handoff = (trace.path or {}).get('handoff')
    if handoff:
        return {'key': 'port:%s:%s' % (handoff['device_id'], handoff['interface']), 'label': handoff.get('label') or handoff['interface'],
                'org': None}
    started = False
    for hop in trace.hops:
        asn = hop.get('asn')
        if not asn:continue
        org = info[asn]['org']
        if org == own_org:started = True
        elif started or own_org is None:
            return {'key': org, 'label': info[asn]['label'], 'org': org}
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
    references = {c['id']: (c['catalog_metadata'] or {}).get('asn_reference') for c in checks}
    info = asnames.lookup(db, {h.get('asn') for t in traces.values() for row in t for h in row.hops} | set(references.values()) | {own})
    # A network the registry has not named yet takes the name of the catalog service it hosts.
    for c in checks:
        asn = references[c['id']]
        if asn and c['service'] and info[asn]['label'] == 'AS%d' % asn:info[asn]['label'] = c['service']
    own_org = info[own]['org'] if own else None
    service_of = {c['id']: c['service'] or c['name'] for c in checks}
    exits, route_changes, degraded, healthy = {}, {}, set(), set()
    for check in checks:
        service = service_of[check['id']]
        if check['state'] in AFFECTED:degraded.add(service)
        elif check['state'] == 'healthy':healthy.add(service)
        else:continue
        latest = traces.get(check['id'], [])
        found = exit_of(latest[0], own_org, info) if latest else None
        if not found:continue
        row = exits.setdefault(found['key'], {**found, 'services': set(), 'degraded': set(),
                                              'direct': set()})
        row['services'].add(service)
        if check['state'] in AFFECTED:row['degraded'].add(service)
        if found['org'] and references[check['id']] and found['org'] == info[references[check['id']]]['org']:row['direct'].add(service)
        if len(latest) == 2 and latest[0].fingerprint != latest[1].fingerprint and check['state'] in AFFECTED:
            before = exit_of(latest[1], own_org, info)
            if before and before['key'] != found['key']:
                route_changes[service] = {'before': before['label'], 'after': found['label'], 'at': latest[0].finished_at}
    total = degraded | healthy  # a service with one degraded check counts as degraded
    result.update(own_asn=own, own_label=info[own]['label'] if own else None, exits=sorted(({'key': k, 'label': e['label'], 'services': len(e['services']),
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
                         'action': ACTIONS['own'].format(services=listing(sorted(degraded)))})
        explained = set(degraded)
    else:
        for e in bad.values():
            # Every service through it is the exit's own network: a direct interconnection, so it is that provider or the link to it.
            direct = e['direct'] == e['services']
            findings.append({'state': 'exit', 'exit': e['label'], 'direct': direct, 'services': sorted(e['degraded']),
                             'title': f'{LABELS["exit"]}: {len(e["degraded"])} de {len(e["services"])} servicios que salen por {e["label"]}'
                                      + (' (interconexión directa)' if direct else '') + ' están degradados',
                             'action': ACTIONS['direct' if direct else 'exit'].format(exit=e['label'], services=listing(sorted(e['degraded'])))})
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
