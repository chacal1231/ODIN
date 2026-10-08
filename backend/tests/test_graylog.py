import re
import pytest
from sqlalchemy import select, func
from odin.db import session
from odin.models import Integration, EventCollector, EventStream, SourceEvent, LogSender, SourceIdentity, Device, now
from odin.security import encrypt
from odin.inventory.connectors import ConnectorError
from odin.events import graylog
from odin.events.graylog import Graylog, classify, window, received_at, stamp_text, WINDOW_CAP, SETTLE, OVERLAP
from odin.events.collector import schedule, execute
from odin.jobs import claim
from test_access import enroll

# Verbatim messages from the installed PEs (Graylog 6.1.16, October 2026).
ADJ_DOWN = 'PE-CENTRO01 Bgp: 60877: %BGP-5-ADJCHANGE: peer 2001:db8:4003:b100::1 (VRF default AS 64500) old state Established event RecvHardReset new state Idle'
ADJ_UP = 'PE-CENTRO01 Bgp: 60885: %BGP-5-ADJCHANGE: peer 2001:db8:4003:b100::1 (VRF default AS 64500) old state OpenConfirm event Established new state Established'
NOTIFICATION = 'PE-CENTRO01 Bgp: 60892: %BGP-3-NOTIFICATION: received from neighbor 2001:db8:4003:b100::1 (VRF default AS 64500) 6/1 (Cease/maximum number of prefixes reached) 0 bytes'
LINE_DOWN = 'PE-SUR Ebra: 573: %LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet26 (CUST: CLIENTE (Sur) [500M] {DIA:DIA-0101 | ASN:NA | CT:ISP}), changed state to down'
LINE_UP = 'PE-SUR Ebra: 574: %LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet26 (CUST: CLIENTE (Sur) [500M] {DIA:DIA-0101 | ASN:NA | CT:ISP}), changed state to up'
BFD_DOWN = 'PE-CENTRO01 Bfd: 58575: %BFD-5-STATE_CHANGE: peer (vrf:default, ip:10.254.3.7, intf:Vlan1546, type:normal) changed state from Up to Down diag CtrlTimeout'
BFD_INIT = 'PE-CENTRO01 Bfd: 58577: %BFD-5-STATE_CHANGE: peer (vrf:default, ip:10.254.3.7, intf:Vlan1546, type:normal) changed state from Down to Init diag None'
BFD_UP = 'PE-CENTRO01 Bfd: 58579: %BFD-5-STATE_CHANGE: peer (vrf:default, ip:10.254.3.7, intf:Vlan1546, type:normal) changed state from Init to Up diag None'
CONFIG = 'PE-ESTE ConfigAgent: 949: %SYS-5-CONFIG_E: Enter configuration mode from console by admin on vty5 (203.0.113.247)'
STP = 'PE-SUR Stp: 597: %SPANTREE-6-STABLE_CHANGE: Stp state is now stable'


@pytest.mark.parametrize('text,kind,status,condition,state', [
    (ADJ_DOWN, 'log_bgp', 'open', 'bgp:PE:default:2001:db8:4003:b100::1', 'Idle'),
    (ADJ_UP, 'log_bgp', 'recovered', 'bgp:PE:default:2001:db8:4003:b100::1', 'Established'),
    (NOTIFICATION, 'log_bgp', 'info', None, ''),
    (LINE_DOWN, 'log_interface', 'open', 'interface:PE:Ethernet26', 'down'),
    (LINE_UP, 'log_interface', 'recovered', 'interface:PE:Ethernet26', 'up'),
    (BFD_DOWN, 'log_bfd', 'open', 'bfd:PE:default:10.254.3.7:Vlan1546', 'Down'),
    (BFD_INIT, 'log_bfd', 'info', 'bfd:PE:default:10.254.3.7:Vlan1546', 'Init'),
    (BFD_UP, 'log_bfd', 'recovered', 'bfd:PE:default:10.254.3.7:Vlan1546', 'Up'),
    (CONFIG, 'log_config', 'info', None, ''),
    (STP, 'log_other', 'info', None, ''),
    ('texto sin formato Arista', 'log_other', 'info', None, ''),
])
def test_classification_of_real_arista_messages(text, kind, status, condition, state):
    result = classify('PE', text)
    assert (result['kind'], result['status'], result['condition_key'], result['original_state']) == (kind, status, condition, state)


def test_binary_payload_on_syslog_input_is_not_stored_as_text():
    # Observed 2026-10-01 from an Internet scanner: a TLS ClientHello received by the syslog input.
    probe = '\ufffd\x01\x00\x00\ufffd\x03\x03\ufffd\ufffd\x12\ufffd\ufffd\x08\x15\ufffd}\ufffd\x10\ufffd*|8N\x1b'
    [event] = adapter([message('tls', 1500, probe, '192.0.2.13', '192.0.2.13')]).read({'start': 1000, 'end': 2000, 'offset': 0}).events
    assert event.title == graylog.UNREADABLE and event.kind == 'log_other' and '\x00' not in event.title
    assert graylog.readable('texto con\x00 un nulo y\ttab') == 'texto con un nulo y\ttab'


def test_severity_comes_from_the_event_code():
    assert classify('PE', NOTIFICATION)['level'] == 3 and classify('PE', STP)['level'] == 6
    assert classify('PE', 'sin código')['level'] is None


def test_reception_time_is_utc_text():
    assert received_at('2026-10-05 03:26:58.704') == 1791170818
    assert stamp_text(1791170818) == '2026-10-05 03:26:58.000'
    with pytest.raises(ConnectorError, match='invalid_source_time'):received_at('2026-10-05T03:26:58Z')


class FakeGraylog:
    """Filters, sorts and pages by reception time like the installed legacy search API."""

    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda m: m['gl2_receive_timestamp'])
        self.calls = []

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        if path == '/system':return {'version': '6.1.16+4a05695'}
        query = params['query']
        if query == '*':rows = self.messages
        else:
            low, high = re.match(r'gl2_receive_timestamp:\["(.+)" TO "(.+)"\}', query).groups()
            rows = [m for m in self.messages if low <= m['gl2_receive_timestamp'] < high]
        page = rows[params['offset']:params['offset'] + params['limit']]
        return {'total_results': len(rows), 'messages': [{'index': 'graylog_20', 'message': m} for m in page]}


def message(key, stamp, text=STP, source='PE-SUR', address='10.254.3.15'):
    # The device timestamp is five hours behind on most PEs and must not matter.
    return {'_id': key, 'gl2_receive_timestamp': stamp_text(stamp)[:-4] + '.250', 'timestamp': '2026-10-04T22:00:00.000Z',
            'source': source, 'message': text, 'gl2_remote_ip': address, 'level': 6}


def adapter(messages):
    a = object.__new__(Graylog);a.get = FakeGraylog(messages);return a


def test_bootstrap_starts_at_oldest_retained_message():
    a = adapter([message('b', 2000), message('a', 1000)])
    result = a.read({'bootstrap': True, 'start': 5000, 'end': 5000, 'offset': 0})
    assert result.events == [] and result.progress == {'start': 1000, 'offset': 0} and result.reason == 'bootstrap'
    empty = adapter([]).read({'bootstrap': True, 'start': 5000, 'end': 5000, 'offset': 0})
    assert empty.progress['start'] == 5000


def test_windows_follow_reception_time_and_overlap():
    stamp = 100000
    first = window({'start': 1000, 'offset': 0}, stamp)
    assert first == {'start': 1000, 'end': 1000 + 6 * 3600, 'offset': 0, 'live': False}
    live = window({'start': stamp - 500, 'offset': 0}, stamp)
    assert live['end'] == stamp - SETTLE and live['live']
    assert window({'start': 1000, 'bound': 1500, 'offset': 0}, stamp)['end'] == 1500
    paging = {'start': 1000, 'end': 2000, 'offset': 200, 'live': True}
    assert window(paging, stamp) == paging


def test_pages_within_a_window_and_overlaps_the_next():
    messages = [message(f'm{i:03}', 1000 + i) for i in range(250)]
    a = adapter(messages)
    first = a.read({'start': 1000, 'end': 2000, 'offset': 0, 'live': False})
    assert len(first.events) == 200 and first.reason == 'pagination' and first.progress['offset'] == 200
    second = a.read(first.progress)
    assert len(second.events) == 50 and second.coverage == 'complete'
    assert second.progress == {'start': 2000, 'offset': 0, 'caught_up': False}
    assert {e.external_id for e in first.events + second.events} == {m['_id'] for m in messages}
    assert first.senders == {'PE-SUR': {'address': '10.254.3.15', 'last_seen': 1199}}


def test_event_record_uses_reception_time_and_reference():
    [event] = adapter([message('x1', 1500, LINE_DOWN)]).read({'start': 1000, 'end': 2000, 'offset': 0, 'live': True}).events
    assert event.occurred_at == event.source_updated_at == 1500 and event.entity_id == 'PE-SUR'
    assert event.status == 'open' and event.severity == 'info' and event.original_severity == '5'
    assert event.reference == 'graylog:graylog_20:x1' and event.title == LINE_DOWN


def test_live_window_overlaps_and_waits_for_next_cycle():
    result = adapter([message('a', 1500)]).read({'start': 1000, 'end': 2000, 'offset': 0, 'live': True})
    assert result.progress == {'start': 2000 - OVERLAP, 'offset': 0, 'caught_up': True}


def test_dense_window_is_split_before_ingesting(monkeypatch):
    monkeypatch.setattr(graylog, 'WINDOW_CAP', 3)
    result = adapter([message(f'm{i}', 1000 + i) for i in range(5)]).read({'start': 1000, 'end': 2000, 'offset': 0})
    assert result.events == [] and result.reason == 'window_split' and result.progress == {'start': 1000, 'bound': 1500, 'offset': 0}


def test_dense_minimum_window_is_truncated_explicitly(monkeypatch):
    monkeypatch.setattr(graylog, 'WINDOW_CAP', 3);monkeypatch.setattr(graylog, 'PAGE_SIZE', 3)
    result = adapter([message(f'm{i}', 1000) for i in range(5)]).read({'start': 1000, 'end': 1060, 'offset': 0})
    assert len(result.events) == 3 and result.coverage == 'partial' and result.reason == 'truncated'
    assert result.progress['start'] == 1060


def test_message_outside_requested_window_is_rejected():
    a = adapter([])
    a.get = lambda path, params=None: {'total_results': 1, 'messages': [{'index': 'graylog_20', 'message': message('a', 5000)}]}
    with pytest.raises(ConnectorError, match='invalid_pagination'):
        a.read({'start': 1000, 'end': 2000, 'offset': 0})


def test_connection_test_reports_version():
    assert adapter([]).test() == {'version': '6.1.16+4a05695', 'scope': 'visible_to_token'}


def configure():
    with session() as db:
        db.add(Integration(kind='graylog', endpoint='http://graylog.example:9000', encrypted_secret=encrypt('fake-test-token'),
            allowed_networks=['192.0.2.0/24'], events_enabled=True));db.commit()


def run_once():
    with session() as db:
        schedule(db);job = claim(db)
    execute(job.id, job.lease_token)


def test_collector_ingests_links_recoveries_and_records_senders(monkeypatch):
    configure();stamp = now()
    messages = [message('d', stamp - 600, ADJ_DOWN, 'PE-CENTRO01', '203.0.113.42'),
                message('u', stamp - 500, ADJ_UP, 'PE-CENTRO01', '203.0.113.42'),
                message('s', stamp - 400)]
    fake = adapter(messages)
    monkeypatch.setattr('odin.events.collector.Graylog', lambda *_: fake)
    run_once()  # bootstrap: locate the oldest message
    with session() as db:
        db.execute(EventCollector.__table__.update().values(next_due=0));db.commit()
    run_once()
    with session() as db:
        events = {e.external_id: e for e in db.scalars(select(SourceEvent))}
        assert set(events) == {'d', 'u', 's'} and events['u'].recovery_of == 'd' and events['s'].status == 'info'
        assert {s.name: s.address for s in db.scalars(select(LogSender))} == {'PE-CENTRO01': '203.0.113.42', 'PE-SUR': '10.254.3.15'}
        collector = db.get(EventCollector, 'graylog:logs')
        assert collector.last_error is None and db.scalar(select(EventStream)).coverage == 'complete'


def test_sender_suggestions_and_confirmed_association(client, administrator, monkeypatch):
    configure();stamp = now()
    fake = adapter([message('a', stamp - 300, LINE_DOWN, 'PE-SUR', '10.254.3.15')])
    monkeypatch.setattr('odin.events.collector.Graylog', lambda *_: fake)
    run_once()
    with session() as db:
        db.execute(EventCollector.__table__.update().values(next_due=0));db.commit()
    run_once()
    with session() as db:
        named = Device(name='pe-sur');other = Device(name='PE-OTRO');db.add_all([named, other]);db.flush()
        db.add(Integration(kind='zabbix', endpoint='https://z.example', allowed_networks=[]));db.flush()
        db.add(SourceIdentity(source='zabbix', external_id='9', device_id=other.id, name='PE-OTRO', addresses=['10.254.3.15'],
            details={}, last_seen=stamp));db.commit();named_id, other_id = named.id, other.id
    enroll(client)
    [sender] = client.get('/api/v1/log-senders').json()
    assert sender['messages'] == 1 and sender['device_id'] is None
    assert {(s['device_id'], tuple(s['reasons'])) for s in sender['suggestions']} == {(named_id, ('name',)), (other_id, ('address',))}
    assert client.put('/api/v1/log-senders/PE-SUR', json={'device_id': named_id}).status_code == 200
    with session() as db:
        assert db.scalar(select(SourceEvent)).device_id == named_id
    assert client.get('/api/v1/log-senders').json()[0]['device_name'] == 'pe-sur'
    assert client.put('/api/v1/log-senders/PE-SUR', json={'device_id': None}).status_code == 200
    with session() as db:
        assert db.scalar(select(SourceEvent)).device_id is None
        assert db.scalar(select(func.count()).select_from(SourceIdentity).where(SourceIdentity.source == 'graylog')) == 0
    assert client.put('/api/v1/log-senders/DESCONOCIDO', json={'device_id': named_id}).status_code == 404


def test_changes_filter_hides_informational_logs(client, administrator, monkeypatch):
    configure();stamp = now()
    fake = adapter([message('a', stamp - 300, LINE_DOWN), message('b', stamp - 200)])
    monkeypatch.setattr('odin.events.collector.Graylog', lambda *_: fake)
    run_once()
    with session() as db:
        db.execute(EventCollector.__table__.update().values(next_due=0));db.commit()
    run_once()
    enroll(client)
    items = client.get('/api/v1/source-events?status=changes').json()['items']
    assert [e['external_id'] for e in items] == ['a']
    assert len(client.get('/api/v1/source-events').json()['items']) == 2
