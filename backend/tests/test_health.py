from sqlalchemy import delete
from odin.db import session
from odin.models import ProbeOrigin, ProbeBatch, Integration, EventCollector, Job, now
from odin.health import evaluate, expected_slots, RATE_WINDOW, RATE_SETTLE
from odin.probes.service import slot_at
from odin.security import encrypt
from test_access import enroll
from test_probes import target


def origin(db, stamp, age=0):
    db.add(ProbeOrigin(id='odin', heartbeat=stamp - age));db.commit()


def scheduled(db, t, stamp, keep=1.0):
    """Batches for the slots under evaluation; keep is the fraction actually produced."""
    end = stamp - RATE_SETTLE;start = end - RATE_WINDOW;interval = t.effective['interval_seconds']
    slots = list(range(slot_at(t, end - 1), start - 1, -interval))
    assert len(slots) == expected_slots(t, start, end)
    for slot in slots[:round(len(slots) * keep)]:
        db.add(ProbeBatch(target_id=t.id, epoch=t.epoch, slot=slot, mode='scheduled', status='done', finished_at=slot + 5, config={}))
    db.commit()


def old_target(db, stamp):
    t = target(db);t.created_at = t.changed_at = stamp - 3600;db.commit();return t


def collector(db, stamp, enabled=True, success_age=10, error=None):
    db.add(Integration(kind='zabbix', endpoint='https://example.test', encrypted_secret=encrypt('fake-test-token'),
        allowed_networks=['192.0.2.0/24'], events_enabled=enabled, updated_at=stamp - 86400))
    db.flush()
    db.add(EventCollector(id='zabbix:events', source='zabbix', topic='events', stream_name='events:x', config_version=1,
        next_due=stamp, last_success_at=None if success_age is None else stamp - success_age, last_error=error, failure_count=3 if error else 0))
    db.commit()


def test_quiet_installation_is_healthy():
    with session() as db:
        stamp = now();origin(db, stamp)
        assert evaluate(db, stamp) == {'state': 'healthy', 'checks': [], 'evaluated_at': stamp}


def test_silent_probe_is_critical_and_rate_is_not_judged():
    with session() as db:
        stamp = now();origin(db, stamp, age=120);old_target(db, stamp)
        result = evaluate(db, stamp)
        assert result['state'] == 'critical' and [c['id'] for c in result['checks']] == ['probe']
        assert '120 s' in result['checks'][0]['detail']


def test_skipped_rounds_raise_rate_alarm():
    with session() as db:
        stamp = now();origin(db, stamp);t = old_target(db, stamp)
        scheduled(db, t, stamp)
        assert evaluate(db, stamp)['checks'] == []
        db.execute(delete(ProbeBatch));db.commit()
        scheduled(db, t, stamp, keep=0.7)
        assert [(c['id'], c['state']) for c in evaluate(db, stamp)['checks']] == [('probe_rate', 'warning')]
        db.execute(delete(ProbeBatch));db.commit()
        scheduled(db, t, stamp, keep=0.3)
        assert [(c['id'], c['state']) for c in evaluate(db, stamp)['checks']] == [('probe_rate', 'critical')]


def test_recently_changed_target_is_not_judged():
    with session() as db:
        stamp = now();origin(db, stamp);target(db)
        assert evaluate(db, stamp)['checks'] == []


def test_stalled_event_reader_is_reported_with_its_error():
    with session() as db:
        stamp = now();origin(db, stamp);collector(db, stamp, success_age=400, error='revision_conflict')
        [c] = evaluate(db, stamp)['checks']
        assert c['id'] == 'events:zabbix:events' and c['state'] == 'warning' and c['page'] == 'Eventos'
        assert c['error_code'] == 'revision_conflict' and c['failures'] == 3
        db.get(EventCollector, 'zabbix:events').last_success_at = stamp - 4000;db.commit()
        assert evaluate(db, stamp)['state'] == 'critical'


def test_fresh_or_paused_readers_are_quiet_and_never_read_is_measured_from_activation():
    with session() as db:
        stamp = now();origin(db, stamp);collector(db, stamp)
        assert evaluate(db, stamp)['checks'] == []
        row = db.get(EventCollector, 'zabbix:events');row.last_success_at = None;db.commit()
        assert evaluate(db, stamp)['state'] == 'critical'
        db.get(Integration, 'zabbix').events_enabled = False;db.commit()
        assert evaluate(db, stamp)['checks'] == []


def test_overdue_jobs_mean_worker_is_behind():
    with session() as db:
        stamp = now();origin(db, stamp)
        db.add(Job(kind='event_poll', idempotency_key='due', run_after=stamp - 60));db.commit()
        assert evaluate(db, stamp)['checks'] == []
        db.add(Job(kind='inventory', idempotency_key='late', run_after=stamp - 1000));db.commit()
        [c] = evaluate(db, stamp)['checks']
        assert c['id'] == 'jobs' and c['state'] == 'critical' and '1 trabajo espera' in c['detail']


def test_monitoring_includes_odin_health(client, administrator):
    enroll(client)
    health = client.get('/api/v1/monitoring').json()['odin_health']
    assert health['state'] in ('healthy', 'warning', 'critical') and isinstance(health['checks'], list)
