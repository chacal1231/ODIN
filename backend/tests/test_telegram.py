from sqlalchemy import select
from odin.db import session
from odin.models import Analysis, AnalysisReview, Case, TelegramMessage, User, now, uid
from odin.security import hasher
from odin import telegram


class Bot:
    """Records what ODIN sends; getUpdates hands out the queued updates once."""
    def __init__(self):
        self.calls, self.updates, self.next_id = [], [], 100

    def call(self, method, **payload):
        self.calls.append((method, payload))
        if method == 'getUpdates':
            updates, self.updates = self.updates, []
            return updates
        if method == 'sendMessage':
            self.next_id += 1
            return {'message_id': self.next_id}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == 'sendMessage']


def message(update_id, sender, text, chat=-100, kind='supergroup', reply_to=None, topic=None):
    body = {'message_id': update_id, 'from': {'id': sender}, 'chat': {'id': chat, 'type': kind}, 'text': text}
    if topic:body.update(message_thread_id=topic, is_topic_message=True)
    if reply_to:body['reply_to_message'] = {'message_id': reply_to}
    return {'update_id': update_id, 'message': body}


def button(update_id, sender, data, message_id):
    return {'update_id': update_id, 'callback_query': {'id': str(update_id), 'from': {'id': sender}, 'data': data,
                                                       'message': {'message_id': message_id, 'chat': {'id': -100}}}}


def run(bot):
    with session() as db:
        st = telegram.state(db)
        telegram.inbound(db, bot, st)
        if st.chat_id and not st.paused:telegram.outbound(db, bot, st)
        db.commit()


def test_case_diagnosis_and_verdict_travel_through_the_noc_group():
    stamp = now() - 600
    with session() as db:
        admin = User(username='admin', role='admin', password_hash=hasher.hash('x' * 20))
        db.add(admin);db.flush()
        code = telegram.new_code(db, admin.id)
        case = Case(kind='interface', key=uid(), entity_key='device:x', entity_label='pe-centro01', subject='Ethernet51/1',
                    detail={'port': {'role': 'transit', 'label': 'tránsito', 'description': 'TRANSITO-CARRIER-100G'}}, opened_at=stamp,
                    last_activity_at=stamp, last_state='down', last_state_at=stamp, state='active', severity='critical', rule_version=4)
        db.add(case);db.commit()
        admin_id, case_id = admin.id, case.id
    bot = Bot()
    bot.updates = [message(1, 7, '/grupo', topic=4242),  # not linked yet
                   message(2, 7, f'/vincular {code}', chat=7, kind='private'), message(3, 7, '/grupo@odin_bot', topic=4242)]
    run(bot)
    first = bot.sent()
    assert first[0]['text'].startswith('Solo un administrador de ODIN vinculado') and first[0]['message_thread_id'] == 4242
    assert first[1]['text'].startswith('Vinculado como admin') and 'message_thread_id' not in first[1]
    assert first[2]['text'].startswith('Este tema recibirá') and first[3]['message_thread_id'] == 4242
    first = first[1:]
    case_message = first[2]
    assert 'Interfaz Ethernet51/1' in case_message['text'] and 'TRANSITO-CARRIER-100G' in case_message['text'] and f'#caso={case_id}' in case_message['text']

    with session() as db:
        db.add(Analysis(id=uid(), kind='case', case_id=case_id, requested_by='odin', status='done', finished_at=now(), progress={}, pack={},
                        result={'resumen': 'Cayó el tránsito <Carrier>', 'hipotesis': [{'hipotesis': 'Corte de fibra', 'confianza': 'media'}],
                                'recomendaciones': [{'accion': 'Llamar a Carrier', 'comprobacion': 'Ticket abierto'}]}))
        db.commit()
    bot.calls.clear();run(bot)
    (diagnosis,) = bot.sent()
    assert '&lt;Carrier&gt;' in diagnosis['text'] and diagnosis['reply_parameters']['message_id'] == 104 and diagnosis['message_thread_id'] == 4242
    analysis_id = diagnosis['reply_markup']['inline_keyboard'][0][1]['callback_data'].split(':')[2]

    bot.calls.clear()
    bot.updates = [button(4, 99, f'v:correct:{analysis_id}', 105),           # not linked: ignored
                   button(5, 7, f'v:partial:{analysis_id}', 105)]
    run(bot)
    answers = [p['text'] for m, p in bot.calls if m == 'answerCallbackQuery']
    assert answers == ['Vincula tu cuenta en ODIN → Mi acceso', 'Cuéntanos qué pasó']
    prompt_id = bot.next_id
    bot.calls.clear()
    bot.updates = [message(6, 7, 'Fue un corte de fibra de Carrier en Bogotá, ticket 123', reply_to=prompt_id)]
    run(bot)
    with session() as db:
        (review,) = db.scalars(select(AnalysisReview))
        assert (review.verdict, review.author_id, review.correction) == ('partial', admin_id, 'Fue un corte de fibra de Carrier en Bogotá, ticket 123')
        case = db.get(Case, case_id);case.last_state, case.last_state_at = 'up', stamp + 900;db.commit()
    assert bot.sent()[0]['text'].startswith('🟡 Parcialmente correcto')
    bot.calls.clear();run(bot)
    assert bot.sent()[0]['text'].startswith('✅ <b>Restablecido</b> · Ethernet51/1') and 'duró 15 min' in bot.sent()[0]['text']
    bot.calls.clear();run(bot)
    assert bot.sent() == []  # nothing new
    with session() as db:
        assert db.scalar(select(TelegramMessage.recovered)) is True


def test_connection_test_is_queued_for_any_source_and_sync_only_for_inventory():
    import pytest
    from odin.models import Integration
    from odin.inventory.service import queue_run
    with session() as db:
        db.add_all([Integration(kind='telegram', endpoint='https://api.telegram.org', allowed_networks=['149.154.160.0/20']),
                    Integration(kind='graylog', endpoint='http://graylog', allowed_networks=['10.0.0.0/8'])]);db.flush()
        assert queue_run(db, 'telegram', 'test').mode == 'test' and queue_run(db, 'graylog', 'test').source == 'graylog'
        with pytest.raises(ValueError):queue_run(db, 'telegram', 'sync')
        db.rollback()


def test_questions_from_the_group_are_answered_in_the_thread_and_kept_as_conversations():
    from odin.models import Integration, Conversation, TelegramState
    from odin.security import encrypt
    stamp = now()
    with session() as db:
        db.add(Integration(kind='llama_server', endpoint='http://llm', encrypted_secret=encrypt('k'), allowed_networks=['10.0.0.0/8'], events_enabled=True))
        user = User(username='operador', role='operator', password_hash=hasher.hash('x' * 20));db.add(user);db.flush()
        code = telegram.new_code(db, user.id)
        case = Case(kind='interface', key=uid(), entity_key='k', entity_label='pe-x', subject='Ethernet1', detail={}, opened_at=stamp - 600,
                    last_activity_at=stamp, last_state='down', last_state_at=stamp, state='closed', severity='warning', rule_version=4)
        db.add(case);db.flush()
        db.add(TelegramState(id='telegram', chat_id=-100, thread_id=4242, update_offset=0, paused=False))
        db.add(TelegramMessage(case_id=case.id, chat_id=-100, message_id=50, diagnosis_message_id=51, analysis_id='a', recovered=True, notified_at=stamp))
        db.commit();user_id, case_id = user.id, case.id
    bot = Bot()
    bot.updates = [message(1, 9, '/pregunta ¿algo?', topic=4242),                       # not linked
                   message(2, 8, f'/vincular {code}', chat=8, kind='private'),
                   message(3, 8, '/pregunta 24h ¿qué pasó con los tránsitos?', topic=4242),
                   message(4, 8, '¿hubo cambios de configuración antes?', reply_to=51, topic=4242)]  # one question at a time
    run(bot)
    replies = [p['text'] for p in bot.sent()]
    assert replies[0].startswith('Vincula tu cuenta') and replies[2].startswith('⏳ Pensando…') and replies[3].startswith('Ya tienes una pregunta en curso')
    with session() as db:
        (question,) = db.scalars(select(Analysis).where(Analysis.kind == 'chat'))
        request = question.pack['request']
        assert request['end'] - request['start'] == 86400 and request['case_id'] is None and question.requested_by == user_id
        assert db.scalar(select(Conversation.title)) == 'Telegram · ¿qué pasó con los tránsitos?'
        question.status, question.result = 'done', {'respuesta': 'Cayó la sesión con Carrier [C1, A2] a las 14:05 [L3].', 'referencias': ['C1'], 'datos_faltantes': []}
        db.commit()
    bot.calls.clear();bot.updates = [message(5, 8, '¿hubo cambios de configuración antes?', reply_to=51, topic=4242)]
    run(bot)
    thinking, answer = bot.sent()
    assert thinking['text'] == '⏳ Pensando sobre el caso… Suele tardar 1–3 minutos.' and thinking['message_thread_id'] == 4242
    assert answer['text'].startswith('💬 Cayó la sesión con Carrier a las 14:05.') and answer['reply_parameters']['message_id'] == 3
    with session() as db:
        case_question = db.scalar(select(Analysis).where(Analysis.kind == 'chat', Analysis.status == 'pending'))
        assert case_question.pack['request']['case_id'] == case_id
