from sqlalchemy import select, func
from odin.db import session
from odin.models import (Case, CaseEvidence, CorrelationState, EventStream, SourceEvent, Integration, Device, SourceIdentity,
    LogSender, InventoryInterface, ProbeOrigin, ProbeIncident, MonitoringDestination, Pop, now, uid)
from odin.cases.correlator import parse, run, QUIET, STALE, SUSTAINED, FLAP_CRITICAL
from odin.cases.entities import Resolver
from odin.health import evaluate
from test_access import enroll

PEER = '2001:db8:4003:b100::1'
BGP = f'bgp:PE-A:default:{PEER}'


def test_condition_keys_are_parsed_including_ipv6_peers():
    assert parse(BGP) == {'kind': 'bgp_session', 'source': 'PE-A', 'subject': PEER, 'detail': {'vrf': 'default', 'peer': PEER}}
    assert parse('interface:PE-A:Ethernet26')['subject'] == 'Ethernet26'
    assert parse('bfd:PE-A:default:10.0.0.1:Vlan1') is None and parse(None) is None and parse('bgp:PE-A') is None


def stream(db, source='graylog'):
    row = db.scalar(select(EventStream).where(EventStream.source == source))
    if not row:
        row = EventStream(source=source, name='s:' + source);db.add(row);db.flush()
    return row


def event(db, t, status='info', condition=None, title='log', source='PE-A', kind='log_bgp', severity='info', origin='graylog', device_id=None, received=None):
    row = SourceEvent(stream_id=stream(db, origin).id, external_id=uid(), identity_kind='source_id', entity_id=source, device_id=device_id,
        occurred_at=t, source_updated_at=t, received_at=received or t, last_received_at=received or t, kind=kind, status=status,
        severity=severity, original_severity='5', title=title, reference='r', condition_key=condition, content_hash='x')
    db.add(row);db.flush();return row


def flap(db, t, drops, gap=60, condition=BGP):
    for i in range(drops):
        event(db, t + i * 2 * gap, 'open', condition, 'down');event(db, t + i * 2 * gap + gap, 'recovered', condition, 'up')


def correlate(stamp):
    with session() as db:
        while run(db, stamp):db.commit()
        db.commit()


def cases():
    with session() as db:
        return list(db.scalars(select(Case).order_by(Case.opened_at)))


def test_flaps_of_one_session_form_one_case_and_a_later_drop_a_new_one():
    t = now() - 10 * 86400
    with session() as db:
        flap(db, t, FLAP_CRITICAL)
        flap(db, t + 2 * 3600, 1)  # after more than QUIET stable
        event(db, t - 3600, 'recovered', BGP)  # a recovery without a known drop opens nothing
        db.commit()
    correlate(now())
    first, second = cases()
    assert (first.drops, first.recoveries, first.state, first.severity) == (FLAP_CRITICAL, FLAP_CRITICAL, 'closed', 'critical')
    assert first.closed_at == t + (2 * FLAP_CRITICAL - 1) * 60 and (second.drops, second.severity) == (1, 'warning')
    with session() as db:
        assert db.scalar(select(func.count()).select_from(CaseEvidence).where(CaseEvidence.role == 'trigger')) == 2 * FLAP_CRITICAL + 2


def test_lifecycle_sustained_down_stale_and_quiet_close():
    stamp = now()
    with session() as db:
        event(db, stamp - SUSTAINED - 60, 'open', BGP)
        event(db, stamp - STALE - 3600, 'open', 'interface:PE-A:Ethernet1', kind='log_interface')
        event(db, stamp - 600, 'open', 'interface:PE-A:Ethernet2', kind='log_interface');event(db, stamp - 500, 'recovered', 'interface:PE-A:Ethernet2', kind='log_interface')
        db.commit()
    correlate(stamp)
    by = {c.subject: c for c in cases()}
    assert (by[PEER].state, by[PEER].severity, by[PEER].last_state) == ('active', 'critical', 'down')
    assert by['Ethernet1'].state == 'stale'
    assert (by['Ethernet2'].state, by['Ethernet2'].last_state) == ('active', 'up')
    correlate(stamp + QUIET + 60)
    assert {c.subject: c.state for c in cases()}['Ethernet2'] == 'closed'


def test_out_of_order_event_does_not_flip_current_state():
    stamp = now()
    with session() as db:
        event(db, stamp - 300, 'open', BGP, received=stamp - 290);event(db, stamp - 200, 'recovered', BGP, received=stamp - 190);db.commit()
    correlate(stamp)
    with session() as db:
        event(db, stamp - 250, 'open', BGP, received=stamp - 10);db.commit()  # late, older than the recovery
    correlate(stamp)
    [case] = cases()
    assert case.last_state == 'up' and case.drops == 2


def identities(db):
    device = Device(name='PE-CENTRO-ARISTA');other = Device(name='PE-OTRO');db.add_all([device, other]);db.flush()
    db.add(Integration(kind='graylog', endpoint='http://x', allowed_networks=[]));db.flush()
    # PE-A sends from the same address as PE-A-LOOPBACK, which is the name confirmed in the inventory.
    db.add_all([LogSender(name='PE-A', address='192.0.2.42', first_seen=1, last_seen=1),
                LogSender(name='PE-A-LOOPBACK', address='192.0.2.42', first_seen=1, last_seen=1),
                LogSender(name='PE-B', address='192.0.2.50', first_seen=1, last_seen=1),
                SourceIdentity(source='graylog', external_id='PE-A-LOOPBACK', device_id=device.id, name='PE-A-LOOPBACK', addresses=['192.0.2.42'], details={}, last_seen=1),
                SourceIdentity(source='graylog', external_id='PE-B', device_id=other.id, name='PE-B', addresses=['192.0.2.50'], details={}, last_seen=1)])
    db.flush();return device.id, other.id


def test_identity_joins_by_confirmation_address_and_name_without_merging_devices():
    with session() as db:
        device_id, other_id = identities(db)
        db.add(LogSender(name='pe-a', address='192.0.2.99', first_seen=1, last_seen=1))
        db.add(LogSender(name='pe-b', address='192.0.2.42', first_seen=1, last_seen=1));db.flush()  # probable link between two devices
        r = Resolver(db)
        a = r.entity('graylog', 'PE-A')
        assert a.key == 'device:' + device_id and a.label == 'PE-CENTRO-ARISTA'
        # pe-b sends from PE-A's address and shares PE-B's name: the address (stronger) wins over the name,
        # and the two inventory devices are never merged through probable links.
        assert a.members == {('graylog', 'PE-A'): 'same_address', ('graylog', 'PE-A-LOOPBACK'): 'confirmed',
                             ('graylog', 'pe-a'): 'same_name', ('graylog', 'pe-b'): 'same_address'}
        assert 'logs PE-A-LOOPBACK (vínculo confirmado en el inventario)' in r.explain(a)
        assert r.entity('graylog', 'PE-B').key == 'device:' + other_id
        assert r.entity('graylog', 'DESCONOCIDO').key == 'name:DESCONOCIDO'


def test_enrichment_links_only_explained_evidence():
    stamp = now() // 300 * 300
    with session() as db:
        device_id, _ = identities(db)
        t = stamp - 3600
        event(db, t, 'open', BGP)
        event(db, t + 60, title=f'%BGP-3-NOTIFICATION: received from neighbor {PEER} (VRF default AS 64500) Cease', severity='warning')
        event(db, t + 70, title='%BGP-3-NOTIFICATION: sent to neighbor 10.9.9.9', severity='warning')  # another session: not linked
        event(db, t + 80, title='%SYS-5-CONFIG_E: Enter configuration mode', kind='log_config')
        event(db, t + 90, title='%LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet1, changed state to down',
              status='open', condition='interface:PE-A:Ethernet1', kind='log_interface')  # its own case, shown as related
        event(db, t + 100, 'open', title='Router con pérdida', origin='zabbix', kind='zabbix_problem', device_id=device_id, source='10001')
        db.commit()
    correlate(stamp)
    with session() as db:
        case = db.scalar(select(Case).where(Case.kind == 'bgp_session'))
        found = {(e.source, e.reason): e for e in db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role != 'trigger'))}
    assert set(found) == {('graylog', 'Menciona el mismo vecino BGP'), ('graylog', 'Cambio de configuración del mismo equipo'),
                          ('zabbix', 'Alerta del mismo equipo')}


def test_case_follows_identity_confirmed_later():
    stamp = now()
    with session() as db:
        db.add(Integration(kind='graylog', endpoint='http://x', allowed_networks=[]))
        db.add(LogSender(name='PE-A', address='192.0.2.42', first_seen=1, last_seen=1))
        event(db, stamp - 300, 'open', BGP);db.commit()
    correlate(stamp)
    assert cases()[0].entity_key == 'name:PE-A'
    with session() as db:
        device = Device(name='PE-REAL');db.add(device);db.flush()
        db.add(SourceIdentity(source='graylog', external_id='PE-A', device_id=device.id, name='PE-A', addresses=[], details={}, last_seen=1))
        db.commit();device_id = device.id
    correlate(stamp + 60)
    assert (cases()[0].entity_key, cases()[0].entity_label) == ('device:' + device_id, 'PE-REAL')


def test_point_to_point_ends():
    from odin.cases.api import same_link
    assert same_link('10.254.3.15', '10.254.3.14') and same_link('fc80:0:0:e000::1e', 'fc80:0:0:e000::1d')
    assert not same_link('10.254.3.15', '10.254.3.16') and not same_link('10.254.3.15', '10.254.3.15') and not same_link('x', '10.0.0.1')


def test_alert_on_the_port_facing_the_neighbor_is_linked_to_the_session():
    stamp = now()
    with session() as db:
        device_id, _ = identities(db)
        db.add(Integration(kind='librenms', endpoint='http://x', allowed_networks=[]));db.flush()
        identity = SourceIdentity(source='librenms', external_id='1', device_id=device_id, name='PE-A', addresses=[], details={}, last_seen=1)
        db.add(identity);db.flush()
        db.add(InventoryInterface(identity_id=identity.id, external_id='1', name='Ethernet1', kind='port',
                                  addresses=['2001:db8:4003:b100::2/126'], description='TRANSITO-TX-10G', last_seen=1))
        event(db, stamp - 600, 'open', BGP)
        event(db, stamp - 500, 'open', title='Interface Ethernet1(TRANSITO-TX-10G): High bandwidth usage (>90%)', origin='zabbix',
              kind='zabbix_problem', device_id=device_id, source='10001')
        event(db, stamp - 400, 'open', title='Interface Ethernet10(OTRA): High bandwidth usage (>90%)', origin='zabbix',
              kind='zabbix_problem', device_id=device_id, source='10001')
        db.commit()
    correlate(stamp)
    with session() as db:
        reasons = sorted(e.reason for e in db.scalars(select(CaseEvidence).where(CaseEvidence.source == 'zabbix')))
    assert reasons == ['Alerta de la interfaz hacia el vecino (por su subred)', 'Alerta del mismo equipo']


def test_api_lists_details_relates_other_end_and_records_actions(client, administrator):
    stamp = now()
    with session() as db:
        db.add(Integration(kind='graylog', endpoint='http://x', allowed_networks=[]))
        db.add_all([LogSender(name='PE-A', address='10.0.0.1', first_seen=1, last_seen=1),
                    LogSender(name='PE-B', address='10.0.0.2', first_seen=1, last_seen=1)])
        event(db, stamp - 600, 'open', 'bgp:PE-A:default:10.0.0.2')
        event(db, stamp - 590, 'open', 'bgp:PE-B:default:10.0.0.1', source='PE-B');db.commit()
    correlate(stamp)
    enroll(client)
    listed = client.get('/api/v1/cases').json()
    assert len(listed['items']) == 2 and listed['counts'] == {'active': 2, 'alerting': 0}
    case_id = next(c['id'] for c in listed['items'] if c['entity_label'] == 'PE-A')
    detail = client.get('/api/v1/cases/' + case_id).json()
    assert [r['relation'] for r in detail['related']] == ['Otro extremo de la misma sesión BGP']
    # The same pair seen only through link addresses (no source reports them) is still recognised.
    with session() as db:
        event(db, stamp - 500, 'open', 'bgp:PE-A:default:10.254.3.14');event(db, stamp - 490, 'open', 'bgp:PE-B:default:10.254.3.13', source='PE-B');db.commit()
    correlate(stamp)
    pair = next(c['id'] for c in client.get('/api/v1/cases').json()['items'] if c['subject'] == '10.254.3.14')
    assert any('mismo enlace punto a punto' in r['relation'] and r['subject'] == '10.254.3.13'
               for r in client.get('/api/v1/cases/' + pair).json()['related'])
    assert detail['triggers_total'] == 1 and detail['identity'] == 'logs PE-A'
    assert client.post(f'/api/v1/cases/{case_id}/acknowledge').status_code == 200
    assert client.post(f'/api/v1/cases/{case_id}/notes', json={'text': 'Revisado con el proveedor'}).status_code == 201
    detail = client.get('/api/v1/cases/' + case_id).json()
    assert detail['acknowledged_at'] and detail['notes'][0]['text'] == 'Revisado con el proveedor'
    assert client.get('/api/v1/monitoring').json()['cases'] == {'alerts_enabled': False}
    assert client.put('/api/v1/cases/settings/alerts', json={'alerts_enabled': True}).status_code == 200
    summary = client.get('/api/v1/monitoring').json()['cases']
    assert summary == {'alerts_enabled': True, 'active': 0, 'critical': 0, 'unacknowledged': 0}  # brief drops without a role do not alert


def test_health_reports_stalled_correlator():
    stamp = now()
    with session() as db:
        db.add(ProbeOrigin(id='odin', heartbeat=stamp))
        db.add(CorrelationState(id='cases', last_run_at=stamp, last_success_at=stamp - 2000, last_error='correlation_failed'));db.commit()
        [check] = evaluate(db, stamp)['checks']
        assert check['id'] == 'correlation' and check['state'] == 'critical' and check['error_code'] == 'correlation_failed'


def test_latency_incidents_become_cases_per_destination_or_service_related_by_pop_and_period():
    from odin.models import ProbeTarget
    from odin.probes.config import ProbeConfig, DEFAULT_VERSION_ID
    from odin.cases.api import related_cases
    def target(db, name, **kwargs):
        row = ProbeTarget(name=name, address='10.20.0.2', profile_version_id=DEFAULT_VERSION_ID, approved_ips=['10.20.0.2'],
                          effective=ProbeConfig().model_dump(), **kwargs)
        db.add(row);db.flush();return row
    from odin.analyst.chat import period_lines, pop_scope
    stamp = now();t = stamp - 7200
    with session() as db:
        device_id, other_id = identities(db)
        pop = Pop(name='Norte');db.add(pop);db.flush()
        db.get(Device, device_id).pop_id = pop.id;db.get(Device, other_id).pop_id = pop.id
        own = MonitoringDestination(name='PE-CENTRO', scope='network', device_id=device_id)
        fra, eus = MonitoringDestination(name='AZURE-FRA', scope='external'), MonitoringDestination(name='AZURE-EUS', scope='external')
        db.add_all([own, fra, eus]);db.flush()
        pe = target(db, 'PE-CENTRO', destination_id=own.id)
        a1 = target(db, 'AZURE-FRA', service='Azure', destination_id=fra.id)
        a2 = target(db, 'AZURE-EUS', service='Azure', destination_id=eus.id)
        def incident(row, opened, closed=None, severity='warning'):
            db.add(ProbeIncident(target_id=row.id, address=row.address, opened_at=opened, closed_at=closed, severity=severity,
                                 conditions={'loss': severity}, profile=row.effective))
        incident(pe, t, t + 600);incident(pe, t + 900, severity='critical')  # within QUIET: same case, still open
        incident(a1, t + 100, t + 400);incident(a2, t + 200, t + 500)       # two regions, one service case
        incident(a1, t + 500 + QUIET + 60)                                   # later episode: a new case
        event(db, t + 50, title='%SYS-5-CONFIG_I: Configured from console', kind='log_config')
        event(db, t + 300, 'open', 'bgp:PE-B:default:10.0.0.1', source='PE-B')  # other equipment of the same POP
        db.commit()
    correlate(stamp);correlate(stamp)  # a second pass adds nothing
    by = {}
    for c in cases():by.setdefault(c.subject, []).append(c)
    (network,), (azure, later) = by['PE-CENTRO'], by['Azure']
    assert (network.entity_key, network.drops, network.state, network.last_state, network.severity) == ('device:' + device_id, 2, 'active', 'down', 'critical')
    assert (azure.drops, azure.recoveries, azure.state, azure.closed_at) == (2, 2, 'closed', t + 500) and later.state == 'active'
    with session() as db:
        reasons = {e.reason for e in db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == network.id))}
        assert reasons == {'Incidente de latencia del mismo destino', 'Cambio de configuración del mismo equipo'}
        resolver = Resolver(db)
        relations = lambda case: {(r['subject'], r['relation']) for r in related_cases(db, db.get(Case, case.id), resolver)}
        assert relations(network) == {('10.0.0.1', 'Otro equipo del mismo POP en el mismo período')}
        assert ('10.0.0.1', 'Mismo período, solo coincidencia temporal') in relations(azure)
        scoped = {l.text.split(' · ')[1] for l in period_lines(db, t - 600, stamp, pop_scope(db, pop.id)) if l.label.startswith('C')}
        assert scoped == {'caso de latencia hacia PE-CENTRO en PE-CENTRO-ARISTA', 'caso de sesión BGP 10.0.0.1 en PE-OTRO'}
        assert any('Azure' in l.text for l in period_lines(db, t - 600, stamp) if l.label.startswith('C'))
        # Incidents are grouped by destination: one line each, with how many and whether one is still open.
        latency = {l.text.split(' · ')[1]: l for l in period_lines(db, t - 600, stamp) if l.label.startswith('L')}
        assert set(latency) == {'latencia hacia el destino PE-CENTRO (10.20.0.2)', 'latencia hacia el destino AZURE-FRA (10.20.0.2)',
                                'latencia hacia el destino AZURE-EUS (10.20.0.2)'}
        line = latency['latencia hacia el destino PE-CENTRO (10.20.0.2)']
        assert line.ref['count'] == 2 and '2 incidentes entre' in line.text and 'severidad critical, warning · 1 sigue abierto' in line.text
        db.execute(ProbeIncident.__table__.update().where(ProbeIncident.closed_at.is_(None), ProbeIncident.target_id == pe.id).values(closed_at=t + 1200))
        db.commit()
    correlate(stamp)
    with session() as db:
        case = db.get(Case, network.id)
        assert (case.last_state, case.resolved_at, case.recoveries) == ('up', t + 1200, 2)


def test_roles_set_severity_and_relate_routes_to_the_same_router(client, administrator):
    stamp = now() // 300 * 300;t = stamp - 3600
    with session() as db:
        device_id, other_id = identities(db)
        db.add(Integration(kind='librenms', endpoint='http://x', allowed_networks=[]));db.flush()
        for owner, ports in ((device_id, [('Vlan1', 'TRANS-TX-POP-B-RUTA-01', '10.254.3.2/30'), ('Vlan2', 'TRANS-TX-POP-B-RUTA-02', '10.254.3.6/30'),
                                          ('Vlan3', 'CUST: CLIENTE (Norte) [1G] {DIA:DIA-0001 | ASN:NA | CT:ISP}', '10.9.9.1/30'),
                                          ('Ethernet51/1', 'TRANSITO-CARRIER-100G', '198.51.100.133/30')]),
                             (other_id, [('VlanA', 'TRANS-TX-RUTA-01', '10.254.3.1/30'), ('VlanB', 'TRANS-TX-RUTA-02', '10.254.3.5/30')])):
            identity = SourceIdentity(source='librenms', external_id=owner, device_id=owner, name=owner, addresses=[], details={}, last_seen=1)
            db.add(identity);db.flush()
            for n, (name, description, address) in enumerate(ports):
                db.add(InventoryInterface(identity_id=identity.id, external_id=str(n), name=name, kind='port', addresses=[address], description=description, last_seen=1))
        event(db, t, 'open', 'bgp:PE-A:default:10.254.3.1');event(db, t + 60, 'open', 'bgp:PE-A:default:10.254.3.5')
        event(db, t + 30, 'open', 'bgp:PE-A:default:10.9.9.2');event(db, t + 90, 'recovered', 'bgp:PE-A:default:10.9.9.2')
        event(db, t + 10, 'open', 'interface:PE-A:Ethernet51/1', kind='log_interface')
        db.commit()
    correlate(stamp);correlate(stamp)  # the first pass reads the roles, the next one applies them
    by = {c.subject: c for c in cases()}
    route1, route2, customer, transit = by['10.254.3.1'], by['10.254.3.5'], by['10.9.9.2'], by['Ethernet51/1']
    assert route1.detail['neighbor']['remote_device_name'] == 'PE-OTRO' and route1.detail['neighbor']['interface'] == 'Vlan1'
    assert (route1.severity, route1.drops) == ('critical', 1)       # internal link: critical from the first drop
    assert (customer.severity, customer.detail['neighbor']['label']) == ('warning', 'cliente')
    assert (transit.severity, transit.detail['port']['label']) == ('critical', 'tránsito')
    enroll(client)
    data = client.get(f'/api/v1/cases/{route1.id}').json()
    assert ('10.254.3.5', 'Otra ruta hacia PE-OTRO en el mismo período') in {(r['subject'], r['relation']) for r in data['related']}
    assert data['isolation'].startswith('Todas las rutas conocidas hacia PE-OTRO (2)')


def test_alert_policy_by_role_sustained_and_simultaneous_external_services(client, administrator):
    from odin.cases.summary import alerting, calibration
    stamp = now() - 3600
    def case(kind, subject, severity='warning', state='active', **detail):
        return Case(kind=kind, key=uid(), entity_key='k', entity_label='PE', subject=subject, detail=detail, opened_at=stamp,
                    last_activity_at=stamp + 60, last_state='down', last_state_at=stamp, state=state, severity=severity, rule_version=4)
    with session() as db:
        db.add_all([case('bgp_session', 'interno', neighbor={'role': 'transport', 'remote_device_id': 'x'}),
                    case('bgp_session', 'cliente', neighbor={'role': 'customer', 'remote_device_id': None}),
                    case('bgp_session', 'cliente-sostenido', severity='critical', neighbor={'role': 'customer', 'remote_device_id': None}),
                    case('interface', 'Ethernet1', port={'role': 'transit'}),
                    case('latency', 'PE-X', severity='critical', scope='network'), case('latency', 'PE-Y', scope='network'),
                    case('latency', 'Azure', severity='critical', scope='external'), case('latency', 'Oracle', severity='critical', scope='external'),
                    case('bgp_session', 'cerrado', state='closed', neighbor={'role': 'transit', 'remote_device_id': None}),
                    case('interface', 'antiguo', severity='critical', state='stale')])
        db.commit()
        assert {c.subject for c in alerting(db)} == {'interno', 'cliente-sostenido', 'Ethernet1', 'PE-X'}  # two external services are not enough
        db.add(case('latency', 'AWS', scope='external'));db.commit()
        assert {'Azure', 'Oracle', 'AWS'} <= {c.subject for c in alerting(db)}
        result = calibration(db)
        assert sum(d['alerts'] for d in result['days']) == 9 and result['thresholds']['external_together'] == 3
    enroll(client)
    assert {c['subject'] for c in client.get('/api/v1/cases?state=alerting').json()['items']} >= {'interno', 'AWS'}
    assert client.get('/api/v1/cases/calibration').status_code == 200
