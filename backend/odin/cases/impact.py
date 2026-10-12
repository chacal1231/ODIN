"""Customer impact of a case, so level 1 reports and prioritizes by who is affected, not by arrival order.

Customers are counted as ONUs (SmartOLT), placed by OLT and zone; an OLT is placed in the network by the inventory
links that reach it (the router ports whose subnet holds an OLT address).
- confirmed: ONUs SmartOLT reports affected (an access case, or an access case open for an OLT behind this one);
- potential: ONUs behind a router interface or a router that failed, while their OLT reports nothing; an OLT that
  another link still reaches is named as such;
- indirect: an exit to the internet (transit, IXP, peering): traffic can leave by the other exits;
- service: an external service, its users; ODIN does not measure use per customer, the ONUs online are a reference.
Without SmartOLT or the inventory it says so instead of guessing.
"""
from collections import defaultdict
from sqlalchemy import select, func
from ..models import Case, Olt, OltPonPort, Onu
from ..inventory import roles

INDIRECT = {'transit', 'ixp', 'peering', 'cdn'}


def indirect(found):
    if found.get('role') == 'cdn':
        return {'kind': 'indirect', 'onus': 0, 'text': 'Indirecto: caché interna de CDN; mientras tanto ese contenido llega por internet.'}
    return {'kind': 'indirect', 'onus': 0, 'text': f'Indirecto: salida a internet por {found.get("provider") or "un proveedor"}; '
                                                   'los clientes salen por las otras salidas si las hay (ver servicios externos).'}


class Customers:
    """ONUs per OLT and the router ports that reach each OLT, read once per correlation pass."""

    def __init__(self, db, network):
        self.olts = {o.id: o for o in db.scalars(select(Olt).where(Olt.present.is_(True)))}
        self.onus, self.online = defaultdict(int), defaultdict(int)
        for olt_id, onus, online in db.execute(select(OltPonPort.olt_id, func.sum(OltPonPort.onus), func.sum(OltPonPort.online)).group_by(OltPonPort.olt_id)):
            self.onus[olt_id], self.online[olt_id] = int(onus or 0), int(online or 0)
        self.zones = defaultdict(list)
        for olt_id, zone in db.execute(select(Onu.olt_id, Onu.zone).where(Onu.present.is_(True), Onu.zone != '').distinct()):
            self.zones[olt_id].append(zone)
        self.reaching = {o.id: {('device:' + d, port) for d, port in network.links_to(o.device_id)} for o in self.olts.values() if o.device_id}
        self.down = {(c.entity_key, c.subject) for c in db.scalars(select(Case).where(Case.kind == 'interface', Case.state == 'active',
                                                                                     Case.last_state == 'down'))}
        self.access = {c.detail.get('olt_id'): c for c in db.scalars(select(Case).where(Case.kind == 'access', Case.state == 'active',
                                                                                      Case.last_state == 'down'))}

    def total_online(self):
        return sum(self.online.values())


def number(n):
    return f'{n:,}'.replace(',', '.')


def behind(customers, olt_ids, why):
    """Potential impact of OLTs behind a failure; confirmed for those whose own access case is open."""
    confirmed = [o for o in olt_ids if o in customers.access]
    olts = [{'id': o, 'name': customers.olts[o].name, 'onus': customers.onus[o], 'zones': sorted(customers.zones[o])[:10],
             'confirmed': o in confirmed, 'other_link': any(p not in customers.down for p in customers.reaching.get(o, ())) and why == 'port'}
            for o in sorted(olt_ids, key=lambda x: -customers.onus[x])]
    if confirmed:
        affected = sum(customers.access[o].detail.get('affected', 0) for o in confirmed)
        names = ', '.join(customers.olts[o].name for o in confirmed)
        return {'kind': 'confirmed', 'onus': affected, 'olts': olts,
                'text': f'Confirmado: {number(affected)} ONUs sin servicio en {names}, detrás de {"esta interfaz" if why == "port" else "este equipo"}'}
    total = sum(o['onus'] for o in olts)
    redundant = [o['name'] for o in olts if o['other_link']]
    return {'kind': 'potential', 'onus': total, 'olts': olts,
            'text': f'Potencial: {number(total)} ONUs detrás de {"esta interfaz" if why == "port" else "este equipo"} ('
                    + ', '.join(f'{o["name"]} {number(o["onus"])}' for o in olts[:5]) + ')'
                    + (f'; {", ".join(redundant)} sigue con otro enlace' if redundant else '') + '. SmartOLT no reporta caídas en ellas.'}


def compute(customers, case):
    if case.kind == 'access':
        d = case.detail
        if case.last_state != 'down':return {'kind': 'none', 'onus': 0, 'text': 'Sin ONUs afectadas: la OLT se recuperó.'}
        total = customers.onus.get(d.get('olt_id'), 0)
        return {'kind': 'confirmed', 'onus': d.get('affected', 0), 'zones': d.get('zones', []),
                'text': f'Confirmado: {number(d.get("affected", 0))} ONUs afectadas' + (f' de {number(total)} de la OLT' if total else '')
                        + (f' (zonas {", ".join(d["zones"])})' if d.get('zones') else '')}
    if case.kind == 'latency' and case.detail.get('scope') == 'external':
        verdict = (case.detail.get('verdict') or {}).get('state')
        online = customers.total_online()
        reference = f' (referencia: {number(online)} ONUs en línea)' if online else ''
        if verdict in ('own', 'exit'):
            return {'kind': 'all', 'onus': online, 'text': 'Todos los clientes que usan servicios por esa salida' + reference}
        return {'kind': 'service', 'onus': 0, 'text': f'Los clientes que usan {case.subject}; ODIN no mide el uso por cliente' + reference}
    if not customers.olts:
        return {'kind': 'unknown', 'onus': 0, 'text': 'Sin datos de clientes: conecta SmartOLT y el inventario para estimar el impacto.'}
    device = case.entity_key.removeprefix('device:') if case.entity_key.startswith('device:') else case.detail.get('device_id')
    if case.kind == 'interface':
        port = case.detail.get('port') or {}
        if port.get('role') in INDIRECT:return indirect(port)
        olts = [o for o, ports in customers.reaching.items() if (case.entity_key, case.subject) in ports]
        if olts:return behind(customers, olts, 'port')
    if case.kind == 'bgp_session':
        neighbor = case.detail.get('neighbor') or {}
        if neighbor.get('role') in INDIRECT:return indirect(neighbor)
        # Over an internal link, the customers at stake are those behind the router at the far end.
        device = neighbor.get('remote_device_id') or device
    if device:
        own = [o.id for o in customers.olts.values() if o.device_id == device]
        if own:return behind(customers, own, 'device')
        olts = [o for o, ports in customers.reaching.items() if any(key == 'device:' + device for key, _ in ports)]
        if olts:return behind(customers, olts, 'device')
    return {'kind': 'unknown', 'onus': 0, 'text': 'Sin clientes identificados detrás: el inventario no conecta este equipo con ninguna OLT.'}


def annotate(db, cases, network=None, customers=None):
    """Keeps each case's impact on its detail (read by the inbox, Telegram, the analyst and the case view)."""
    customers = customers or Customers(db, network or roles.Network(db))
    for case in cases:
        value = compute(customers, case)
        if case.detail.get('impact') != value:case.detail = {**case.detail, 'impact': value}
    return customers
