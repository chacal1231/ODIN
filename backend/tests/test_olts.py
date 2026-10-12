"""SmartOLT: read-only connector, no customer data, budget, inventory, PON outages, views and self-supervision."""
import json
from zoneinfo import ZoneInfo
import httpx
import pytest
from sqlalchemy import select, func
from odin.db import session
from odin.jobs import claim
from odin.models import (Device, Integration, Job, Olt, OltOutage, OltPonPort, OltUplink, Onu, Pop, SmartoltState, SourceIdentity, now)
from odin.security import encrypt
from odin.inventory.connectors import ConnectorError
from odin.olts import connector, service
from odin.olts.connector import SmartOLT, RateLimited, READS, ONU_FIELDS, endpoint_valid
from odin.health import smartolt_checks
from test_access import enroll

ENDPOINT = 'https://isp.smartolt.com'


def fake_http(monkeypatch, handler):
    """Every SmartOLT request goes to `handler`; the public address check is satisfied."""
    seen = []
    def route(request):
        seen.append(request)
        return handler(request)
    real = httpx.Client
    monkeypatch.setattr(connector.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(route), **kw))
    monkeypatch.setattr(connector, 'public_address', lambda host, port: '203.0.113.7')
    monkeypatch.setattr(connector, 'SPACING', 0)
    return seen


def ok(payload, **headers):
    return httpx.Response(200, json={'status': True, **payload}, headers=headers)


def test_only_read_endpoints_exist():
    # Every SmartOLT read is named get_*; nothing that writes (reboot, disable, delete, move...) can be called.
    assert READS and all('/get_' in path and path.startswith('/api/') for path in READS)
    client = SmartOLT(ENDPOINT, 'k')
    for path in ('/api/onu/reboot/{}', '/api/onu/delete/{}', '/api/onu/bulk_disable', '/api/system/save_config'):
        with pytest.raises(ConnectorError, match='endpoint_not_allowed'):client.get(path, 'x')
    assert not [name for name in dir(SmartOLT) if not name.startswith('_') and name not in ('get', 'test', 'onus')]


@pytest.mark.parametrize('endpoint,valid', [(ENDPOINT, True), (ENDPOINT + '/', True), ('http://isp.smartolt.com', False),
    ('https://isp.smartolt.com.evil.net', False), ('https://evil.net/isp.smartolt.com', False), ('https://isp.smartolt.com:8443', False),
    ('https://user:pass@isp.smartolt.com', False), ('https://isp.smartolt.com/api', False)])
def test_endpoint_must_be_a_smartolt_subdomain(endpoint, valid):
    assert endpoint_valid(endpoint) is valid


def test_requests_carry_the_key_pin_the_address_and_keep_the_budget(monkeypatch):
    seen = fake_http(monkeypatch, lambda r: ok({'response': []}, **{'X-RateLimit-Remaining': '980', 'X-RateLimit-Limit': '1000'}))
    client = SmartOLT(ENDPOINT, 'secret-key')
    client.get('/api/system/get_outage_pons/{}', '1/../x')
    request = seen[0]
    assert request.url.host == '203.0.113.7' and request.headers['host'] == 'isp.smartolt.com' and request.headers['x-token'] == 'secret-key'
    assert request.method == 'GET' and request.url.raw_path == b'/api/system/get_outage_pons/1%2F..%2Fx'
    assert (client.remaining, client.limit, client.calls) == (980, 1000, 1)


def test_rate_limit_errors_and_redirects(monkeypatch):
    responses = iter([httpx.Response(429, json={'status': False, 'error_code': 'rate_limit_exceeded'}, headers={'Retry-After': '120'}),
                      httpx.Response(200, json={'status': False, 'error': 'OLT not found', 'error_code': 'not_found'}),
                      httpx.Response(302, headers={'Location': 'https://elsewhere'}), httpx.Response(403, json={})])
    fake_http(monkeypatch, lambda r: next(responses))
    client = SmartOLT(ENDPOINT, 'k')
    with pytest.raises(RateLimited) as limited:client.get('/api/system/get_olts')
    assert limited.value.retry_after == 120
    with pytest.raises(ConnectorError) as error:client.get('/api/system/get_olts')
    assert (error.value.code, error.value.detail) == ('smartolt_error', 'not_found')
    with pytest.raises(ConnectorError, match='redirect_not_allowed'):client.get('/api/system/get_olts')
    with pytest.raises(ConnectorError, match='access_denied'):client.get('/api/system/get_olts')


def test_onus_are_paged_and_never_bring_customer_data(monkeypatch):
    def handler(request):
        page = int(request.url.params['page'])
        onu = {'unique_external_id': f'onu{page}', 'sn': 'ZTEG1', 'olt_id': '1', 'board': '0', 'port': '3', 'onu': str(page), 'name': 'Cliente',
               'address': 'Calle 1', 'contact': '300', 'username': 'pppoe-user', 'password': 'pppoe-pass', 'mgmt_ip_address': '10.1.1.1'}
        return httpx.Response(200, json={'status': True, 'page': page, 'total_pages': 2, 'onus': [onu]})
    seen = fake_http(monkeypatch, handler)
    rows = SmartOLT(ENDPOINT, 'k').onus()
    assert [r['unique_external_id'] for r in rows] == ['onu1', 'onu2']
    asked = seen[0].url.params['fields'].split(',')
    assert asked == list(ONU_FIELDS) and not {'address', 'contact', 'username', 'password'} & set(asked)
    assert all(set(r) == set(ONU_FIELDS) for r in rows) and 'pppoe-pass' not in json.dumps(rows)
    assert seen[0].url.params['page_size'] == '100'


def test_values_as_smartolt_writes_them():
    assert [service.number(x) for x in ('6.839 dBm', '24°C', '-20.05 dBm', 25.2, 'N/A', '-', None, '')] == [6.839, 24, -20.05, 25.2, None, None, None, None]
    assert service.uptime('576 days, 21:05') == ((576 * 24 + 21) * 60 + 5) * 60 and service.uptime('03:10:05') == 3 * 3600 + 605
    assert service.stamp('2026-06-21 21:14:22', ZoneInfo('America/Bogota')) == service.stamp('2026-06-22 02:14:22', ZoneInfo('UTC'))
    assert service.stamp('ayer', ZoneInfo('UTC')) is None


def inventory_data(ip='10.9.9.9', pons=(('1', '1', 'Up'), ('1', '2', 'Down'))):
    return {'olts': [{'id': '1', 'name': 'OLT-Norte', 'olt_hardware_version': 'ZTE-C620', 'ip': ip}],
            'health': {'1': {'olt_id': '1', 'uptime': '93 days, 10:48', 'env_temp': '38°C'}},
            'ports': {'1': ([{'board': b, 'pon_port': p, 'pon_type': 'GPON', 'admin_status': 'Enabled', 'operational_status': st,
                              'tx_power': '6.8 dBm', 'onus_count': 64, 'online_onus_count': 60, 'average_signal': '-21.3', 'description': ''}
                             for b, p, st in pons],
                            [{'name': 'xgei_1/4/1', 'status': 'Down', 'admin_status': 'Enabled', 'type': 'Fiber', 'temperature': 'N/A'},
                             {'name': 'xgei_1/4/2', 'status': '10G-FullD', 'admin_status': 'Enabled', 'type': 'Fiber', 'temperature': 25.2}])}}


def test_inventory_matches_the_olt_with_its_device_and_pop():
    stamp = now()
    with session() as db:
        pop = Pop(name='Norte');db.add(pop);db.flush()
        device = Device(name='OLT-NORTE', pop_id=pop.id);db.add(device)
        db.add(Integration(kind='librenms', endpoint='http://x', allowed_networks=[]));db.flush()
        db.add(SourceIdentity(source='librenms', external_id='9', device_id=device.id, name='OLT-NORTE', addresses=['10.9.9.9'], details={}, last_seen=1))
        db.commit()
    # A fresh session, as in the worker: looking up the device flushes the new OLT.
    with session() as db:
        service.write_inventory(db, inventory_data(), stamp);db.commit()
        device, pop = db.get(Device, device.id), db.get(Pop, pop.id)
        olt = db.get(Olt, '1')
        assert (olt.device_id, olt.pop_id, olt.temperature, olt.uptime_seconds) == (device.id, pop.id, 38, (93 * 24 + 10) * 3600 + 48 * 60)
        assert db.get(OltPonPort, ('1', '1', '2')).status == 'Down' and db.get(OltPonPort, ('1', '1', '1')).average_signal == -21.3
        assert db.get(OltUplink, ('1', 'xgei_1/4/2')).temperature == 25.2
        # An administrator's POP stays when the inventory moves; a vanished PON port goes; a vanished OLT is not present.
        other = Pop(name='Sur');db.add(other);db.flush();olt.pop_id, olt.pop_manual = other.id, True;db.commit()
        service.write_inventory(db, inventory_data(pons=(('1', '1', 'Up'),)), stamp + 3600);db.commit()
        assert db.get(Olt, '1').pop_id == other.id and db.get(OltPonPort, ('1', '1', '2')) is None
        service.write_inventory(db, {'olts': [], 'health': {}, 'ports': {}}, stamp + 7200);db.commit()
        assert db.get(Olt, '1').present is False


OUTAGE = {'total_pons': 1, 'sections': [
    {'key': 'los', 'groups': [{'label': 'troncal', 'pons': [{'olt_id': '1', 'board': '1', 'port': '2', 'total_onus': '64', 'los_count': '61',
        'power_count': '0', 'offline_count': '3', 'zone_name': 'Norte', 'pon_description': 'Troncal norte',
        'odb_names': 'F401-1, F401-2, F401-3', 'latest_status_change': '2026-06-21 21:14:22'}]}]},
    {'key': 'power', 'groups': [{'label': 'Norte', 'pons': [{'olt_id': '1', 'board': '1', 'port': '5', 'total_onus': '20', 'power_count': '18',
        'zone_name': 'Norte', 'odb_names': '', 'latest_status_change': '2026-06-21 21:20:00'}]}]},
    {'key': 'offline', 'groups': []}],
    'stale': {'groups': [{'pons': [{'olt_id': '1', 'board': '2', 'port': '8', 'total_onus': '4', 'odb_names': 'X'}]}]}, 'unreachable_olts': []}


def test_outages_open_update_and_end():
    stamp = now();zone = ZoneInfo('America/Bogota')
    with session() as db:
        service.write_inventory(db, inventory_data(), stamp)
        service.write_outages(db, {'outages': {'1': OUTAGE}}, stamp, zone);db.commit()
        rows = {(o.board, o.port, o.cause): o for o in db.scalars(select(OltOutage).where(OltOutage.active.is_(True)))}
        assert set(rows) == {('1', '2', 'los'), ('1', '5', 'power'), ('2', '8', 'stale')}
        los = rows[('1', '2', 'los')]
        assert (los.subscribers, los.affected, los.odbs, los.zone) == (64, 61, ['F401-1', 'F401-2', 'F401-3'], 'Norte')
        assert los.started_at == service.stamp('2026-06-21 21:14:22', zone) and rows[('1', '5', 'power')].affected == 18
        first = los.id
        # Still down: the same outage. Then the fiber is back and SmartOLT stops listing it.
        service.write_outages(db, {'outages': {'1': OUTAGE}}, stamp + 300, zone);db.commit()
        assert db.scalar(select(func.count()).select_from(OltOutage)) == 3
        service.write_outages(db, {'outages': {'1': {**OUTAGE, 'sections': [], 'unreachable_olts': ['1']}}}, stamp + 600, zone);db.commit()
        ended = db.get(OltOutage, first)
        assert (ended.active, ended.ended_at) == (False, stamp + 600) and db.get(Olt, '1').reachable is False


def enable(db, **values):
    db.add(Integration(**{'kind': 'smartolt', 'endpoint': ENDPOINT, 'encrypted_secret': encrypt('k'), 'allowed_networks': [],
                          'events_enabled': True, **values}))
    db.commit()


def run_next():
    with session() as db:
        service.schedule(db)
        job = claim(db, kinds=('smartolt_poll',))
    if job:service.execute(job.id, job.lease_token)
    return job


def test_reads_are_scheduled_one_at_a_time_and_a_429_stops_them(monkeypatch):
    calls = []
    def fetch(client, task, known):
        calls.append((task, list(known)))
        client.remaining, client.limit = 970, 1000
        if task == 'inventory':return inventory_data()
        if task == 'outages':return {'outages': {k: OUTAGE for k in known}}
        return {'onus': [{'unique_external_id': 'a', 'olt_id': '1', 'board': '1', 'port': '2', 'signal': 'Critical', 'status': 'LOS'}]}
    monkeypatch.setattr(service, 'fetch', fetch)
    with session() as db:enable(db)
    assert run_next() and run_next() and run_next()
    assert calls == [('inventory', []), ('outages', ['1']), ('onus', ['1'])]
    with session() as db:
        st = db.get(SmartoltState, 'smartolt')
        assert set(st.tasks) == {'inventory', 'outages', 'onus'} and st.rate_remaining == 970
        assert st.due['outages'] >= now() + 300 and st.due['onus'] >= now() + 86400
        assert db.get(Onu, 'a').signal == 'Critical'
    assert run_next() is None  # nothing due
    def limited(client, task, known):raise RateLimited(600)
    monkeypatch.setattr(service, 'fetch', limited)
    with session() as db:
        st = db.get(SmartoltState, 'smartolt');st.due = {**st.due, 'outages': 0};db.commit()
    run_next()
    with session() as db:
        st = db.get(SmartoltState, 'smartolt')
        assert st.blocked_until >= now() + 590 and st.tasks['outages']['last_error'] == 'rate_limited'
        st.due = {**st.due, 'outages': 0};db.commit()
    assert run_next() is None  # blocked until Retry-After


def test_disabled_or_changed_source_reads_nothing(monkeypatch):
    monkeypatch.setattr(service, 'fetch', lambda *a: pytest.fail('no read expected'))
    with session() as db:enable(db, events_enabled=False)
    assert run_next() is None
    with session() as db:
        db.get(Integration, 'smartolt').events_enabled = True;db.commit()
        service.schedule(db);job = claim(db, kinds=('smartolt_poll',))
        db.get(Integration, 'smartolt').version += 1;db.commit()
    service.execute(job.id, job.lease_token)
    with session() as db:assert db.get(Job, job.id).result == {'status': 'cancelled'}


def test_views_assignment_and_settings(client, administrator):
    stamp = now()
    with session() as db:
        enable(db, events_enabled=False)
        service.write_inventory(db, inventory_data(), stamp)
        service.write_outages(db, {'outages': {'1': OUTAGE}}, stamp, ZoneInfo('UTC'))
        service.write_onus(db, {'onus': [{'unique_external_id': f'o{i}', 'olt_id': '1', 'board': '1', 'port': '1',
                                          'signal': 'Critical' if i < 3 else 'Very good'} for i in range(5)]}, stamp, ZoneInfo('UTC'))
        pop = Pop(name='Norte');db.add(pop);db.commit();pop_id = pop.id
    enroll(client)
    listing = client.get('/api/v1/olts').json()
    (item,) = listing['olts']
    assert (item['pons'], item['pons_down'], item['onus'], item['online'], item['uplinks_down'], item['outages'], item['affected']) == (2, 1, 128, 120, 1, 2, 79)
    assert listing['source']['configured'] and not listing['source']['enabled']
    detail = client.get('/api/v1/olts/1').json()
    first = detail['pon_ports'][0]
    assert (first['board'], first['port'], first['critical'], first['down']) == ('1', '1', 3, False)
    assert detail['pon_ports'][1]['outage'] == 'los' and detail['uplink_ports'][0]['down'] is True
    assert {o['cause'] for o in detail['active_outages']} == {'los', 'power', 'stale'}
    assert client.get('/api/v1/olts/nope').status_code == 404
    assert client.put('/api/v1/olts/1', json={'pop_id': pop_id}).status_code == 200
    assert client.get('/api/v1/olts').json()['olts'][0]['pop_name'] == 'Norte'
    version = listing['source']['version']
    assert client.put('/api/v1/smartolt/settings', json={'version': version, 'enabled': True, 'timezone': 'Marte/Olympus'}).status_code == 422
    assert client.put('/api/v1/smartolt/settings', json={'version': version, 'enabled': True, 'timezone': 'America/Bogota'}).status_code == 200
    assert client.get('/api/v1/olts').json()['source']['timezone'] == 'America/Bogota'
    for _ in range(4):assert client.post('/api/v1/smartolt/refresh').status_code == 202
    assert client.post('/api/v1/smartolt/refresh').status_code == 429


def test_integration_requires_a_smartolt_subdomain(client, administrator):
    enroll(client)
    assert client.put('/api/v1/admin/integrations/smartolt', json={'endpoint': 'https://evil.net', 'secret': 'k'}).status_code == 422
    assert client.put('/api/v1/admin/integrations/smartolt', json={'endpoint': ENDPOINT, 'secret': 'k'}).status_code == 200
    assert client.post('/api/v1/admin/integrations/smartolt/test').status_code == 202


def test_health_reports_a_stopped_reader_and_the_budget():
    stamp = now()
    with session() as db:
        assert smartolt_checks(db, stamp) == []
        enable(db, updated_at=stamp - 1800)
        assert smartolt_checks(db, stamp)[0]['title'] == 'Lectura de SmartOLT detenida'
        db.add(SmartoltState(id='smartolt', due={}, tasks={'outages': {'last_success_at': stamp - 60}}, rate_remaining=50, rate_limit=1000,
                             rate_observed_at=stamp - 30))
        db.commit()
        assert [c['title'] for c in smartolt_checks(db, stamp)] == ['Presupuesto de SmartOLT casi agotado']
        db.get(SmartoltState, 'smartolt').blocked_until = stamp + 300;db.commit()
        assert [c['title'] for c in smartolt_checks(db, stamp)] == ['SmartOLT limitó las llamadas']


def test_huawei_pon_status_and_unused_pon():
    from odin.olts.api import down, pon_down
    assert not down('Enabled', 'Up / Autofind') and down('Enabled', 'Down / Autofind') and not down('Disabled', 'Down')
    assert not pon_down(OltPonPort(admin_status='Enabled', status='Down / Autofind', onus=0))
    assert pon_down(OltPonPort(admin_status='Enabled', status='Down / Autofind', onus=4))
