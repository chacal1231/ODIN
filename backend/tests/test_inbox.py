"""ODIN's inbox: needs attention, in progress and resolved in the shift, with links that open each item."""
from zoneinfo import ZoneInfo
from sqlalchemy import select
from odin.db import session
from odin.models import Analysis, AnalysisReview, Case, CaseNote, CorrelationState, ProbeSeries, ProbeIncident, User, now, uid
from odin.security import hasher
from odin.inbox import build
from odin.olts import service as olts
from test_access import enroll
from test_probes import target, add_round
from test_providers import carrier_case
from test_olts import inventory_data, OUTAGE


def kinds(section):
    return [(x['kind'], x['severity']) for x in section]


def test_empty_installation():
    with session() as db:
        result = build(db)
    # Only ODIN's own health: the probe has not started yet.
    assert {x['kind'] for x in result['attention']} == {'health'} and (result['progress'], result['resolved']) == ([], [])
    assert result['counts'] == {'Casos': 0, 'Incidentes': 0, 'OLTs': 0} and result['shift_hours'] == 12
    assert [c['id'] for c in result['health']][:3] == ['probe', 'tracer', 'smartolt']


def test_sections_and_links():
    stamp = now()
    with session() as db:
        db.add(CorrelationState(id='cases', next_due=stamp, alerts_enabled=True));db.flush()
        noc = User(username='ana', role='operator', password_hash=hasher.hash('x' * 20));db.add(noc);db.flush()
        open_case = carrier_case(db, stamp - 900)
        acked = carrier_case(db, stamp - 600)
        acked.subject, acked.acknowledged_at, acked.acknowledged_by = '198.51.100.138', stamp - 300, noc.id
        closed = carrier_case(db, stamp - 7200)
        closed.state, closed.closed_at, closed.last_state = 'closed', stamp - 1800, 'up'
        old = carrier_case(db, stamp - 90000)
        old.state, old.closed_at = 'closed', stamp - 80000  # before the shift
        db.add(Analysis(id=uid(), kind='case', case_id=closed.id, requested_by='odin', status='done', finished_at=stamp - 1700,
                        progress={}, pack={}, result={'resumen': 'Cayó el tránsito'}))
        db.add(Analysis(id=uid(), kind='case', case_id=open_case.id, requested_by='odin', status='running', stage='writing', progress={}, pack={}))
        db.add(CaseNote(case_id=acked.id, author_id=noc.id, text='Proveedor (Telegram): Ticket #4411, ETA 2 h', created_at=stamp - 100))
        db.add(CaseNote(case_id=acked.id, author_id=noc.id, text='Nota interna', created_at=stamp - 90))
        t = target(db);series = ProbeSeries(target_id=t.id, address=t.address, epoch=t.epoch, started_at=stamp - 1800, config=t.effective)
        db.add(series);db.commit()
        for i in range(3):add_round(db, t, series, stamp - 1800 + i * 30, 0)
        incident_id = db.scalar(select(ProbeIncident.id))
        olts.write_inventory(db, inventory_data(), stamp - 60)
        olts.write_outages(db, {'outages': {'1': OUTAGE}}, stamp - 600, ZoneInfo('UTC'))
        olts.write_outages(db, {'outages': {'1': {**OUTAGE, 'sections': [OUTAGE['sections'][0]]}}}, stamp - 300, ZoneInfo('UTC'))
        db.commit()
        result = build(db, stamp)
        ids = {'open': open_case.id, 'acked': acked.id, 'closed': closed.id, 'old': old.id}
    attention = result['attention']
    # Critical network problems first (oldest first), then ODIN's own health, then what only asks for a rating.
    order = [x['kind'] for x in attention]
    assert set(order[:4]) == {'case', 'incident', 'olt_outage', 'olt'} and all(x['severity'] == 'critical' for x in attention[:4])
    assert order.index('health') > 3 and order[-1] == 'review'
    by_kind = {x['kind']: x for x in attention}
    assert by_kind['case']['link'] == '#caso=' + ids['open'] and by_kind['case']['title'] == 'Sesión BGP 198.51.100.134'
    assert by_kind['incident']['link'] == '#incidente=' + incident_id and by_kind['incident']['title'] == 'PE-LAB'
    assert by_kind['incident']['conditions'] == ['unreachable']
    outage = by_kind['olt_outage']
    assert (outage['link'], outage['title'], outage['where']) == ('#olt=1', 'PON 1/2 · Corte de fibra (LOS)', 'OLT-Norte')
    assert outage['detail'] == '61 de 64 ONUs · zona Norte · 3 splitters'
    assert by_kind['olt']['title'] == 'Uplink xgei_1/4/1 caído'
    review = next(x for x in attention if x['kind'] == 'review')
    assert review['link'] == '#caso=' + ids['closed'] and review['detail'] == 'Cayó el tránsito'
    progress = {x['kind']: x for x in result['progress']}
    assert progress['case']['acknowledged_by'] == 'ana' and progress['case']['id'] == ids['acked']
    assert progress['analysis']['link'] == '#caso=' + ids['open'] and progress['analysis']['detail'] == 'writing'
    assert progress['note']['title'] == 'Ticket #4411, ETA 2 h' and progress['note']['detail'] == 'ana'
    assert [x['kind'] for x in result['progress']].count('note') == 1  # internal notes are not provider news
    resolved = {(x['kind'], x['id']) for x in result['resolved']}
    assert ('case_closed', ids['closed']) in resolved and ('case_closed', ids['old']) not in resolved
    assert any(k == 'olt_outage_ended' for k, _ in resolved)  # the power failure ended at stamp - 300
    closed_item = next(x for x in result['resolved'] if x['id'] == ids['closed'])
    assert closed_item['analysis_id'] and closed_item['rated'] is False
    assert result['counts'] == {'Casos': 1, 'Incidentes': 1, 'OLTs': 2}
    with session() as db:
        db.add(AnalysisReview(analysis_id=closed_item['analysis_id'], author_id='x', verdict='correct', correction=''));db.commit()
        again = build(db, stamp)
    assert next(x for x in again['resolved'] if x['id'] == ids['closed'])['rated'] is True
    assert not any(x['kind'] == 'review' for x in again['attention'])


def test_inbox_requires_a_session(client, administrator):
    assert client.get('/api/v1/inbox').status_code == 401
    enroll(client)
    assert set(client.get('/api/v1/inbox').json()) >= {'attention', 'progress', 'resolved', 'counts', 'health'}
