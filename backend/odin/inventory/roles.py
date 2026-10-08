"""What each interface and BGP neighbor is, read from the network itself.

The NOC describes each router port as ROLE-PROVIDER-DETAIL (TRANSITO-CARRIER-100G, TRANS-TX-POP-NORTE-RUTA-01,
PIT-IXP, PNI-CDN-01-100G, PTP-PROV) or, for customers, «CUST: name ... {DIA:DIA-0001 | ASN:NA | CT:ISP}» or
«DIA-0009-name». The provider of a transport is not always the second word (TRANSPORTE-CENTRO-NORTE-INTXA), so
transports are shown with their whole description.
LibreNMS supplies the descriptions (ifAlias) and the port addresses with their prefixes; a BGP neighbor is
the address that falls in the subnet of one of those ports. A neighbor that is an address of another device
of the inventory is an internal link between two own routers (over a carrier's transport, for instance).
"""
import ipaddress
import re
from dataclasses import dataclass
from sqlalchemy import select
from ..models import Device, InventoryInterface, SourceIdentity

ROLES = {'TRANSITO': 'transit', 'TRANSPORTE': 'transport', 'TRANS': 'transport', 'PIT': 'ixp', 'IXP': 'ixp',
         'PTP': 'peering', 'PEERING': 'peering', 'PNI': 'peering', 'CUST': 'customer',
         'CDN': 'cdn', 'OCA': 'cdn'}  # OCA, OCA01...: Netflix Open Connect caches inside the network
LABELS = {'transit': 'tránsito', 'transport': 'transporte', 'ixp': 'IXP', 'peering': 'peering', 'customer': 'cliente', 'cdn': 'CDN interno'}
CUSTOMER = re.compile(r'\{\s*DIA:\s*([^|}]*)\|\s*ASN:\s*([^|}]*)', re.IGNORECASE)


def classify(description):
    """(role, provider, details) from a port description; role None when it follows no known convention."""
    text = (description or '').strip()
    first = re.split(r'[-:\s_]', text, maxsplit=1)[0].upper()
    found = CUSTOMER.search(text)
    coded = re.match(r'(DIA-\d+)-?(.*)', text, re.IGNORECASE)
    if first == 'CUST' or found or coded:
        rest = text.split(':', 1)[1] if first == 'CUST' else coded.group(2) if coded and not found else text
        name = re.split(r'[(\[{]', rest, maxsplit=1)[0].strip(' -')
        details = ({'dia': found.group(1).strip(), 'asn': re.sub(r'^AS', '', found.group(2).strip(), flags=re.IGNORECASE)} if found else
                   {'dia': coded.group(1).upper()} if coded else {})
        return 'customer', name[:120], {k: v for k, v in details.items() if v and v.upper() != 'NA'}
    role = ROLES.get(re.sub(r'^OCA\d+$', 'OCA', first))
    if not role:return None, '', {}
    parts = [p for p in re.split(r'[-_\s]+', text)[1:] if p]
    return role, (parts[0].upper() if parts else ''), {}


@dataclass
class Port:
    device_id: str
    device_name: str
    pop_id: str | None
    name: str
    description: str
    network: ipaddress.IPv4Interface | ipaddress.IPv6Interface


class Network:
    """Port subnets of the inventory, to resolve BGP neighbors."""

    def __init__(self, db):
        devices = {d.id: d for d in db.scalars(select(Device).where(Device.archived.is_(False)))}
        self.ports = []
        self.descriptions = {}
        rows = db.execute(select(InventoryInterface, SourceIdentity.device_id).join(SourceIdentity, SourceIdentity.id == InventoryInterface.identity_id)
            .where(InventoryInterface.kind == 'port', InventoryInterface.present.is_(True), SourceIdentity.device_id.is_not(None))).all()
        for port, device_id in rows:
            device = devices.get(device_id)
            if not device:continue
            self.descriptions[(device.id, port.name)] = port.description
            for value in port.addresses or []:
                try:network = ipaddress.ip_interface(value)
                except ValueError:continue
                if network.network.prefixlen == network.max_prefixlen:continue  # a loopback or host route is no link
                self.ports.append(Port(device.id, device.name, device.pop_id, port.name, port.description, network))
        self.owner = {p.network.ip: p for p in self.ports}

    def neighbor(self, address, device_id=None):
        """The local port whose subnet holds the neighbor (on `device_id` when given), its role and, for an internal link, the far end."""
        try:ip = ipaddress.ip_address(address)
        except ValueError:return None
        candidates = [p for p in self.ports if ip in p.network.network and ip != p.network.ip]
        # Prefer the router of the case; its logs may come from a device not yet reconciled with LibreNMS.
        candidates = [p for p in candidates if p.device_id == device_id] or candidates
        if not candidates:return None
        port = max(candidates, key=lambda p: p.network.network.prefixlen)  # the most specific subnet
        role, provider, details = classify(port.description)
        far = self.owner.get(ip)
        internal = far and far.device_id != port.device_id
        return {'device_id': port.device_id, 'device_name': port.device_name, 'pop_id': port.pop_id, 'interface': port.name,
                'description': port.description, 'network': str(port.network.network), 'role': role, 'provider': provider, 'details': details,
                'remote_device_id': far.device_id if internal else None, 'remote_device_name': far.device_name if internal else None,
                'remote_interface': far.name if internal else None}


    def port(self, device_id, name):
        """What one port of a device is, from its description; None when the inventory does not know the port."""
        description = self.descriptions.get((device_id, name))
        if description is None:return None
        role, provider, details = classify(description)
        return {'role': role, 'provider': provider, 'details': details, 'description': description}

    def links_to(self, device_id):
        """Ports of other devices whose subnet holds an address of `device_id`: the links that reach it."""
        mine = [p.network.ip for p in self.ports if p.device_id == device_id]
        return {(p.device_id, p.name) for p in self.ports if p.device_id != device_id and any(ip in p.network.network for ip in mine)}


def summary(found):
    """What is kept on a case: enough to set its severity, relate it and explain it."""
    if not found:return None
    return {'role': found['role'], 'label': 'enlace interno' if found['remote_device_id'] else LABELS.get(found['role']),
            'interface': found['interface'], 'device_id': found['device_id'], 'remote_device_id': found['remote_device_id'],
            'remote_device_name': found['remote_device_name'], 'summary': describe(found)}


CRITICAL = ('transit', 'transport')  # and internal links: they carry many customers at once


def critical(found):
    return bool(found) and (bool(found.get('remote_device_id')) or found.get('role') in CRITICAL)


def describe(found):
    """One sentence for people and for the analyst."""
    if not found:return ''
    where = f'interfaz {found["interface"]} de {found["device_name"]} ({found["network"]}, «{found["description"]}»)'
    if found['remote_device_name']:
        return (f'enlace interno con {found["remote_device_name"]} ({found["remote_interface"]})'
                + (f' por {LABELS.get(found["role"], found["role"])}' if found['role'] else '') + f'; {where}')
    if not found['role']:
        return f'{where}; la descripción no sigue la convención de roles'
    extra = ''.join(f', {k.upper()} {v}' for k, v in found['details'].items())
    name = '' if found['role'] == 'transport' else f' {found["provider"]}'
    return f'{LABELS[found["role"]]}{name}{extra}; {where}'
