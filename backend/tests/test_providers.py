"""Model providers (local llama-server or DeepSeek by API) and the Telegram group for carrier incidents."""
import json
import socket
import pytest
from sqlalchemy import select
from odin.db import session
from odin.models import Analysis, Case, CaseNote, Integration, TelegramProviderMessage, User, now, uid
from odin.security import encrypt, hasher
from odin.inventory.connectors import ConnectorError
from odin.analyst import service
from odin.analyst.llm import ApiLlm, DEFAULT_MODEL
from odin import telegram
from odin.noc import messages
from test_access import enroll
from test_analyst import FakeLlm, answer, one_case, run_jobs
from test_telegram import Bot, message


def resolving(monkeypatch, *addresses):
    monkeypatch.setattr('odin.inventory.connectors.socket.getaddrinfo',
                        lambda host, port, **_: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, port)) for a in addresses])


def test_api_client_reaches_public_https_only(monkeypatch):
    client = ApiLlm('https://api.deepseek.com', 'sk-test', None)
    assert client.model == DEFAULT_MODEL
    resolving(monkeypatch, '104.18.26.90')
    target, headers, sni = client.url('/chat/completions')
    assert target == 'https://104.18.26.90:443/chat/completions' and headers['Host'] == 'api.deepseek.com' and sni == 'api.deepseek.com'
    assert headers['Authorization'] == 'Bearer sk-test'
    # A DNS answer pointing inside the network is refused, even mixed with a public one.
    resolving(monkeypatch, '104.18.26.90', '10.0.0.5')
    with pytest.raises(ConnectorError, match='network_not_allowed'):client.url('/models')
    with pytest.raises(ConnectorError, match='endpoint_invalid'):ApiLlm('http://api.deepseek.com', 'sk', None).url('/models')
    with pytest.raises(ConnectorError, match='token_missing'):ApiLlm('https://api.deepseek.com', '', None)


def test_api_client_puts_the_schema_in_the_instructions(monkeypatch):
    client = ApiLlm('https://api.deepseek.com', 'sk', 'deepseek-reasoner')
    messages = [{'role': 'system', 'content': 'Reglas'}, {'role': 'user', 'content': 'Caso'}]
    body = client.body(messages, {'type': 'object', 'required': ['resumen']}, 2500, 0.2)
    assert body['model'] == 'deepseek-reasoner' and body['response_format'] == {'type': 'json_object'} and body['stream']
    assert body['messages'][0]['content'].startswith('Reglas') and '"required": ["resumen"]' in body['messages'][0]['content']
    assert messages[0]['content'] == 'Reglas'  # the caller's messages are not changed
    assert 'chat_template_kwargs' not in body and 'return_progress' not in body
    assert client.count(messages) >= len(json.dumps(messages, ensure_ascii=False)) // 3
    monkeypatch.setattr(ApiLlm, 'call', lambda self, method, path, payload=None, timeout=30: {'data': [{'id': 'deepseek-chat'}]})
    with pytest.raises(ConnectorError, match='model_not_found'):client.test()
    assert ApiLlm('https://api.deepseek.com', 'sk', 'deepseek-chat').test()['model'] == 'deepseek-chat'


class FakeApi(FakeLlm):
    models = []

    def __init__(self, endpoint, secret, model, heartbeat=lambda: None):
        super().__init__(endpoint, secret, [], heartbeat)
        FakeApi.models.append(model)

    def props(self):
        return {'model': FakeApi.models[-1], 'context': 64000, 'slots': 0, 'build': 'api'}


@pytest.fixture
def both(monkeypatch):
    FakeLlm.answers, FakeLlm.calls, FakeApi.models = [], [], []
    monkeypatch.setattr(service, 'Llm', FakeLlm)
    monkeypatch.setattr(service, 'ApiLlm', FakeApi)
    with session() as db:
        db.add(Integration(kind='llama_server', endpoint='http://172.18.0.1:8081', encrypted_secret=encrypt('k'),
                           allowed_networks=['172.18.0.1/32'], events_enabled=True))
        # DeepSeek needs no allowed networks: it is reached by public address only.
        db.add(Integration(kind='deepseek', endpoint='https://api.deepseek.com', encrypted_secret=encrypt('sk'), model='deepseek-chat',
                           allowed_networks=[], events_enabled=False))
        db.commit()


def test_admin_switches_the_one_active_provider(client, administrator, both):
    case = one_case()
    enroll(client)
    status = client.get('/api/v1/analyst/status').json()
    assert status['enabled'] and status['provider'] == 'llama_server'
    assert {p['kind']: p['configured'] for p in status['providers']} == {'llama_server': True, 'deepseek': True}
    deepseek = next(p for p in status['providers'] if p['kind'] == 'deepseek')
    assert client.put('/api/v1/analyst/settings', json={'provider': 'deepseek', 'version': deepseek['version'], 'enabled': True}).status_code == 200
    status = client.get('/api/v1/analyst/status').json()
    assert status['provider'] == 'deepseek' and status['enabled']
    with session() as db:
        assert not db.get(Integration, 'llama_server').events_enabled and db.get(Integration, 'deepseek').events_enabled
    FakeLlm.answers = [answer()]
    assert client.post(f'/api/v1/cases/{case.id}/analyses').status_code == 202
    run_jobs()
    with session() as db:
        row = db.scalar(select(Analysis).where(Analysis.case_id == case.id))
        assert (row.status, row.model) == ('done', 'deepseek-chat')
    assert FakeApi.models == ['deepseek-chat']
    # Stale version: the configuration changed meanwhile.
    assert client.put('/api/v1/analyst/settings', json={'provider': 'llama_server', 'version': 1, 'enabled': True}).status_code == 409


def test_deepseek_integration_requires_https_and_keeps_its_model(client, administrator):
    enroll(client)
    assert client.put('/api/v1/admin/integrations/deepseek', json={'endpoint': 'http://api.deepseek.com', 'secret': 'sk'}).status_code == 422
    assert client.put('/api/v1/admin/integrations/deepseek', json={'endpoint': 'https://api.deepseek.com', 'secret': 'sk'}).status_code == 200
    saved = next(x for x in client.get('/api/v1/admin/integrations').json() if x['kind'] == 'deepseek')
    assert saved['model'] == DEFAULT_MODEL and saved['has_secret']
    body = {'endpoint': 'https://api.deepseek.com', 'model': 'deepseek-reasoner', 'version': saved['version']}
    assert client.put('/api/v1/admin/integrations/deepseek', json=body).status_code == 200
    assert next(x for x in client.get('/api/v1/admin/integrations').json() if x['kind'] == 'deepseek')['model'] == 'deepseek-reasoner'
    assert client.put('/api/v1/admin/integrations/deepseek', json={**body, 'model': 'bad model;'}).status_code == 422
    # The connection test needs no allowed networks for an API.
    assert client.post('/api/v1/admin/integrations/deepseek/test').status_code == 202


def carrier_case(db, stamp, port=None, **values):
    case = Case(kind='bgp_session', key=uid(), entity_key='device:x', entity_label='PE-A', subject='198.51.100.134',
                detail={'neighbor': port or {'role': 'transit', 'label': 'tránsito', 'provider': 'CARRIER', 'description': 'TRANSITO-CARRIER-100G',
                                             'interface': 'Ethernet51/1', 'summary': 'tránsito CARRIER; interfaz Ethernet51/1 de PE-A'}},
                opened_at=stamp, last_activity_at=stamp, last_state='down', last_state_at=stamp, state='active', severity='critical',
                rule_version=4, drops=1, **values)
    db.add(case);db.flush();return case


def run(bot):
    with session() as db:
        st = telegram.state(db)
        telegram.inbound(db, bot, st)
        if st.providers_chat_id and not st.paused:telegram.providers(db, bot, st)
        db.commit()


def test_carrier_incidents_go_to_the_providers_group_and_replies_become_notes():
    stamp = now() - 600
    with session() as db:
        admin = User(username='admin', role='admin', password_hash=hasher.hash('x' * 20));db.add(admin);db.flush()
        code = telegram.new_code(db, admin.id)
        case = carrier_case(db, stamp)
        # Not a carrier link: it stays out of the providers group.
        carrier_case(db, stamp, port={'role': 'customer', 'label': 'cliente', 'provider': 'ACME', 'description': 'CUST: ACME'})
        db.commit();case_id, admin_id = case.id, admin.id
    bot = Bot()
    bot.updates = [message(1, 7, f'/vincular {code}', chat=7, kind='private'), message(2, 7, '/proveedores', chat=-200, topic=55)]
    run(bot)
    sent = bot.sent()
    assert sent[1]['text'].startswith('Este tema recibirá las fallas con proveedores')
    (incident,) = sent[2:]
    assert incident['chat_id'] == -200 and incident['message_thread_id'] == 55
    assert '<b>Proveedor:</b> CARRIER (tránsito)' in incident['text'] and incident['text'].startswith('<b>[CRÍTICO]</b> Sesión BGP · 198.51.100.134 · PE-A')
    assert f'#caso={case_id}' in incident['text']
    thread = bot.next_id

    bot.calls.clear()
    bot.updates = [message(3, 99, 'Ticket 555', chat=-200, reply_to=thread),  # not linked
                   message(4, 7, 'Ticket CARRIER #4411, ETA 2 h, corte de fibra', chat=-200, reply_to=thread, topic=55)]
    run(bot)
    replies = [p['text'] for p in bot.sent()]
    assert replies[0].startswith('Vincula tu cuenta') and replies[1] == '<b>[NOTA REGISTRADA]</b> en el caso, por admin.'
    with session() as db:
        (note,) = db.scalars(select(CaseNote))
        assert (note.case_id, note.author_id, note.text) == (case_id, admin_id, 'Proveedor (Telegram): Ticket CARRIER #4411, ETA 2 h, corte de fibra')
        row = db.get(Case, case_id);row.last_state, row.last_state_at = 'up', stamp + 300;db.commit()

    bot.calls.clear();run(bot)
    (back,) = bot.sent()
    assert back['text'].startswith('<b>[RESTABLECIDO · EN OBSERVACIÓN]</b>') and back['reply_parameters']['message_id'] == thread
    bot.calls.clear();run(bot)
    assert bot.sent() == []
    with session() as db:
        row = db.get(Case, case_id);row.state, row.closed_at, row.drops = 'closed', stamp + 2400, 2;db.commit()
    bot.calls.clear();run(bot)
    (closed,) = bot.sent()
    assert closed['text'] == '<b>[CERRADO]</b> 198.51.100.134 · <b>Duración:</b> 40 min · <b>Caídas:</b> 2'
    bot.calls.clear();run(bot)
    assert bot.sent() == []
    with session() as db:
        assert db.scalar(select(TelegramProviderMessage.closed)) is True


def test_transport_is_named_by_its_whole_description():
    with session() as db:
        case = carrier_case(db, now(), port={'role': 'transport', 'label': 'transporte', 'provider': 'CENTRO',
                                             'description': 'TRANSPORTE-CENTRO-NORTE-INTXA', 'remote_device_name': 'PE-B'})
        text = telegram.provider_text(case, messages.carrier(case))
        db.rollback()
    assert '<b>Proveedor:</b> TRANSPORTE-CENTRO-NORTE-INTXA (transporte)' in text


def test_carrier_incidents_go_to_the_case_thread_of_the_whatsapp_group():
    from odin.models import WhatsAppMessage, WhatsAppProviderMessage, WhatsAppState
    from odin.noc import whatsapp

    class Service:
        def __init__(self):self.sent = []
        def send(self, group, text, quoted=None):
            self.sent.append((group, text, quoted));return f'msg{len(self.sent)}'
    stamp = now() - 600
    with session() as db:
        db.add(WhatsAppState(id='whatsapp', group_id='123@g.us', group_name='NOC', paused=False))
        case = carrier_case(db, stamp)
        carrier_case(db, stamp, port={'role': 'customer', 'label': 'cliente', 'provider': 'ACME', 'description': 'CUST: ACME'})
        db.commit();case_id = case.id
    service = Service()
    whatsapp.cycle(service)
    # Each case gets its alert; only the carrier one gets the provider's details, quoting its own alert.
    alerts = [m for m in service.sent if m[2] is None]
    (details,) = [m for m in service.sent if '*Proveedor:*' in m[1]]
    assert len(alerts) == 2 and all(a[1].startswith('*[CRÍTICO]* Sesión BGP · 198.51.100.134 · PE-A') for a in alerts)
    with session() as db:
        assert details[0] == '123@g.us' and details[2] == db.get(WhatsAppMessage, case_id).message_id
    assert '*Proveedor:* CARRIER (tránsito)' in details[1] and '*Seguimiento:* registrar el ticket del proveedor' in details[1]
    count = len(service.sent)
    with session() as db:
        row = db.get(Case, case_id);row.last_state, row.last_state_at = 'up', stamp + 300;db.commit()
    whatsapp.cycle(service);whatsapp.cycle(service)
    # The recovery is the case thread's own: no second one for the provider.
    recovered = [t for _, t, _ in service.sent[count:] if t.startswith('*[RESTABLECIDO')]
    assert len(recovered) == 1
    with session() as db:
        row = db.get(Case, case_id);row.state, row.closed_at, row.drops = 'closed', stamp + 2400, 2;db.commit()
    count = len(service.sent)
    whatsapp.cycle(service);whatsapp.cycle(service)
    assert [(t, q) for _, t, q in service.sent[count:]] == [('*[CERRADO]* Sesión BGP · 198.51.100.134 · PE-A\n*Duración:* 40 min\n*Caídas:* 2', details[2])]
    with session() as db:
        assert db.scalar(select(WhatsAppProviderMessage.closed)) is True
