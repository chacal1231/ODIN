from sqlalchemy import select
from odin.db import session
from odin.models import (Case, CaseEvidence, Device, InventoryInterface, Olt, OltOutage, OltUplink, Pop, SourceIdentity, now, uid)
from odin.cases import correlator
from odin.cases.api import related_cases
from odin.cases.entities import Resolver
from odin.cases.summary import alerting
from odin.analyst.packs import case_pack
from odin.inbox import build
from test_traces import inventory

T = 1_800_000_000


def olt(db, name='OLT-NORTE', **kwargs):
    row = Olt(id=uid()[:8], name=name, first_seen=T, last_seen=T, pop_manual=False, present=True, **{'reachable': True, **kwargs})
    db.add(row);db.flush();return row


def outage(db, o, port, cause='los', affected=20, started=T, zone='Norte', active=True):
    row = OltOutage(id=uid(), olt_id=o.id, board='0', port=str(port), cause=cause, zone=zone, subscribers=32, affected=affected,
                    odbs=['S1', 'S2'], active=active, started_at=started, first_seen=started, last_seen=started)
    db.add(row);db.flush();return row


def access(db):
    return db.scalar(select(Case).where(Case.kind == 'access'))


def test_single_pon_cut_is_reported_without_alerting():
    with session() as db:
        o = olt(db);outage(db, o, 1);db.commit()
        correlator.run(db, T + 60);db.commit()
        case = access(db)
        assert case.severity == 'warning' and case.last_state == 'down' and case.drops == 1
        assert case.detail['reading']['title'] == 'Corte de fibra en el PON 0/1: 20 de 32 ONUs (zona Norte, splitters S1, S2)'
        assert case.detail['reading']['action'] == 'Informar corte de fibra en Norte. N2 despacha a campo.'
        assert case not in alerting(db)
        # Level 1 still sees it, as the case and not as a loose outage.
        attention = build(db, T + 60)['attention']
        assert [i['kind'] for i in attention if i['kind'] in ('case', 'olt_outage')] == ['case']
        found = next(i for i in attention if i['kind'] == 'case')
        assert found['title'] == 'Acceso OLT-NORTE' and found['detail'].startswith('Corte de fibra en el PON 0/1')


def test_cut_on_several_pons_at_once_is_critical_and_alerts():
    with session() as db:
        o = olt(db);outage(db, o, 1);outage(db, o, 2, affected=30, started=T + 120, zone='Centro');db.commit()
        correlator.run(db, T + 300);db.commit()
        case = access(db)
        assert case.severity == 'critical' and case.detail['affected'] == 50 and case.detail['zones'] == ['Centro', 'Norte']
        assert case.detail['reading']['title'] == 'Corte de fibra común: 2 PON con LOS a la vez, 50 ONUs (Centro, Norte)'
        assert alerting(db) == [case]


def test_power_outage_is_not_the_network():
    with session() as db:
        o = olt(db);outage(db, o, 1, cause='power', affected=12);db.commit()
        correlator.run(db, T + 60);db.commit()
        reading = access(db).detail['reading']
        assert reading == {'code': 'power', 'title': 'Falla de energía en la zona: 12 ONUs sin energía (Norte)',
                           'action': 'Informar falla eléctrica en Norte: no es la red. No requiere despacho.'}


def test_unreachable_olt_uplink_recovery_and_close():
    with session() as db:
        o = olt(db, reachable=False)
        db.add(OltUplink(olt_id=o.id, name='xge 0/9', admin_status='Enabled', status='Down', last_seen=T));cut = outage(db, o, 1);db.commit()
        correlator.run(db, T + 60);db.commit()
        case = access(db)
        assert case.severity == 'critical' and case.detail['reading']['title'] == 'SmartOLT no llega a la OLT OLT-NORTE y tiene PON caídos: posible caída total de la OLT'
        assert case.detail['uplinks_down'] == ['xge 0/9']
        # Everything comes back.
        o.reachable = True;db.get(OltUplink, (o.id, 'xge 0/9')).status = 'Up';cut.active, cut.ended_at = False, T + 600;db.commit()
        correlator.run(db, T + 660);db.commit()
        db.refresh(case)
        assert case.last_state == 'up' and case.recoveries == 1 and case.state == 'active'
        titles = [e.title for e in db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id).order_by(CaseEvidence.occurred_at))]
        assert {'SmartOLT no llega a la OLT', 'Uplink xge 0/9 caído', 'SmartOLT vuelve a llegar a la OLT', 'Uplink xge 0/9 restablecido',
                'PON 0/1: restablecido'} <= set(titles)
        correlator.run(db, T + 660 + correlator.QUIET + 1);db.commit()
        db.refresh(case)
        assert case.state == 'closed'
        # A new outage later is a new case.
        outage(db, o, 3, started=T + 9000);db.commit()
        correlator.run(db, T + 9060);db.commit()
        assert db.scalar(select(Case).where(Case.kind == 'access', Case.state == 'active')).id != case.id


def test_router_interface_reaching_the_olt_is_related_and_analyst_reads_it():
    with session() as db:
        ids = inventory(db)
        device = Device(name='OLT-NORTE');db.add(device);db.flush()
        identity = SourceIdentity(source='librenms', external_id='olt', device_id=device.id, name='OLT-NORTE', addresses=[], details={}, last_seen=1)
        db.add(identity);db.flush()
        db.add(InventoryInterface(identity_id=identity.id, external_id='u', name='mgmt', kind='port', addresses=['10.0.0.2/30'], description='', last_seen=1))
        o = olt(db, device_id=device.id);outage(db, o, 1);outage(db, o, 2);db.commit()
        correlator.run(db, T + 60)
        # PE-A's CORE port (10.0.0.1/30) reaches the OLT's address.
        db.add(Case(kind='interface', key='i', entity_key='device:' + ids['PE-A'], entity_label='PE-A', subject='Ethernet1', detail={},
                    opened_at=T - 60, last_activity_at=T, last_state='down', last_state_at=T - 60, state='active', rule_version=4))
        db.commit()
        case = access(db)
        assert case.entity_key == 'device:' + device.id
        relations = {r['subject']: r['relation'] for r in related_cases(db, case, Resolver(db))}
        assert relations['Ethernet1'] == 'Interfaz del router que llega a esta OLT'
        interface = db.scalar(select(Case).where(Case.kind == 'interface'))
        assert {r['relation'] for r in related_cases(db, interface, Resolver(db))} >= {'OLT que cuelga de esta interfaz'}
        header = case_pack(db, case).header
        assert 'caída de acceso en la OLT OLT-NORTE' in header and 'Lectura de ODIN' in header and 'PON 0/1 los 20 de 32 ONUs' in header
