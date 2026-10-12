from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import select, text, func
from odin.db import session
from odin.models import ProbeTarget, ProbeSeries, ProbeRound, ProbeIncident, ProbeBaseline, ProbeState, Job, now, uid
from odin.probes import baseline
from odin.probes.config import ProbeConfig
from odin.probes.service import snapshot
from test_probes import target, add_round
from test_access import enroll

DAY = 86400


def rounds(start, days, interval=300, value=lambda slot: 40.0, address='10.20.0.2'):
    return [(address, slot, [value(slot)] * 10) for slot in range(start, start + int(days * DAY), interval)]


def test_summary_base_spread_jitter_loss_and_peak():
    stamp = 1_800_000_000 // DAY * DAY
    data = rounds(stamp - 2 * DAY, 2, value=lambda slot: 100.0 if slot % 7200 == 0 else 40.0)
    data.append(('10.20.0.2', stamp - 600, [40, 42, None, 46, 40, 40, 40, 40, 40, 40]))
    m = baseline.summarize(data, 300, stamp, 'America/Bogota')
    # Isolated spikes do not move the median.
    assert m['base_p95'] == 40 and m['mad'] == 0 and m['peak_p95'] == 40 and m['valley_p95'] == 40
    assert m['days'] == 2 and m['coverage'] == 1 and m['windows'] == 576
    assert m['loss_percent'] == pytest.approx(100 / 5770, abs=0.01) and m['jitter_base'] == 0
    assert baseline.summarize([], 300, stamp) is None


def metrics(base, mad=0, jitter=None, loss=0):
    return {'base_p95': base, 'mad': mad, 'jitter_base': jitter, 'loss_percent': loss}


def test_proposal_margins_ceiling_brake_and_loss():
    # Valve Lima: the margin wins over the ratio for the warning.
    assert baseline.propose(metrics(38), 'icmp', ['juegos'], None, 5) == ({'rtt_warning': 58, 'rtt_critical': 95}, [])
    # Bogotá at 1.5 ms: margins keep a fast destination from alerting on noise.
    assert baseline.propose(metrics(1.5, jitter=0.2), 'icmp', [], None, 5)[0] == {
        'rtt_warning': 21.5, 'rtt_critical': 51.5, 'jitter_warning': 5.2, 'jitter_critical': 15.2}
    assert baseline.propose(metrics(300), 'https', [], None, 5)[0] == {'rtt_warning': 600, 'rtt_critical': 1200}
    # Spread: a noisy destination gets room above its base.
    assert baseline.propose(metrics(40, mad=10), 'icmp', [], None, 5)[0]['rtt_warning'] == pytest.approx(114.1)
    # Ceiling for games, unless the destination itself is slower than it.
    assert baseline.propose(metrics(70), 'icmp', ['juegos'], None, 5)[0]['rtt_critical'] == 150
    proposal, flags = baseline.propose(metrics(134), 'icmp', ['juegos'], None, 5)
    assert proposal['rtt_critical'] == 335 and flags == ['above_ceiling']
    # Brake: at most 25 % per step from what is applied.
    proposal, flags = baseline.propose(metrics(100), 'icmp', [], {'rtt_warning': 50, 'rtt_critical': 100}, 5)
    assert proposal == {'rtt_warning': 62.5, 'rtt_critical': 125} and flags == ['step_limited']
    assert baseline.propose(metrics(40, loss=6), 'icmp', [], None, 5)[1] == ['lossy']


def test_overlay_respects_mode_protocol_packets_and_damping():
    config = {**ProbeConfig().model_dump(), 'protocol': 'icmp'}
    adaptive = {'protocol': 'icmp', 'thresholds': {'rtt_warning': 58, 'rtt_critical': 95, 'jitter_warning': 5, 'jitter_critical': 15},
                'damping': {'persistence': 4, 'until': 1000}}
    t = SimpleNamespace(threshold_mode='proposed', adaptive=adaptive)
    result = baseline.overlay(t, config, 999)
    assert (result['rtt_warning'], result['jitter_critical'], result['persistence']) == (58, 15, 4)
    assert baseline.overlay(t, config, 1000)['persistence'] == 2
    assert baseline.overlay(SimpleNamespace(threshold_mode='manual', adaptive=adaptive), config, 0) == config
    assert baseline.overlay(t, {**config, 'protocol': 'https'}, 2000)['rtt_warning'] is None
    assert baseline.overlay(t, {**config, 'packets': 1}, 2000)['jitter_warning'] is None


def partitions(db, start, end):
    for day in range(start // DAY * DAY, end + DAY, DAY):
        suffix = datetime.fromtimestamp(day, timezone.utc).strftime('%Y%m%d')
        db.execute(text(f'CREATE TABLE IF NOT EXISTS probe_rounds_d{suffix} PARTITION OF probe_rounds FOR VALUES FROM ({day}) TO ({day + DAY})'))


def history(db, t, days, value=40.0, stamp=None):
    stamp = stamp or now() // 300 * 300;start = stamp - int(days * DAY)
    t.effective = ProbeConfig(interval_seconds=300).model_dump();db.commit()
    partitions(db, start, stamp)
    s = ProbeSeries(target_id=t.id, address=t.address, epoch=t.epoch, started_at=start, config=snapshot(t));db.add(s);db.flush()
    db.add_all(ProbeRound(series_id=s.id, slot=slot, batch_id=uid(), status='valid', sent=10, received=10, samples=[value] * 10,
                          finished_at=slot) for slot in range(start + 300, stamp, 300))
    db.commit()
    return s, stamp


def test_run_learns_applies_weekly_and_excludes_incidents():
    with session() as db:
        t = target(db);t.threshold_mode = 'auto';t.categories = ['juegos'];db.commit()
        s, stamp = history(db, t, 8)
        # A fault with 500 ms readings is not part of normal behaviour.
        db.query(ProbeRound).filter(ProbeRound.slot > stamp - 3 * DAY, ProbeRound.slot <= stamp - 2 * DAY).update({'samples': [500.0] * 10})
        db.add(ProbeIncident(target_id=t.id, address=t.address, opened_at=stamp - 3 * DAY, closed_at=stamp - 2 * DAY, severity='critical',
                             conditions={}, profile={}));db.commit()
        assert baseline.run(db, stamp) == {'computed': 1, 'applied': 1, 'damped': 0}
        row = db.scalar(select(ProbeBaseline))
        assert row.status == 'ready' and row.metrics['base_p95'] == 40 and row.proposal == {'rtt_warning': 60, 'rtt_critical': 100,
                                                                                          'jitter_warning': 5, 'jitter_critical': 15}
        db.refresh(t)
        assert t.adaptive['thresholds']['rtt_warning'] == 60 and t.adaptive['applied_by'] == 'auto' and t.version == 1 and t.epoch == 1
        # Hourly passes do not recompute; a forced one does, but the weekly step waits.
        assert baseline.run(db, stamp + 3600)['computed'] == 0
        assert baseline.run(db, stamp + 3600, force=True) == {'computed': 1, 'applied': 0, 'damped': 0}
        t.adaptive = {**t.adaptive, 'applied_at': stamp - 7 * DAY};db.commit()
        assert baseline.run(db, stamp + 3600, force=True)['applied'] == 1


def test_provisional_after_a_day_and_nothing_before():
    with session() as db:
        t = target(db);history(db, t, 0.5)
        baseline.run(db, now() // 300 * 300)
        assert db.scalar(select(ProbeBaseline)).status == 'insufficient'
        db.execute(text('DELETE FROM probe_baselines'));db.commit()
        db.execute(text('DELETE FROM probe_rounds'));db.execute(text('DELETE FROM probe_series'));db.commit()
        history(db, t, 2)
        baseline.run(db, now() // 300 * 300)
        row = db.scalar(select(ProbeBaseline))
        # Proposed mode: computed, never applied by itself.
        assert row.status == 'provisional' and row.proposal and not row.applied_at and not db.get(ProbeTarget, t.id).adaptive


def test_detector_uses_applied_thresholds_without_new_series():
    with session() as db:
        t = target(db);t.effective = ProbeConfig(interval_seconds=300).model_dump()
        t.adaptive = {'protocol': 'icmp', 'thresholds': {'rtt_warning': 58, 'rtt_critical': 95}};db.commit()
        start = now() // 300 * 300 - 900;partitions(db, start, start + 900)
        s = ProbeSeries(target_id=t.id, address=t.address, epoch=t.epoch, started_at=start, config=snapshot(t));db.add(s);db.commit()
        for n in range(2):add_round(db, t, s, start + 300 * n, value=120)
        state = db.scalar(select(ProbeState))
        assert state.conditions == {'latency': 'critical'} and state.series_id == s.id
        incident = db.scalar(select(ProbeIncident))
        assert incident.profile['rtt_critical'] == 95


def test_flapping_check_gets_more_persistence_for_a_day():
    with session() as db:
        t = target(db);stamp = now()
        for n in range(4):
            db.add(ProbeIncident(target_id=t.id, address=t.address, opened_at=stamp - 3600 * (n + 1), closed_at=stamp - 3600 * (n + 1) + 120,
                                 severity='warning', conditions={}, profile={}))
        db.commit()
        assert baseline.run(db, stamp)['damped'] == 1
        db.refresh(t)
        assert t.adaptive['damping'] == {'persistence': 4, 'until': stamp + DAY, 'short_incidents': 4}
        baseline.run(db, stamp + DAY + 1)
        db.refresh(t)
        assert 'damping' not in t.adaptive


def test_api_mode_approval_bulk_and_recompute(client, administrator):
    enroll(client)
    with session() as db:
        t = target(db);history(db, t, 2);baseline.run(db, now() // 300 * 300)
        target_id, version = t.id, t.version
        proposal = db.scalar(select(ProbeBaseline.id))
    view = client.get('/api/v1/targets').json()[0]
    assert view['threshold_mode'] == 'proposed' and view['baseline']['status'] == 'provisional'
    assert client.post(f'/api/v1/targets/{target_id}/baselines/{proposal}/apply').json()['adaptive']['thresholds']['rtt_warning'] == 60
    assert client.post('/api/v1/baselines/apply-proposals').json() == {'applied': 0}
    changed = client.put(f'/api/v1/targets/{target_id}/threshold-mode', json={'mode': 'manual'}).json()
    assert changed['threshold_mode'] == 'manual' and changed['version'] == version and changed['epoch'] == 1
    assert client.post(f'/api/v1/targets/{target_id}/baselines/{proposal}/apply').status_code == 409
    assert client.get(f'/api/v1/targets/{target_id}/baselines').json()[0]['applied_by']
    assert client.post('/api/v1/baselines/recompute').status_code == 202
    with session() as db:
        assert db.scalar(select(Job).where(Job.kind == 'probe_baselines')).payload == {'force': True}
