"""Setup assistant: step states, providers read from port descriptions, and suggested checks."""
import ipaddress
from sqlalchemy import select
from odin.db import session
from odin.models import Integration, ProbeTarget, SourceIdentity, InventoryInterface, now
from odin.security import encrypt
from odin.setup import far_end, providers, suggestions, steps
from test_access import enroll
from test_traces import inventory


def add_port(db, device_id, name, description, address):
    identity = db.scalar(select(SourceIdentity).where(SourceIdentity.device_id == device_id))
    db.add(InventoryInterface(identity_id=identity.id, external_id=name, name=name, kind='port', addresses=[address], description=description, last_seen=1))


def test_far_end_only_when_unambiguous():
    cases = {'198.51.100.133/30': '198.51.100.134', '10.0.0.0/31': '10.0.0.1', '10.0.0.1/31': '10.0.0.0', '2001:db8::1/127': '2001:db8::',
             '2001:db8::1/126': None, '10.0.0.1/29': None, '10.0.0.1/24': None}
    assert {k: far_end(ipaddress.ip_interface(k)) for k in cases} == cases


def test_providers_are_grouped_from_port_descriptions():
    with session() as db:
        ids = inventory(db)
        add_port(db, ids['PE-A'], 'Ethernet52/1', 'TRANSITO-CARRIER-100G-B', '198.51.100.137/30')
        add_port(db, ids['PE-A'], 'Ethernet3', 'PIT-IXPBOG', '200.0.0.10/24')
        add_port(db, ids['PE-A'], 'Ethernet4', 'CUST: Acme {DIA:DIA-0001 | ASN:NA | CT:ISP}', '190.0.0.1/30')
        add_port(db, ids['PE-A'], 'Ethernet5', 'enlace a bogota', '10.9.9.1/30')
        db.commit()
        found = providers(db)
    by = {(g['role'], g['provider']): len(g['ports']) for g in found['groups']}
    assert by == {('transit', 'CARRIER'): 2, ('ixp', 'IXPBOG'): 1}
    # The PE-A ↔ PE-B transport is an internal link; the customer is only counted; the unnamed link needs a description.
    assert (found['internal'], found['customers'], found['unclassified_count']) == (2, 1, 2)
    assert {u['interface'] for u in found['unclassified']} == {'Ethernet1', 'Ethernet5'}


def test_suggestions_and_apply(client, administrator):
    with session() as db:
        ids = inventory(db)
        for identity in db.scalars(select(SourceIdentity)):
            identity.addresses = ['10.255.0.1'] if identity.device_id == ids['PE-A'] else ['10.255.0.2']
        db.commit()
        found = suggestions(db)
    assert [(s['kind'], s['address'], s['name']) for s in found] == [
        ('device', '10.255.0.1', 'PE-A'), ('device', '10.255.0.2', 'PE-B'),
        ('carrier', '198.51.100.134', 'Tránsito CARRIER · PE-A Ethernet51/1')]
    enroll(client)
    result = client.post('/api/v1/setup/suggestions', json={'items': [{'address': s['address'], 'name': s['name']} for s in found]
                                                             + [{'address': '8.8.8.8', 'name': 'No sugerido'}]}).json()
    assert result['created'] == ['10.255.0.1', '10.255.0.2', '198.51.100.134']
    assert result['failed'] == [{'address': '8.8.8.8', 'error': 'Ya no es una sugerencia vigente'}]
    with session() as db:
        rows = {t.name: t for t in db.scalars(select(ProbeTarget))}
        assert rows['PE-A'].enabled and rows['PE-A'].categories == ['red_propia'] and rows['PE-A'].pop_id
        assert rows['Tránsito CARRIER · PE-A Ethernet51/1'].profile_version_id == '00000000-0000-4000-8000-000000000014'
        assert suggestions(db) == []  # already monitored


def test_steps_follow_what_odin_holds(client, administrator):
    with session() as db:
        first = {s['id']: s['state'] for s in steps(db)}
        assert first == {'librenms': 'pending', 'providers': 'pending', 'zabbix': 'pending', 'graylog': 'pending', 'smartolt': 'pending',
                         'checks': 'pending', 'telegram': 'pending', 'model': 'pending'}
        inventory(db)
        db.get(Integration, 'librenms').encrypted_secret = encrypt('k')
        db.get(Integration, 'librenms').allowed_networks = ['10.0.0.0/8']
        db.get(Integration, 'librenms').checked_at = db.get(Integration, 'librenms').synced_at = now()
        db.commit()
        later = {s['id']: s for s in steps(db)}
    assert later['librenms']['state'] == 'done' and later['librenms']['devices'] == 2 and later['providers']['state'] == 'done'
    enroll(client)
    data = client.get('/api/v1/setup').json()
    assert data['total'] == 8 and data['done'] == 2 and not data['required_done']
    assert client.get('/api/v1/setup/providers').status_code == 200
