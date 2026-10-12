from sqlalchemy import select
from odin.db import session
from odin.models import Case, Device, InventoryInterface, Olt, OltOutage, OltPonPort, SourceIdentity, uid
from odin.cases import correlator, impact
from odin.inventory.roles import Network
from odin.inbox import build
from test_traces import inventory

T = 1_800_000_000


def network_with_olt(db, addresses=('10.0.0.2/30',)):
    """PE-A's Ethernet1 (10.0.0.1/30) reaches OLT-NORTE, with 512 ONUs, 500 online."""
    ids = inventory(db)
    device = Device(name='OLT-NORTE');db.add(device);db.flush()
    identity = SourceIdentity(source='librenms', external_id='olt', device_id=device.id, name='OLT-NORTE', addresses=[], details={}, last_seen=1)
    db.add(identity);db.flush()
    for n, address in enumerate(addresses):
        db.add(InventoryInterface(identity_id=identity.id, external_id=str(n), name=f'mgmt{n}', kind='port', addresses=[address], description='', last_seen=1))
    olt = Olt(id='7', name='OLT-NORTE', device_id=device.id, first_seen=T, last_seen=T, pop_manual=False, reachable=True, present=True)
    db.add(olt);db.flush()
    for port in range(4):
        db.add(OltPonPort(olt_id='7', board='0', port=str(port), onus=128, online=125, last_seen=T))
    db.flush()
    return ids, olt


def case(db, kind, entity_key, subject, **detail):
    row = Case(kind=kind, key=uid(), entity_key=entity_key, entity_label='x', subject=subject, detail=detail, opened_at=T, last_activity_at=T,
               last_state='down', last_state_at=T, state='active', rule_version=4)
    db.add(row);db.flush();return row


def read(db, row):
    return impact.compute(impact.Customers(db, Network(db)), row)


def test_interface_reaching_an_olt_is_potential_then_confirmed():
    with session() as db:
        ids, olt = network_with_olt(db)
        port = case(db, 'interface', 'device:' + ids['PE-A'], 'Ethernet1')
        result = read(db, port)
        assert result['kind'] == 'potential' and result['onus'] == 512
        assert result['text'] == 'Potencial: 512 ONUs detrás de esta interfaz (OLT-NORTE 512). SmartOLT no reporta caídas en ellas.'
        # SmartOLT reports the OLT's PONs down: the impact is confirmed with its affected count.
        case(db, 'access', 'device:' + olt.device_id, 'OLT-NORTE', olt_id='7', affected=300)
        result = read(db, port)
        assert result['kind'] == 'confirmed' and result['onus'] == 300
        assert result['text'] == 'Confirmado: 300 ONUs sin servicio en OLT-NORTE, detrás de esta interfaz'


def test_olt_with_another_link_and_router_behind_internal_link():
    with session() as db:
        # A second OLT address in the PE-A Ethernet2 / PE-B Ethernet9 subnet: two more links reach it.
        ids, _ = network_with_olt(db, ('10.0.0.2/30', '10.0.1.3/30'))
        result = read(db, case(db, 'interface', 'device:' + ids['PE-A'], 'Ethernet1'))
        assert result['text'].endswith('; OLT-NORTE sigue con otro enlace. SmartOLT no reporta caídas en ellas.')
        assert result['olts'][0]['other_link']
        # A BGP session over an internal link puts at stake the customers behind the far router.
        bgp = case(db, 'bgp_session', 'device:' + ids['PE-B'], '10.0.1.1', neighbor={'remote_device_id': ids['PE-A'], 'role': 'transport'})
        assert read(db, bgp)['text'].startswith('Potencial: 512 ONUs detrás de este equipo')


def test_exits_external_services_and_missing_data():
    with session() as db:
        result = read(db, case(db, 'interface', 'device:x', 'Ethernet9'))
        assert result == {'kind': 'unknown', 'onus': 0, 'text': 'Sin datos de clientes: conecta SmartOLT y el inventario para estimar el impacto.'}
        ids, _ = network_with_olt(db)
        transit = case(db, 'interface', 'device:' + ids['PE-A'], 'Ethernet51/1', port={'role': 'transit', 'provider': 'CARRIER'})
        assert read(db, transit)['text'].startswith('Indirecto: salida a internet por CARRIER')
        service = case(db, 'latency', 'service:Netflix', 'Netflix', scope='external', verdict={'state': 'service'})
        assert read(db, service)['text'] == 'Los clientes que usan Netflix; ODIN no mide el uso por cliente (referencia: 500 ONUs en línea)'
        service.detail = {**service.detail, 'verdict': {'state': 'exit'}}
        assert read(db, service)['kind'] == 'all'


def test_correlator_annotates_and_inbox_puts_most_customers_first():
    with session() as db:
        _, olt = network_with_olt(db)
        small = Olt(id='8', name='OLT-SUR', first_seen=T, last_seen=T, pop_manual=False, reachable=True, present=True);db.add(small);db.flush()
        for olt_id, affected, start in (('8', 10, T), ('7', 200, T + 60)):
            db.add(OltOutage(id=uid(), olt_id=olt_id, board='0', port='1', cause='offline', zone='Z', subscribers=128, affected=affected,
                             odbs=[], active=True, started_at=start, first_seen=start, last_seen=start))
        db.commit()
        correlator.run(db, T + 120);db.commit()
        cases = {c.subject: c for c in db.scalars(select(Case).where(Case.kind == 'access'))}
        assert cases['OLT-NORTE'].detail['impact']['text'] == 'Confirmado: 200 ONUs afectadas de 512 de la OLT (zonas Z)'
        # The older warning comes second: more customers affected goes first.
        rows = [i['title'] for i in build(db, T + 120)['attention'] if i['kind'] == 'case']
        assert rows == ['Acceso OLT-NORTE', 'Acceso OLT-SUR']
        # A pass without changes rewrites nothing.
        before = dict(cases['OLT-NORTE'].detail)
        correlator.run(db, T + 180);db.commit();db.refresh(cases['OLT-NORTE'])
        assert cases['OLT-NORTE'].detail == before
