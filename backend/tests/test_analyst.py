import json
import pytest
from sqlalchemy import select
from odin.db import session
from odin.models import (Analysis, AnalystState, Case, CaseEvidence, CaseNote, Integration, Job, now)
from odin.jobs import claim
from odin.security import encrypt
from odin.inventory.connectors import ConnectorError
from odin.analyst import service
from odin.analyst.packs import case_pack, fit, reading
from odin.analyst.analysis import parse
from odin.health import evaluate
from test_access import enroll
from test_cases import event, flap, correlate, cases, BGP, PEER

NOTIFY = f'PE-A Bgp: 1: %BGP-3-NOTIFICATION: received from neighbor {PEER} (VRF default AS 64500) 6/1 (Cease/maximum number of prefixes reached) 0 bytes'
CONFIG = 'PE-A ConfigAgent: 2: %SYS-5-CONFIG_STARTUP: Startup config saved from system:/running-config by admin on vty6 (10.0.0.9).'


def answer(refs=('E1',), summary='Resumen'):
    return json.dumps({'resumen': summary, 'hechos': [{'afirmacion': 'Hecho', 'evidencias': list(refs)}], 'hipotesis': [],
                       'datos_faltantes': [], 'recomendaciones': []})


class FakeLlm:
    """Stands in for llama-server: counts characters as tokens and replays scripted answers.

    Planning calls (the planner's schema) are answered from `plans`, the general plan when it is empty, and kept in
    `plan_calls`, so `answers` and `calls` stay the answering calls.
    """
    answers = []
    calls = []
    plans = []
    plan_calls = []

    def __init__(self, endpoint, secret, networks, heartbeat=lambda: None):
        self.heartbeat = heartbeat

    def props(self):
        return {'model': 'qwen-test.gguf', 'context': 262144, 'slots': 1, 'build': 'b1'}

    def count(self, messages):
        return sum(len(m['content']) for m in messages) // 4

    def chat(self, messages, schema, max_tokens, progress=lambda *a: None, temperature=0.2):
        from odin.analyst import planner
        if schema is planner.SCHEMA:
            FakeLlm.plan_calls.append(messages)
            item = FakeLlm.plans.pop(0) if FakeLlm.plans else json.dumps(planner.GENERAL)
            if isinstance(item, Exception):raise item
            return item, {}, {}
        FakeLlm.calls.append(messages)
        progress('reading', 10, 10)
        item = FakeLlm.answers.pop(0)
        if isinstance(item, Exception):raise item
        return item, {'prompt_tokens': 100, 'completion_tokens': 20}, {}


@pytest.fixture
def llm(monkeypatch):
    FakeLlm.answers, FakeLlm.calls, FakeLlm.plans, FakeLlm.plan_calls = [], [], [], []
    monkeypatch.setattr(service, 'Llm', FakeLlm)
    with session() as db:
        db.add(Integration(kind='llama_server', endpoint='http://172.18.0.1:8081', encrypted_secret=encrypt('k'),
                           allowed_networks=['172.18.0.1/32'], events_enabled=True))
        db.commit()
    return FakeLlm


def one_case():
    t = now() - 3 * 86400
    with session() as db:
        flap(db, t, 2)
        event(db, t + 3, title=CONFIG, kind='log_config')
        event(db, t + 30, title=NOTIFY)
        event(db, t + 31, title=NOTIFY.replace(': 1:', ': 7:'))
        db.commit()
    correlate(now())
    return cases()[0]


def run_jobs():
    """What the analyst process does: claim analyst jobs only and execute them."""
    while True:
        with session() as db:
            job = claim(db, kinds=service.KINDS)
        if not job:return
        service.execute(job.id, job.lease_token)


def test_readings_say_who_closed_the_session_and_what_a_config_save_means():
    assert any('fue el vecino quien cerró la sesión' in n for n in reading(NOTIFY))
    assert any('este router le anunció más de los permitidos' in n for n in reading(NOTIFY))
    assert any('fue este router' in n for n in reading(NOTIFY.replace('received from', 'sent to')))
    assert any('configuración' in n for n in reading(CONFIG))


def test_case_pack_groups_repeats_and_marks_moments_near_the_first_drop():
    case = one_case()
    with session() as db:
        db.add(CaseNote(case_id=case.id, author_id='x', text='El NOC llamó al proveedor'));db.commit()
        pack = case_pack(db, db.get(Case, case.id))
    text = pack.text()
    # Two notifications with different sequence numbers are one line with a count.
    notify = next(l for l in pack.lines if 'NOTIFICATION' in l.text)
    assert notify.ref['count'] == 2 and '2 veces' in notify.text
    config = next(l for l in pack.lines if 'CONFIG_STARTUP' in l.text)
    assert '3 s después de la primera caída' in config.text
    assert any(l.label.startswith('N') and 'llamó al proveedor' in l.text for l in pack.lines)
    assert 'Cobertura de fuentes' in text and 'Logs (Graylog): no se lee' in text
    assert set(pack.refs()) == {l.label for l in pack.lines}


def test_fit_drops_least_important_groups_and_reports_them():
    case = one_case()
    with session() as db:
        pack = case_pack(db, db.get(Case, case.id))
    full = len(pack.text()) // 4
    triggers = [l.label for l in pack.lines if l.priority == 0]
    tokens = fit(pack, lambda text: len(text) // 4, full - 20)
    assert tokens <= full - 20 and pack.omitted
    assert all(label in pack.refs() for label in triggers)
    assert 'no se incluyen por espacio' in pack.text()
    with pytest.raises(ValueError):
        fit(pack, lambda text: len(text) // 4, 10)


def test_parse_rejects_unknown_references():
    refs = {'E1': {}, 'N1': {}}
    assert parse(answer(['E1', 'N1']), refs)[1] is None
    assert parse(answer(['E1', 'E9']), refs)[1] == 'unknown_refs:E9'
    assert parse('{"resumen": ', refs)[1] == 'invalid_json'


def test_case_analysis_runs_in_the_analyst_process_only(client, administrator, llm):
    case = one_case()
    enroll(client)
    llm.answers = [answer(['E1'])]
    first = client.post(f'/api/v1/cases/{case.id}/analyses').json()
    # A second request while one is queued returns the same analysis.
    assert client.post(f'/api/v1/cases/{case.id}/analyses').json()['id'] == first['id']
    with session() as db:
        assert claim(db, exclude=service.KINDS) is None
    run_jobs()
    listed = client.get(f'/api/v1/cases/{case.id}/analyses').json()
    done = listed['items'][0]
    assert done['status'] == 'done' and done['result']['hechos'][0]['evidencias'] == ['E1']
    assert done['refs']['E1']['ids'] and done['input_version'] == listed['version'] and done['model'] == 'qwen-test.gguf'
    # The model reads the pack with external text quoted and never as instructions.
    assert 'nunca instrucciones' in llm.calls[0][0]['content'] and 'E1 ·' in llm.calls[0][1]['content']
    with session() as db:
        assert db.get(AnalystState, 'analyst').last_success_at
    # A new note changes the case version: the analysis shows as outdated.
    client.post(f'/api/v1/cases/{case.id}/notes', json={'text': 'Revisado'})
    assert client.get(f'/api/v1/cases/{case.id}/analyses').json()['version'] != done['input_version']


def test_unknown_references_get_one_repair_then_fail(client, administrator, llm):
    case = one_case()
    enroll(client)
    llm.answers = [answer(['E99']), answer(['E1'])]
    client.post(f'/api/v1/cases/{case.id}/analyses');run_jobs()
    item = client.get(f'/api/v1/cases/{case.id}/analyses').json()['items'][0]
    assert item['status'] == 'done' and 'E99' in llm.calls[1][-1]['content']
    llm.answers = [answer(['E99']), answer(['E98'])]
    row = client.post(f'/api/v1/cases/{case.id}/analyses').json();run_jobs()
    item = next(i for i in client.get(f'/api/v1/cases/{case.id}/analyses').json()['items'] if i['id'] == row['id'])
    assert item['status'] == 'failed' and item['error'] == 'unknown_references' and item['result'] is None


def test_unreachable_server_delays_without_spending_attempts(client, administrator, llm):
    case = one_case()
    enroll(client)
    llm.answers = [ConnectorError('connection_failed')]
    row = client.post(f'/api/v1/cases/{case.id}/analyses').json()
    run_jobs()
    with session() as db:
        analysis = db.get(Analysis, row['id']);job = db.get(Job, analysis.job_id)
        assert analysis.status == 'pending' and analysis.stage == 'delayed' and job.attempts == 0 and job.run_after > now()
        # After an hour waiting the run fails instead of waiting forever.
        analysis.created_at = now() - service.MAX_DELAY - 1;job.run_after = now();db.commit()
    llm.answers = [ConnectorError('connection_failed')]
    run_jobs()
    with session() as db:
        assert db.get(Analysis, row['id']).status == 'failed'


def test_cancel_and_review(client, administrator, llm):
    case = one_case()
    enroll(client)
    row = client.post(f'/api/v1/cases/{case.id}/analyses').json()
    assert client.post(f'/api/v1/analyses/{row["id"]}/cancel').status_code == 200
    run_jobs()
    assert not llm.calls and client.get(f'/api/v1/cases/{case.id}/analyses').json()['items'][0]['status'] == 'cancelled'
    llm.answers = [answer()]
    row = client.post(f'/api/v1/cases/{case.id}/analyses').json();run_jobs()
    assert client.post(f'/api/v1/analyses/{row["id"]}/reviews', json={'verdict': 'incorrect'}).status_code == 422
    assert client.post(f'/api/v1/analyses/{row["id"]}/reviews', json={'verdict': 'incorrect', 'correction': 'Fue el proveedor'}).status_code == 201
    item = next(i for i in client.get(f'/api/v1/cases/{case.id}/analyses').json()['items'] if i['id'] == row['id'])
    assert item['reviews'][0]['correction'] == 'Fue el proveedor'


def test_disabled_analyst_rejects_requests_and_admin_enables_it(client, administrator, llm):
    case = one_case()
    with session() as db:
        db.get(Integration, 'llama_server').events_enabled = False;db.commit()
    enroll(client)
    assert client.post(f'/api/v1/cases/{case.id}/analyses').status_code == 409
    status = client.get('/api/v1/analyst/status').json()
    assert status['configured'] and not status['enabled']
    assert client.put('/api/v1/analyst/settings', json={'version': status['version'], 'enabled': True}).status_code == 200
    assert client.post(f'/api/v1/cases/{case.id}/analyses').status_code == 202


def test_health_reports_a_stopped_analyst_but_not_its_queue_as_job_lag(llm):
    stamp = now()
    with session() as db:
        db.add(AnalystState(id='analyst', heartbeat_at=stamp - 600));db.commit()
        checks = {c['id'] for c in evaluate(db, stamp)['checks']}
    assert 'analyst' in checks and 'jobs' not in checks


def test_context_logs_over_the_cap_keep_the_first_and_the_last():
    from odin.cases.correlator import CONTEXT_CAP
    t = now() - 5 * 86400
    with session() as db:
        flap(db, t, 1, gap=4000)
        for i in range(CONTEXT_CAP + 20):
            event(db, t + 10 + i * 10, title=f'PE-A Bgp: {i}: %BGP-3-NOTIFICATION: received from neighbor {PEER} hold')
        db.commit()
    correlate(now())
    with session() as db:
        case = db.get(Case, cases()[0].id)
        rows = list(db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id, CaseEvidence.role == 'context').order_by(CaseEvidence.occurred_at)))
        assert len(rows) == CONTEXT_CAP and rows[0].occurred_at == t + 10 and rows[-1].occurred_at == t + 10 + (CONTEXT_CAP + 19) * 10
        assert case.detail['context_total']['entity'] == CONTEXT_CAP + 20
        assert 'se guardaron los' in case_pack(db, case).text()


def chat_answer(text='Hubo un caso [C1].', refs=('C1',)):
    return json.dumps({'respuesta': text, 'referencias': list(refs), 'datos_faltantes': []})


def operator_client():
    from fastapi.testclient import TestClient
    from odin.main import app
    from odin.models import User
    from odin.security import hasher
    from conftest import PASSWORD
    with session() as db:
        db.add(User(username='operador', role='operator', password_hash=hasher.hash(PASSWORD)));db.commit()
    other = TestClient(app, base_url='http://localhost:8080', headers={'Origin': 'http://localhost:8080'})
    enroll(other, 'operador')
    return other


def ask(client, conversation, content='¿Qué pasó?', client_id='mensaje-0001', hours=6 * 24, case_id=None):
    stamp = now()
    return client.post(f'/api/v1/conversations/{conversation}/messages', json={'client_id': client_id, 'content': content,
        'start': stamp - hours * 3600, 'end': stamp, 'case_id': case_id})


def test_chat_answers_a_period_with_cited_cases_and_is_idempotent(client, administrator, llm):
    one_case()
    enroll(client)
    conversation = client.post('/api/v1/conversations', json={}).json()['id']
    llm.answers = [chat_answer()]
    first = ask(client, conversation)
    assert first.status_code == 202
    # The same client message id is the same question, not a second one.
    assert ask(client, conversation).json()['message_id'] == first.json()['message_id']
    assert ask(client, conversation, client_id='mensaje-0002').status_code == 409
    run_jobs()
    data = client.get(f'/api/v1/conversations/{conversation}').json()
    assert [m['role'] for m in data['messages']] == ['user', 'assistant'] and data['title'] == '¿Qué pasó?'
    answer_run = data['messages'][0]['run']
    assert answer_run['status'] == 'done' and answer_run['refs']['C1']['kind'] == 'case' and answer_run['result']['referencias'] == ['C1']
    context = llm.calls[0][1]['content']
    assert 'Período consultado' in context and 'C1 · caso de sesión BGP' in context and 'Cobertura de fuentes' in context
    # A follow-up carries the earlier turn without its labels.
    llm.answers = [chat_answer('Nada más.', ())]
    ask(client, conversation, '¿Y después?', 'mensaje-0003');run_jobs()
    turns = llm.calls[1]
    assert turns[-1]['content'] == '¿Y después?' and turns[-2] == {'role': 'assistant', 'content': 'Hubo un caso .'}


def test_a_follow_up_sends_the_same_context_again_unless_the_period_changes(client, administrator, llm):
    one_case()
    enroll(client)
    conversation = client.post('/api/v1/conversations', json={}).json()['id']
    llm.answers = [chat_answer(), chat_answer('Nada más.', ()), chat_answer('Nada.', ())]
    ask(client, conversation);run_jobs()
    # Five minutes later with the same relative period: the context, and its period, are the first ones.
    stamp = now() - 300
    client.post(f'/api/v1/conversations/{conversation}/messages', json={'client_id': 'mensaje-0002', 'content': '¿Y después?',
        'start': stamp - 6 * 24 * 3600, 'end': stamp})
    run_jobs()
    first, second = llm.calls
    assert second[:3] == first[:3] and second[-1]['content'] == '¿Y después?'
    runs = [m['run'] for m in client.get(f'/api/v1/conversations/{conversation}').json()['messages'] if m['role'] == 'user']
    assert runs[1]['request']['start'] == runs[0]['request']['start'] and runs[1]['refs'] == runs[0]['refs']
    # Another period length is another context.
    ask(client, conversation, '¿Y en la última hora?', 'mensaje-0003', hours=1);run_jobs()
    assert 'Período consultado' in llm.calls[2][1]['content'] and llm.calls[2][1] != first[1]


def test_chat_with_a_case_in_context_uses_the_case_evidence(client, administrator, llm):
    case = one_case()
    enroll(client)
    conversation = client.post('/api/v1/conversations', json={}).json()['id']
    llm.answers = [chat_answer('El vecino cerró la sesión [E1].', ('E1',))]
    ask(client, conversation, case_id=case.id);run_jobs()
    assert 'el operador pregunta por este caso' in llm.calls[0][1]['content']
    assert client.get(f'/api/v1/conversations/{conversation}').json()['messages'][1]['content'].startswith('El vecino')


def test_conversations_are_private_even_to_administrators(client, administrator, llm):
    one_case()
    other = operator_client()
    conversation = other.post('/api/v1/conversations', json={}).json()['id']
    llm.answers = [chat_answer()]
    run = ask(other, conversation).json()['run']['id']
    enroll(client)
    assert client.get('/api/v1/conversations').json() == []
    assert client.get(f'/api/v1/conversations/{conversation}').status_code == 404
    assert client.get(f'/api/v1/chat-runs/{run}/events').status_code == 404
    assert client.post(f'/api/v1/analyses/{run}/cancel').status_code == 404
    assert client.delete(f'/api/v1/conversations/{conversation}').status_code == 404
    run_jobs()
    events = other.get(f'/api/v1/chat-runs/{run}/events')
    assert events.headers['content-type'].startswith('text/event-stream') and '"status": "done"' in events.text


def test_failed_answer_keeps_the_question_for_a_retry_and_delete_removes_the_conversation(client, administrator, llm):
    one_case()
    enroll(client)
    conversation = client.post('/api/v1/conversations', json={}).json()['id']
    llm.answers = [chat_answer('Mal [C9].', ('C9',)), chat_answer('Mal [C8].', ('C8',))]
    message = ask(client, conversation).json()['message_id'];run_jobs()
    data = client.get(f'/api/v1/conversations/{conversation}').json()
    assert len(data['messages']) == 1 and data['messages'][0]['run']['error'] == 'unknown_references'
    llm.answers = [chat_answer()]
    assert client.post(f'/api/v1/conversations/{conversation}/messages/{message}/retry').status_code == 202
    run_jobs()
    assert [m['role'] for m in client.get(f'/api/v1/conversations/{conversation}').json()['messages']] == ['user', 'assistant']
    # Deleting while a question is queued: the analyst skips it.
    ask(client, conversation, 'Otra', 'mensaje-0009')
    assert client.delete(f'/api/v1/conversations/{conversation}').status_code == 200
    calls = len(llm.calls);run_jobs()
    assert len(llm.calls) == calls and client.get(f'/api/v1/conversations/{conversation}').status_code == 404
    with session() as db:
        assert db.scalar(select(Case.id))  # cases are untouched


def test_message_period_is_bounded(client, administrator, llm):
    enroll(client)
    conversation = client.post('/api/v1/conversations', json={}).json()['id']
    assert ask(client, conversation, hours=40 * 24).status_code == 422


def test_chat_parse_reads_grouped_citations():
    from odin.analyst.chat import parse as chat_parse
    refs = {'A1': {}, 'L2': {}}
    assert chat_parse(chat_answer('Alertas [A1, L2].', ()), refs)[0]['referencias'] == ['A1', 'L2']
    assert chat_parse(chat_answer('Alertas [A1, L9].', ()), refs)[1] == 'unknown_refs:L9'


def test_lines_about_what_the_question_names_are_cut_last():
    from odin.analyst.chat import focus
    from odin.analyst.packs import Line
    lines = [Line(f'G{i}', 0, 3, f'G{i} · log de configuración de {name}', {}) for i, name in ((1, 'PE-OESTE'), (2, 'PE-PUERTO'))]
    lines.append(Line('A1', 0, 3, 'A1 · Zabbix · PE-PUERTO · «Interface Et1 down»', {}))
    focus(lines, '¿Qué pasó en Puerto?')
    assert [l.priority for l in lines] == [3, 1, 1]
    focus(lines, '¿qué pasó hoy en la red?')
    assert lines[0].priority == 3


def test_earlier_case_and_its_noc_correction_are_memory_for_the_next_one(client, administrator, llm):
    t = now() - 10 * 86400
    with session() as db:
        flap(db, t, 1);flap(db, t + 3 * 86400, 1);db.commit()
    correlate(now())
    first, second = cases()
    enroll(client)
    llm.answers = [answer()]
    row = client.post(f'/api/v1/cases/{first.id}/analyses').json();run_jobs()
    client.post(f'/api/v1/analyses/{row["id"]}/reviews', json={'verdict': 'incorrect', 'correction': 'Fue un corte del proveedor'})
    with session() as db:
        pack = case_pack(db, db.get(Case, second.id))
    memory = next(l for l in pack.lines if l.label == 'M1')
    assert 'misma sesión BGP' in memory.text and 'corrección: «Fue un corte del proveedor»' in memory.text and memory.ref['ids'] == [first.id]
    # The earlier case does not see the later one.
    with session() as db:
        assert not any(l.label.startswith('M') for l in case_pack(db, db.get(Case, first.id)).lines)


def test_knowledge_is_written_by_administrators_and_matched_by_kind_and_keywords(client, administrator, llm):
    case = one_case()
    other = operator_client()
    entry = {'title': 'Límite de prefijos del tránsito', 'body': 'Revisar el filtro de salida hacia AS 64500.', 'case_kind': 'bgp_session',
             'keywords': ['maximum number of prefixes', ' ']}
    assert other.post('/api/v1/knowledge', json=entry).status_code == 403
    enroll(client)
    created = client.post('/api/v1/knowledge', json=entry).json()
    assert created['keywords'] == ['maximum number of prefixes']
    client.post('/api/v1/knowledge', json={**entry, 'title': 'Interfaces', 'case_kind': 'interface', 'keywords': []})
    client.post('/api/v1/knowledge', json={**entry, 'title': 'Otra cosa', 'keywords': ['no aparece']})
    with session() as db:
        lines = [l for l in case_pack(db, db.get(Case, case.id)).lines if l.label.startswith('K')]
    assert [l.ref['ids'] for l in lines] == [[created['id']]] and 'Revisar el filtro de salida' in lines[0].text
    assert len(other.get('/api/v1/knowledge').json()) == 3
    client.post(f'/api/v1/knowledge/{created["id"]}/archive')
    with session() as db:
        assert not any(l.label.startswith('K') for l in case_pack(db, db.get(Case, case.id)).lines)
