"""One equipment, many names: join the identities each source uses for the same box.

Links, strongest first: an identity confirmed in the inventory; the same sending address (two log
names sent from one IP); the same exact name. Only confirmed links
reach the inventory; the other two are reported as probable. Two inventory devices are never
joined through a probable link: the conflict keeps them apart.
"""
from dataclasses import dataclass, field
from sqlalchemy import select
from ..models import SourceIdentity, Device, LogSender

CONFIRMED, SAME_ADDRESS, SAME_NAME = 'confirmed', 'same_address', 'same_name'
EXPLAIN = {CONFIRMED: 'vínculo confirmado en el inventario', SAME_ADDRESS: 'misma IP de envío', SAME_NAME: 'mismo nombre'}
SOURCES = {'graylog': 'graylog'}
RANK = {'self': 0, SAME_NAME: 1, SAME_ADDRESS: 2, CONFIRMED: 3}


@dataclass
class Entity:
    key: str
    label: str
    device_id: str | None
    members: dict = field(default_factory=dict)   # node -> how it joined ('confirmed', 'same_address', 'same_name', 'self')

    def names(self, kind):
        return sorted(name for (k, name) in self.members if k == kind)


class Resolver:
    def __init__(self, db):
        self.parent = {};self.how = {}
        devices = {d.id: d for d in db.scalars(select(Device).where(Device.archived.is_(False)))}
        nodes = {}
        for row in db.scalars(select(LogSender)):nodes[('graylog', row.name)] = row.address
        for node in nodes:self.find(node)
        for identity in db.scalars(select(SourceIdentity).where(SourceIdentity.source.in_(list(SOURCES)))):
            node = (SOURCES[identity.source], identity.external_id)
            if identity.device_id in devices:self.join(node, ('device', identity.device_id), CONFIRMED)
        by_address, by_name = {}, {}
        for node, address in nodes.items():
            if address:by_address.setdefault(address, []).append(node)
            by_name.setdefault(node[1].casefold(), []).append(node)
        for groups, how in ((by_address, SAME_ADDRESS), (by_name, SAME_NAME)):
            for members in groups.values():
                for node in members[1:]:self.join(members[0], node, how)
        self.entities = {}
        for node in list(self.parent):
            root = self.find(node)
            entity = self.entities.get(root)
            if not entity:
                entity = self.entities[root] = Entity(key='', label='', device_id=None)
            if node[0] == 'device':
                entity.device_id = node[1]
            else:
                entity.members[node] = self.how.get(node, 'self')
        for entity in self.entities.values():
            device = devices.get(entity.device_id)
            names = sorted(n for _, n in entity.members)
            entity.key = 'device:' + entity.device_id if device else 'name:' + (names[0] if names else '?')
            entity.label = device.name if device else (names[0] if names else '?')

    def find(self, node):
        self.parent.setdefault(node, node)
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]];node = self.parent[node]
        return node

    def device_of(self, root):
        return [n for n in self.parent if n[0] == 'device' and self.find(n) == root]

    def join(self, a, b, how):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:return
        # Never let a probable link join two different inventory devices.
        if how != CONFIRMED and self.device_of(ra) and self.device_of(rb):return
        self.parent[rb] = ra
        for node in (a, b):
            # Each name keeps the strongest link it took part in.
            if node[0] != 'device' and RANK[how] > RANK[self.how.get(node, 'self')]:
                self.how[node] = how

    def entity(self, kind, name):
        node = (kind, name)
        if node not in self.parent:
            return Entity(key='name:' + name, label=name, device_id=None, members={node: 'self'})
        return self.entities[self.find(node)]

    def by_device(self, device_id):
        node = ('device', device_id)
        return self.entities.get(self.find(node)) if node in self.parent else None

    def explain(self, entity):
        """Human-readable account of how the names were joined."""
        parts = []
        for (kind, name), how in sorted(entity.members.items()):
            label = {'graylog': 'logs'}[kind]
            parts.append(f'{label} {name}' + ('' if how == 'self' else f' ({EXPLAIN[how]})'))
        return '; '.join(parts)
