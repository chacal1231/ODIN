from sqlalchemy import select
from odin.db import session
from odin.models import (Case, CaseEvidence, Device, InventoryInterface, Olt, OltOutage, OltPonPort, OltUplink, Pop,
                         SourceIdentity, now, uid)
from odin.cases import access as access_rules
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


def pons(db, o, sizes):
    """The OLT's PON ports with their registered ONUs: the rest of the OLT a PON outage is compared with."""
    for port, onus in enumerate(sizes, 1):
        db.add(OltPonPort(olt_id=o.id, board='0', port=str(port), admin_status='Enabled', status='Up', onus=onus, online=onus, last_seen=T))
    db.flush()


def test_without_the_olt_size_a_single_pon_cut_is_reported_without_alerting():
    # No PON ports read yet: nothing to compare the outage with, so it cannot be called a common failure.
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
                           'action': 'Informar falla eléctrica en Norte: no es la red. No requiere despacho.', 'pon': '0/1'}


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


def test_a_cut_on_most_of_one_pon_is_a_common_failure_and_alerts():
    with session() as db:
        o = olt(db);pons(db, o, [32, 32, 32, 32]);outage(db, o, 1);db.commit()
        correlator.run(db, T + 60);db.commit()
        case = access(db)
        expected = access_rules.one_in(access_rules.chance(20, 32, 1 / 98))
        assert case.severity == 'critical' and case in alerting(db)
        assert case.detail['reading']['title'] == ('Corte de fibra en el PON 0/1: 20 de 32 ONUs (zona Norte, splitters S1, S2) · 16 % de la OLT'
                                                   f' · falla común: si cayeran por separado, {expected} (frente al resto de la OLT)')
        assert expected.startswith('1 en 10') and case.detail['pons'][0]['chance'] < 1e-20
        # The NOC message: the situation in the title, the share and why it is common, and what to do.
        from odin.noc import messages
        assert messages.whatsapp(messages.case_alert(case)).split('\n')[::3] == ['*[CRÍTICO]* Corte de fibra · OLT-NORTE · PON 0/1',
            '*Acción:* informar corte de fibra en Norte; N2 despacha a campo']
        assert '*Afectación:* 20 de 32 ONUs del PON (16 % de la OLT) · zona Norte · falla común, no son clientes sueltos' in messages.whatsapp(messages.case_alert(case))


def test_scattered_customers_are_not_a_common_failure():
    # Power lost by a few customers on every PON: what people switching off their ONU looks like.
    with session() as db:
        o = olt(db);pons(db, o, [32, 32, 32, 32])
        for port, affected in ((1, 3), (2, 4), (3, 3)):outage(db, o, port, cause='power', affected=affected)
        db.commit()
        correlator.run(db, T + 60);db.commit()
        case = access(db)
        assert case.severity == 'warning' and case not in alerting(db)
        assert case.detail['reading']['title'] == 'Falla de energía en la zona: 10 ONUs sin energía (Norte) · 8 % de la OLT'


def test_a_whole_small_pon_alerts_and_a_lone_customer_does_not():
    assert access_rules.reading('OLT', False, [], [{'board': '0', 'port': '1', 'cause': 'los', 'zone': '', 'affected': 4, 'subscribers': 4,
                                                    'started_at': T, 'odbs': []}], registered=100)[3] is True
    assert access_rules.reading('OLT', False, [], [{'board': '0', 'port': '1', 'cause': 'los', 'zone': '', 'affected': 1, 'subscribers': 1,
                                                    'started_at': T, 'odbs': []}], registered=100)[3] is False


def test_the_noc_sets_how_unlikely_a_common_failure_must_be(client, administrator):
    from test_access import enroll
    enroll(client)
    with session() as db:
        o = olt(db);pons(db, o, [32, 32, 32, 32]);outage(db, o, 1, affected=5);db.commit()
    # 5 of 32 down with 1 in 98 elsewhere: about 1 in 56,000.
    with session() as db:
        correlator.run(db, T + 60);db.commit()
        assert access(db).severity == 'critical'
    assert client.put('/api/v1/cases/settings/access', json={'significance': 0.5}).status_code == 422
    assert client.put('/api/v1/cases/settings/access', json={'significance': 0.00001}).json() == {'significance': 0.00001}
    assert client.get('/api/v1/cases/calibration').json()['thresholds']['access_significance'] == 0.00001
    with session() as db:
        correlator.run(db, T + 120);db.commit()
        assert access(db).severity == 'warning'


def test_each_pon_learns_its_normal_by_hour_and_an_outage_does_not_teach_it():
    import datetime as dt
    from zoneinfo import ZoneInfo
    from odin.config import settings
    from odin.models import PonSample
    from odin.olts import baselines
    zone = ZoneInfo(settings().timezone)
    first = int(dt.datetime.combine(dt.date(2027, 3, 1), dt.time(0), zone).timestamp())
    rows = []
    for day in range(15):
        for hour in range(24):
            # Every evening 2 of 20 customers switch their ONU off; one morning the whole PON lost power.
            power = 2 if hour == 21 else 20 if (day, hour) == (3, 10) else 0
            rows.append(PonSample(olt_id='1', board='1', port='1', at=first + (day * 24 + hour) * 3600, onus=20, power=power))
    found = baselines.compute(rows, first + 15 * 86400, zone)
    assert found[('1', '1', 'power', 21)] == (0.1, 15) and found[('1', '1', 'power', 10)] == (0.0, 15)
    assert found[('1', '1', 'los', 21)] == (0.0, 15)


def test_the_same_outage_is_normal_in_the_evening_and_a_common_failure_in_the_morning():
    def outage_of(affected):
        return [{'board': '1', 'port': '1', 'cause': 'power', 'zone': 'Norte', 'affected': affected, 'subscribers': 20, 'started_at': T, 'odbs': []}]
    evening, morning = {('1', '1', 'power'): (0.1, 15)}, {('1', '1', 'power'): (0.0, 15)}
    assert access_rules.reading('OLT', False, [], outage_of(3), 100, normal=evening)[3] is False
    code, title, _, critical = access_rules.reading('OLT', False, [], outage_of(3), 100, normal=morning)
    assert critical and title.endswith('(frente a lo normal de ese PON a esta hora)')


def test_history_is_used_from_enough_days_and_recomputed_daily():
    from odin.models import PonBaseline, PonSample
    from odin.olts import baselines
    hour = baselines.hour_of(T + 60)
    with session() as db:
        o = olt(db);pons(db, o, [32, 32, 32, 32]);outage(db, o, 1, cause='power', affected=3)
        db.add(PonBaseline(olt_id=o.id, board='0', port='1', cause='power', hour=hour, rate=0.0, days=baselines.MIN_DAYS - 1, computed_at=T))
        db.commit()
        # Not enough days: the rest of the OLT, where 3 of 32 is not unusual.
        correlator.run(db, T + 60);db.commit()
        assert access(db).severity == 'warning'
        db.get(PonBaseline, (o.id, '0', '1', 'power', hour)).days = baselines.MIN_DAYS;db.commit()
        correlator.run(db, T + 120);db.commit()
        case = access(db)
        assert case.severity == 'critical' and case.detail['pons'][0]['basis'] == 'history'
        # Once a day, from the samples: the stored one is replaced.
        db.add(PonSample(olt_id=o.id, board='0', port='1', at=T, onus=32, power=0));db.commit()
        assert baselines.refresh(db, T + 3600) is False
        assert baselines.refresh(db, T + 86400 + 1) is True;db.commit()
        stored = {(b.hour, b.days) for b in db.scalars(select(PonBaseline))}
        assert (baselines.hour_of(T), 1) in stored and {days for _, days in stored} == {1}
