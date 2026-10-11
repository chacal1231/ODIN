import json
import subprocess
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from odin.db import session
from odin.main import app
from odin.models import (Pop, Device, Integration, SourceIdentity, InventoryInterface, ProbeTarget, ProbeTrace, ProbeSeries,
                         ProbeIncident, ProbeOrigin, now)
from odin.probes import trace
from odin.probes.resolver import ProbeError
from odin.probes.storage import upkeep
from odin.health import trace_checks
from test_probes import target, add_round
from test_access import enroll


def hub(n, host, asn='AS???', loss=0.0, avg=1.5):
    return {'count': n, 'host': host, 'ASN': asn, 'Loss%': loss, 'Snt': 10, 'Last': avg, 'Avg': avg, 'Best': avg - 0.5,
            'Wrst': avg + 2, 'StDev': 0.3}


def report(*hubs):
    return json.dumps({'report': {'mtr': {'src': 'odin', 'dst': 'x', 'tests': 10}, 'hubs': list(hubs)}})


def test_parse_real_format_stops_at_destination():
    hops, reached = trace.parse(report(hub(1, '172.17.0.1'), hub(2, '???', loss=100.0), hub(3, '1.1.1.1', 'AS13335', avg=7.3),
                                       hub(4, '1.1.1.1', 'AS13335')), '1.1.1.1')
    assert reached and len(hops) == 3
    assert hops[1] == {'hop': 2, 'address': None, 'asn': None, 'loss': 100.0, 'sent': 10, 'avg': None, 'best': None, 'worst': None, 'stdev': None}
    assert hops[2]['asn'] == 13335 and hops[2]['avg'] == 7.3 and hops[0]['asn'] is None


def test_unreached_destination_and_total_loss_at_destination():
    assert trace.parse(report(hub(1, '10.0.0.1'), hub(2, '???', loss=100.0)), '8.8.8.8')[1] is False
    assert trace.parse(report(hub(1, '8.8.8.8', loss=100.0)), '8.8.8.8')[1] is False


@pytest.mark.parametrize('output', ['', 'not json', report(hub(2, '10.0.0.1')), report(hub(1, 'host.example')),
                                    report({**hub(1, '10.0.0.1'), 'Avg': -1}), report({**hub(1, '10.0.0.1'), 'Loss%': 'x'}),
                                    report(*[hub(n, '10.0.0.%d' % n) for n in range(1, 32)])])
def test_invalid_output_is_an_error_not_a_path(output):
    with pytest.raises(ProbeError, match='trace_parse_failed'):trace.parse(output, '8.8.8.8')


def test_command_never_takes_hostnames_or_unsafe_addresses(monkeypatch):
    assert trace.command('192.0.2.10', 'ipv4', 'tcp', 443)[-5:] == ['--tcp', '--port', '443', '--', '192.0.2.10']
    assert '--tcp' not in trace.command('2001:db8::1', 'ipv6', 'icmp', None)
    for address in ('127.0.0.1', '169.254.169.254', 'example.com', '-r'):
        with pytest.raises(ValueError):trace.run(address, 'ipv4', 'icmp', None)
    monkeypatch.setattr('odin.probes.trace.subprocess.run', Mock(side_effect=subprocess.TimeoutExpired('mtr', 90)))
    with pytest.raises(ProbeError, match='trace_timeout'):trace.run('192.0.2.10', 'ipv4', 'icmp', None)
    monkeypatch.setattr('odin.probes.trace.subprocess.run', Mock(return_value=subprocess.CompletedProcess([], 1, '', 'mtr: error')))
    with pytest.raises(ProbeError, match='trace_execution_failed'):trace.run('192.0.2.10', 'ipv4', 'icmp', None)


def inventory(db):
    """PE-A with a core port and a transit port to CARRIER; PE-B reached over an internal link."""
    db.add(Integration(kind='librenms', endpoint='http://x', allowed_networks=[]));db.flush()
    pop = Pop(name='BOG');db.add(pop);db.flush()
    ids = {}
    for name, ports in (('PE-A', [('Ethernet1', 'CORE', '10.0.0.1/30'), ('Ethernet51/1', 'TRANSITO-CARRIER-100G', '198.51.100.133/30'),
                                  ('Ethernet2', 'TRANS-TX-NORTE', '10.0.1.1/30')]),
                         ('PE-B', [('Ethernet9', 'TRANS-TX-NORTE', '10.0.1.2/30')])):
        device = Device(name=name, pop_id=pop.id);db.add(device);db.flush();ids[name] = device.id
        identity = SourceIdentity(source='librenms', external_id=name, device_id=device.id, name=name, addresses=[], details={}, last_seen=1)
        db.add(identity);db.flush()
        for n, (port, description, address) in enumerate(ports):
            db.add(InventoryInterface(identity_id=identity.id, external_id=str(n), name=port, kind='port', addresses=[address],
                                      description=description, last_seen=1))
    db.flush()
    return ids


def test_hops_read_against_inventory_and_path_summary():
    with session() as db:
        ids = inventory(db)
        hops = [{'hop': n, 'address': a, 'asn': asn, 'loss': 0.0} for n, (a, asn) in enumerate(
            [('172.17.0.1', None), ('10.0.0.1', None), ('10.0.1.2', None), ('198.51.100.134', 64500), (None, None),
             ('4.69.0.9', 64500), ('4.69.0.200', 64511), ('1.1.1.1', 64511)], 1)]
        read = trace.enrich(hops, trace.Inventory(db), '1.1.1.1')
        assert [h['segment'] for h in read] == ['private', 'own', 'own', 'handoff', 'no_reply', 'external', 'external', 'destination']
        assert (read[1]['device_name'], read[1]['interface']) == ('PE-A', 'Ethernet1')
        # The far end of the internal link answers as PE-B.
        assert read[2]['device_id'] == ids['PE-B']
        assert (read[3]['device_name'], read[3]['interface'], read[3]['role'], read[3]['provider']) == ('PE-A', 'Ethernet51/1', 'transit', 'CARRIER')
        assert 'tránsito CARRIER' in read[3]['label']
        path, fingerprint = trace.summarize(read)
        assert path['last_own']['device_name'] == 'PE-B' and path['handoff']['interface'] == 'Ethernet51/1'
        assert path['asns'] == [64500, 64511]
        assert path['tokens'] == ['device:' + ids['PE-A'], 'device:' + ids['PE-B'], 'port:%s:Ethernet51/1' % ids['PE-A'], 'as:64500', 'as:64511']
        # Another address of the same networks is the same path (equal-cost routes); another transit is not.
        same = [dict(h) for h in read];same[5]['address'] = '4.69.0.10'
        assert trace.summarize(same)[1] == fingerprint
        other = [dict(h) for h in read];other[5]['asn'] = 64999
        assert trace.summarize(other)[1] != fingerprint


def test_baseline_schedule_is_idempotent_and_incidents_go_first():
    with session() as db:
        t = target(db);stamp = trace.slot_for(t.id, now()) + 10
        off = ProbeTarget(name='OFF', address='10.20.0.9', profile_version_id=t.profile_version_id, approved_ips=['10.20.0.9'],
                          effective=t.effective, enabled=False)
        db.add(off);db.commit()
        trace.schedule(db, stamp);trace.schedule(db, stamp + 20)
        assert db.scalar(select(func.count()).select_from(ProbeTrace)) == 1
        # Two minutes late: the slot is not replayed.
        db.execute(ProbeTrace.__table__.delete());db.commit()
        trace.schedule(db, stamp + 200);assert db.scalar(select(func.count()).select_from(ProbeTrace)) == 0
        trace.schedule(db, stamp)
        trace.request(db, t, 'incident_opened', stamp + 5);db.commit()
        first = trace.claim(db, stamp + 6)
        assert db.get(ProbeTrace, first['id']).reason == 'incident_opened'
        assert first['address'] == '10.20.0.2' and first['protocol'] == 'icmp' and first['port'] is None
        second = trace.claim(db, stamp + 6);assert db.get(ProbeTrace, second['id']).reason == 'baseline'
        assert trace.claim(db, stamp + 6) is None
        # An interrupted run is failed, never retried.
        trace.claim(db, stamp + 6 + trace.LEASE + 1)
        db.expire_all();assert db.get(ProbeTrace, first['id']).error_code == 'trace_interrupted'
        assert trace.request(db, off, 'baseline', stamp)
        db.commit();db.get(ProbeTarget, off.id).archived = True;db.commit()
        assert trace.claim(db, stamp + 7) == {'skipped': True}


def test_tcp_checks_trace_with_tcp_and_late_baselines_are_dropped():
    with session() as db:
        t = target(db, protocol='https', check_config={'port': 8443});stamp = now()
        trace.request(db, t, 'baseline', stamp - trace.INTERVAL - 1);db.commit()
        assert trace.claim(db, stamp) is None
        assert db.scalar(select(ProbeTrace.error_code)) == 'trace_skipped'
        trace.request(db, t, 'manual', stamp);db.commit()
        task = trace.claim(db, stamp);assert (task['protocol'], task['port']) == ('tcp', 8443)


def test_address_no_longer_measured_is_not_traced():
    with session() as db:
        t = target(db);trace.request(db, t, 'manual', now())
        t.approved_ips = ['10.20.0.3'];db.commit()
        assert trace.claim(db) == {'skipped': True}
        assert db.scalar(select(ProbeTrace.error_code)) == 'trace_address_changed'


def run_with(monkeypatch, db, t, hops, reached=True):
    trace.request(db, t, 'manual', now());db.commit()
    task = trace.claim(db)
    monkeypatch.setattr('odin.probes.trace.run', lambda *_: (hops, reached))
    trace.execute(task)
    db.expire_all();return db.get(ProbeTrace, task['id'])


def test_execute_compares_with_previous_reached_path(monkeypatch):
    def hops(asn):return [{'hop': 1, 'address': '4.69.0.1', 'asn': asn, 'loss': 0.0}, {'hop': 2, 'address': '10.20.0.2', 'asn': 64511, 'loss': 0.0}]
    with session() as db:
        t = target(db)
        first = run_with(monkeypatch, db, t, hops(64500))
        assert first.status == 'done' and first.reached and first.path_changed is None and first.path['asns'] == [64500, 64511]
        assert run_with(monkeypatch, db, t, hops(64500)).path_changed is False
        assert run_with(monkeypatch, db, t, hops(64501), reached=False).path_changed is None
        assert run_with(monkeypatch, db, t, hops(64501)).path_changed is True
        trace.request(db, t, 'manual', now());db.commit();task = trace.claim(db)
        monkeypatch.setattr('odin.probes.trace.run', Mock(side_effect=ProbeError('trace_timeout')))
        trace.execute(task);db.expire_all()
        failed = db.get(ProbeTrace, task['id']);assert (failed.status, failed.error_code, failed.hops) == ('failed', 'trace_timeout', [])


def test_incident_open_and_recovery_request_traces():
    with session() as db:
        t = target(db);start = now() // 300 * 300 - 1800
        s = ProbeSeries(target_id=t.id, address=t.address, epoch=t.epoch, started_at=start, config=t.effective)
        db.add(s);db.commit()
        for i in range(3):add_round(db, t, s, start + i * 30, 0)
        incident = db.scalar(select(ProbeIncident))
        opened = db.scalar(select(ProbeTrace).where(ProbeTrace.incident_id == incident.id))
        assert (opened.reason, opened.address, opened.slot) == ('incident_opened', '10.20.0.2', start + 60)
        for i in range(3, 25):add_round(db, t, s, start + i * 30, 10)
        reasons = db.scalars(select(ProbeTrace.reason).where(ProbeTrace.incident_id == incident.id).order_by(ProbeTrace.slot)).all()
        assert reasons == ['incident_opened', 'incident_recovered']


def test_retention_keeps_changes_and_incident_traces():
    with session() as db:
        t = target(db);stamp = now()
        for reason, age, changed in (('baseline', 1, False), ('baseline', 4, False), ('baseline', 5, True), ('baseline', 91, True),
                                     ('manual', 30, None), ('manual', 91, None), ('incident_opened', 200, None)):
            key = trace.request(db, t, reason, stamp - age * 86400)
            db.get(ProbeTrace, key).path_changed = changed
        db.commit();upkeep(db, stamp)
        kept = sorted((r.reason, (stamp - r.slot) // 86400) for r in db.scalars(select(ProbeTrace)))
        assert kept == [('baseline', 1), ('baseline', 5), ('incident_opened', 200), ('manual', 30)]


def test_tracer_health():
    with session() as db:
        assert trace_checks(db, now()) == []
        target(db)
        assert trace_checks(db, now())[0]['title'] == 'Trazas detenidas'
        db.add(ProbeOrigin(id='tracer', heartbeat=now()));db.commit()
        assert trace_checks(db, now()) == []
        db.get(ProbeOrigin, 'tracer').error_code = 'mtr_unavailable';db.commit()
        assert 'mtr_unavailable' in trace_checks(db, now())[0]['detail']


def test_api_manual_trace_by_operator_and_detail(client, administrator):
    enroll(client)
    created = client.post('/api/v1/admin/users', json={'username': 'operator'}).json()
    with session() as db:t = target(db);t_id = t.id
    with TestClient(app, base_url='http://localhost:8080', headers={'Origin': 'http://localhost:8080'}) as operator:
        enroll(operator, 'operator', created['temporary_password'])
        first = operator.post(f'/api/v1/targets/{t_id}/traces')
        assert first.status_code == 202 and first.json()['status'] == 'pending' and first.json()['reason'] == 'manual'
        # A pending manual trace is reused, not queued twice.
        assert operator.post(f'/api/v1/targets/{t_id}/traces').json()['id'] == first.json()['id']
        listed = operator.get(f'/api/v1/targets/{t_id}/traces').json()
        assert [x['id'] for x in listed] == [first.json()['id']] and 'hops' not in listed[0]
        assert operator.get('/api/v1/traces/' + first.json()['id']).json()['hops'] == []
        assert operator.get('/api/v1/traces/nope').status_code == 404
        assert operator.post('/api/v1/targets/nope/traces').status_code == 404
    with session() as db:
        db.get(ProbeTarget, t_id).archived = True;db.commit()
    assert client.post(f'/api/v1/targets/{t_id}/traces').status_code == 404


def test_api_target_without_addresses(client, administrator):
    enroll(client)
    with session() as db:
        t = target(db, dns_policy='public_dns');t.address = 'svc.example.com';t.approved_ips = [];t.resolved_ips = [];db.commit();t_id = t.id
    response = client.post(f'/api/v1/targets/{t_id}/traces')
    assert response.status_code == 422
