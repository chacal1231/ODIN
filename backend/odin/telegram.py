"""NOC group on Telegram: the cases that alert, their diagnosis, and the NOC's verdict back into ODIN.

Outbound, one thread per case: the case when it starts alerting (external services degraded together go in
one message), the automatic analysis as a reply with buttons (correct, partial, incorrect, acknowledge), and
the recovery. Partial or incorrect asks «what really happened?»; the reply of that same person becomes the
correction that later analyses of the same subject read.

Inbound by polling Telegram (no open port). Only ODIN users who linked their account count: each one sends
/vincular <code> (from «Mi acceso») to the bot. An administrator binds the group, or one of its forum topics, with /grupo in it, and can
/pausar or /reanudar the messages.
Carrier incidents (transit, transport, IXP, peering) also go, one thread per case, to an internal providers group bound
with /proveedores: what fails, since when, and every change until it closes. A reply of a linked user in that thread
(the carrier's ticket, its ETA, what they said) becomes a note of the case, so the follow-up stays in ODIN. Linked users ask about the network with /pregunta [1h|6h|24h|7d] <question>, or
about a case by replying to its thread; the chat of ODIN answers (one run at a time) and the answer is posted in the
thread. Each question is also kept as a conversation of its author in ODIN.
The same process runs the WhatsApp NOC group (noc/whatsapp.py), delivers level-1 reports and hands over shifts
(noc/reports.py). Messages are technical reports with fixed fields (noc/messages.py).
What is sent leaves the network: names, addresses and log excerpts of cases.
"""
import datetime as dt
import html
import logging
import re
import secrets
import signal
import threading
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, func, or_
from .config import settings
from .db import session
from .inventory.connectors import Transport, ConnectorError, clean
from .models import (Analysis, Case, CaseNote, ChatMessage, Conversation, Integration, TelegramLink, TelegramMessage, TelegramPrompt,
                     TelegramProviderMessage, TelegramQuestion, TelegramState, User, now, uid)
from .security import audit, decrypt
from .noc import messages, reports

ZONE = ZoneInfo('America/Bogota')
CODE_SECONDS = 600
RENOTIFY = 600  # a case that drops again after recovering is announced at most every 10 minutes
KINDS = {'bgp_session': 'Sesión BGP', 'interface': 'Interfaz', 'latency': 'Latencia hacia', 'access': 'Caída de acceso en'}
PERIODS = {'1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800}
CARRIER_ROLES = ('transit', 'transport', 'ixp', 'peering')
VERDICTS = {'correct': '<b>[VALORACIÓN]</b> Correcto', 'partial': '<b>[VALORACIÓN]</b> Parcialmente correcto', 'incorrect': '<b>[VALORACIÓN]</b> Incorrecto'}
stop = threading.Event()


class Telegram:
    def __init__(self, endpoint, token, networks, heartbeat=lambda: None):
        self.http = Transport(endpoint, networks, heartbeat)
        self.base = endpoint.rstrip('/') + '/bot' + token

    def call(self, method, **payload):
        data = self.http.request('POST', f'{self.base}/{method}', headers={'Content-Type': 'application/json'}, payload=payload)
        if not data.get('ok'):raise ConnectorError('telegram_error')
        return data['result']

    def test(self):
        return {'bot': clean(self.call('getMe').get('username'), 64)}


def client(db):
    integration = db.get(Integration, 'telegram')
    if not integration or not integration.encrypted_secret or not integration.allowed_networks:return None
    return Telegram(integration.endpoint, decrypt(integration.encrypted_secret), integration.allowed_networks)


def state(db):
    row = db.get(TelegramState, 'telegram')
    if not row:row = TelegramState(id='telegram', update_offset=0, paused=False);db.add(row);db.flush()
    return row


def new_code(db, user_id):
    """One-time code that proves a Telegram account belongs to this ODIN user."""
    row = db.get(TelegramLink, user_id) or TelegramLink(user_id=user_id)
    db.add(row)
    while True:
        code = f'{secrets.randbelow(10 ** 6):06d}'
        if not db.scalar(select(TelegramLink.user_id).where(TelegramLink.code == code)):break
    row.code, row.code_expires_at = code, now() + CODE_SECONDS
    return code


def esc(text, limit=1500):
    return html.escape(str(text or '')[:limit], quote=False)


def when(stamp):
    return dt.datetime.fromtimestamp(stamp, ZONE).strftime('%d/%m %H:%M') if stamp else '—'


def span(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60} min' if seconds < 7200 else f'{seconds / 3600:.1f} h'.replace('.', ',')


def link(case):
    return f'<a href="{settings().origin}/#caso={case.id}">Abrir en ODIN</a>'


def case_text(db, case):
    from .cases.api import isolation, related_cases
    from .cases.entities import Resolver
    alone = isolation(db, case, related_cases(db, case, Resolver(db))) if case.kind == 'bgp_session' else None
    return messages.telegram(messages.case_alert(case, alone))


def external_text(cases):
    return messages.telegram(messages.external_group(cases))


def diagnosis_text(case, analysis):
    return messages.telegram(messages.diagnosis(case, analysis))


def buttons(analysis_id, case_id):
    return {'inline_keyboard': [[{'text': 'Correcto', 'callback_data': f'v:correct:{analysis_id}'},
                                 {'text': 'Parcial', 'callback_data': f'v:partial:{analysis_id}'},
                                 {'text': 'Incorrecto', 'callback_data': f'v:incorrect:{analysis_id}'}],
                                [{'text': 'Reconocer', 'callback_data': f'k:{case_id}'}]]}


def send(bot, chat_id, text, reply_to=None, markup=None, thread=None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if thread:payload['message_thread_id'] = thread
    if reply_to:payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
    if markup:payload['reply_markup'] = markup
    return bot.call('sendMessage', **payload)


def outbound(db, bot, st):
    from .cases.summary import alerting, external
    stamp = now()
    known = set(db.scalars(select(TelegramMessage.case_id)))
    new = [c for c in alerting(db) if c.id not in known]
    groups = [[c] for c in new if not external(c)] + ([[c for c in new if external(c)]] if any(external(c) for c in new) else [])
    for cases in groups:
        message = send(bot, st.chat_id, external_text(cases) if external(cases[0]) else case_text(db, cases[0]), thread=st.thread_id)
        for case in cases:db.add(TelegramMessage(case_id=case.id, chat_id=st.chat_id, message_id=message['message_id'], notified_at=stamp))
        db.commit()
    for row, case in db.execute(select(TelegramMessage, Case).join(Case, Case.id == TelegramMessage.case_id)
                                .where((TelegramMessage.analysis_id.is_(None)) | (Case.state != 'closed') | (TelegramMessage.recovered.is_(False)))).all():
        if row.analysis_id is None:
            analysis = db.scalar(select(Analysis).where(Analysis.case_id == case.id, Analysis.kind == 'case', Analysis.status == 'done')
                                 .order_by(Analysis.finished_at.desc()).limit(1))
            if analysis:
                sent = send(bot, row.chat_id, diagnosis_text(case, analysis), row.message_id, buttons(analysis.id, case.id), st.thread_id)
                row.analysis_id, row.diagnosis_message_id = analysis.id, sent['message_id'];db.commit()
        if not row.recovered and (case.last_state == 'up' or case.state == 'closed'):
            send(bot, row.chat_id, messages.telegram(messages.recovered(case)), row.message_id, thread=st.thread_id)
            row.recovered = True;db.commit()
        elif row.recovered and case.state == 'active' and case.last_state == 'down' and stamp - row.notified_at > RENOTIFY:
            send(bot, row.chat_id, messages.telegram(messages.dropped_again(case)), row.message_id, thread=st.thread_id)
            row.recovered, row.notified_at = False, stamp;db.commit()


    for question, run in db.execute(select(TelegramQuestion, Analysis).join(Analysis, Analysis.id == TelegramQuestion.analysis_id)
                                    .where(Analysis.status.not_in(['pending', 'running']))).all():
        send(bot, question.chat_id, answer_text(run), question.message_id, thread=question.thread_id)
        db.delete(question);db.commit()


def carrier(case):
    """The port or neighbor summary of a case about a carrier link, or None."""
    info = case.detail.get('neighbor') or case.detail.get('port') or {}
    return info if info.get('role') in CARRIER_ROLES else None


def provider_text(case, info):
    # Transports are named by their whole description: the provider is not always its second word (roles.py).
    name = info.get('description') if info.get('role') == 'transport' else (info.get('provider') or info.get('description'))
    fields = [('Proveedor', f'{name or "sin identificar"} ({info.get("label") or info.get("role")})'),
              ('Estado', f'activo desde {when(case.opened_at)} · {case.drops} caída(s)')]
    fields.append(('Enlace', info['summary']) if info.get('summary') else ('Puerto', f'«{info.get("description")}»'))
    fields.append(('Seguimiento', 'responder a este mensaje con el ticket del proveedor y sus novedades; queda como nota del caso'))
    return messages.telegram(messages.Message('CRÍTICO' if case.severity == 'critical' else 'ADVERTENCIA', messages.title(case), fields,
                                              link=messages.ref(case)))


def providers(db, bot, st):
    """Carrier incidents in the providers group, each in its own thread until the case closes."""
    from .cases.summary import alerting
    stamp = now()
    known = set(db.scalars(select(TelegramProviderMessage.case_id)))
    for case in alerting(db):
        info = carrier(case)
        if not info or case.id in known:continue
        message = send(bot, st.providers_chat_id, provider_text(case, info), thread=st.providers_thread_id)
        db.add(TelegramProviderMessage(case_id=case.id, chat_id=st.providers_chat_id, message_id=message['message_id'],
                                       recovered=False, closed=False, notified_at=stamp))
        db.commit()
    for row, case in db.execute(select(TelegramProviderMessage, Case).join(Case, Case.id == TelegramProviderMessage.case_id)
                                .where(TelegramProviderMessage.closed.is_(False))).all():
        reply = lambda text: send(bot, row.chat_id, text, row.message_id, thread=st.providers_thread_id)
        if case.state == 'closed':
            end = case.closed_at or case.resolved_at or case.last_state_at
            reply(f'<b>[CERRADO]</b> {esc(case.subject, 200)} · <b>Duración:</b> {span(end - case.opened_at)} · <b>Caídas:</b> {case.drops}')
            row.recovered = row.closed = True;db.commit()
        elif not row.recovered and case.last_state == 'up':
            reply(f'<b>[RESTABLECIDO · EN OBSERVACIÓN]</b> {when(case.resolved_at or case.last_state_at)}. Se cierra si sigue estable.')
            row.recovered = True;db.commit()
        elif row.recovered and case.last_state == 'down' and stamp - row.notified_at > RENOTIFY:
            reply(f'<b>[NUEVA CAÍDA]</b> {when(case.last_state_at)} · <b>Caídas:</b> {case.drops}')
            row.recovered, row.notified_at = False, stamp;db.commit()


def answer_text(run):
    from .analyst.chat import LABEL
    if run.status != 'done' or not run.result:
        return f'No pude responder ({esc(run.error or run.status, 80)}). Intenta de nuevo en unos minutos.'
    missing = run.result.get('datos_faltantes') or []
    text = re.sub(r' +([.,;:)])', r'\1', re.sub(r'[ \t]{2,}', ' ', LABEL.sub('', run.result.get('respuesta') or ''))).strip()
    return ('<b>[RESPUESTA ODIN]</b>\n' + esc(text, 3200)
            + (f'\n<i>Faltan datos: {esc("; ".join(missing), 400)}</i>' if missing else '') + '\nReferencias completas en ODIN → Chat.')


def ask(db, bot, st, user, question, reply_to, period=PERIODS['6h'], case_id=None):
    """Queue a chat run as a conversation of the user in ODIN; the answer is posted in the thread when it ends."""
    from .analyst.service import configured, request
    say = lambda body: send(bot, st.chat_id, body, reply_to, thread=st.thread_id)
    question = question.strip()[:2000]
    if not question:return say('Escribe la pregunta, por ejemplo: <code>/pregunta 24h ¿qué pasó con los tránsitos?</code>')
    if not configured(db):return say('El analista no está activado en ODIN.')
    if db.scalar(select(Analysis.id).where(Analysis.requested_by == user.id, Analysis.kind == 'chat', Analysis.status.in_(['pending', 'running'])).limit(1)):
        return say('Ya tienes una pregunta en curso: espera su respuesta.')
    stamp = now()
    conversation = Conversation(owner_id=user.id, title=('Telegram · ' + question)[:80], context={})
    db.add(conversation);db.flush()
    message = ChatMessage(conversation_id=conversation.id, role='user', content=question, client_id='tg-' + uid())
    db.add(message);db.flush()
    context = {'start': stamp - period, 'end': stamp, 'case_id': case_id, 'pop_id': None}
    run = request(db, 'chat', user.id, conversation_id=conversation.id, request_info={'message_id': message.id, **context})
    message.analysis_id, conversation.context = run.id, context
    db.add(TelegramQuestion(analysis_id=run.id, chat_id=st.chat_id, message_id=reply_to, thread_id=st.thread_id, created_at=stamp))
    ahead = db.scalar(select(func.count()).select_from(Analysis).where(Analysis.status.in_(['pending', 'running']), Analysis.id != run.id))
    return say('<b>[CONSULTA EN CURSO]</b>' + (' Caso referido.' if case_id else '') + (f' En cola: {ahead} por delante.' if ahead else ' Tiempo estimado: 1–3 min.'))


def linked(db, telegram_id):
    return db.scalar(select(User).join(TelegramLink, TelegramLink.user_id == User.id)
                     .where(TelegramLink.telegram_user_id == telegram_id, User.active.is_(True)))


def handle(db, bot, st, item):
    from .analyst.service import review
    if 'callback_query' in item:
        query = item['callback_query'];message = query.get('message') or {}
        chat = (message.get('chat') or {}).get('id');user = linked(db, query['from']['id'])
        answer = lambda text: bot.call('answerCallbackQuery', callback_query_id=query['id'], text=text)
        if not user or chat != st.chat_id:return answer('Vincula tu cuenta en ODIN → Mi acceso')
        kind, _, rest = (query.get('data') or '').partition(':')
        if kind == 'k':
            case = db.get(Case, rest)
            if case and not case.acknowledged_at:
                case.acknowledged_at, case.acknowledged_by = now(), user.id;audit(db, 'case_acknowledged', user.id, case.id)
                send(bot, chat, f'<b>[RECONOCIDO]</b> por {esc(user.username, 80)}', message.get('message_id'), thread=st.thread_id)
            return answer('Reconocido')
        verdict, _, analysis_id = rest.partition(':')
        if verdict == 'correct':
            try:review(db, analysis_id, user.id, 'correct')
            except (LookupError, ValueError):return answer('Ese análisis ya no se puede valorar')
            send(bot, chat, f'{VERDICTS["correct"]} · {esc(user.username, 80)}', message.get('message_id'), thread=st.thread_id)
            return answer('Valoración registrada')
        if verdict in ('partial', 'incorrect'):
            prompt = send(bot, chat, f'<b>[CORRECCIÓN]</b> {esc(user.username, 80)}: responder a este mensaje con la causa encontrada.',
                          message.get('message_id'), {'force_reply': True, 'selective': True}, st.thread_id)
            db.add(TelegramPrompt(message_id=prompt['message_id'], chat_id=chat, analysis_id=analysis_id, user_id=user.id, verdict=verdict, created_at=now()))
            return answer('Indicar la causa encontrada')
        return answer('Acción desconocida')
    message = item.get('message') or {}
    text = (message.get('text') or '').strip();chat = (message.get('chat') or {}).get('id')
    topic = message.get('message_thread_id') if message.get('is_topic_message') else None
    reply = lambda body, to=None: send(bot, chat, body, to, thread=topic)
    sender = (message.get('from') or {}).get('id');command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    if command == '/vincular':
        code = text.split()[1] if len(text.split()) > 1 else ''
        row = db.scalar(select(TelegramLink).where(TelegramLink.code == code, TelegramLink.code_expires_at > now())) if code else None
        if not row:return reply('Código no válido o vencido. Genera uno nuevo en ODIN → Mi acceso.')
        db.execute(update(TelegramLink).where(TelegramLink.telegram_user_id == sender).values(telegram_user_id=None))
        row.telegram_user_id, row.code, row.code_expires_at, row.linked_at = sender, None, None, now()
        user = db.get(User, row.user_id);audit(db, 'telegram_linked', user.id, str(sender))
        return reply(f'Vinculado como {esc(user.username, 80)} en ODIN.')
    if command == '/pregunta':
        user = linked(db, sender)
        if chat != st.chat_id:return reply('Pregunta desde el grupo del NOC configurado.')
        if not user:return reply('Vincula tu cuenta en ODIN → Mi acceso para preguntar.', message.get('message_id'))
        words = text.split(maxsplit=2)[1:]  # optional period, then the question
        period = PERIODS.get(words[0].lower()) if words else None
        question = (words[1] if len(words) > 1 else '') if period else ' '.join(words)
        return ask(db, bot, st, user, question, message.get('message_id'), period or PERIODS['6h'])
    if command in ('/grupo', '/proveedores', '/pausar', '/reanudar'):
        user = linked(db, sender)
        if (message.get('chat') or {}).get('type') not in ('group', 'supergroup'):
            return reply('Hazlo desde el grupo del NOC (o desde su tema), no en privado.')
        if not user or user.role != 'admin':
            # An anonymous group admin writes as the group itself: Telegram does not say who it is.
            anonymous = (message.get('sender_chat') or {}).get('id') == chat
            return reply('Telegram envió este mensaje como del grupo, sin tu usuario: desactiva «Permanecer anónimo» en tus permisos de administrador.'
                         if anonymous else 'Solo un administrador de ODIN vinculado puede hacerlo. Vincúlate en ODIN → Mi acceso.')
        if command == '/grupo':
            st.chat_id, st.thread_id, st.paused = chat, topic, False;audit(db, 'telegram_group_bound', user.id, f'{chat}/{topic or ""}')
            return reply('Este ' + ('tema' if topic else 'grupo') + ' recibirá los casos que avisan en ODIN, con su diagnóstico.')
        if command == '/proveedores':
            st.providers_chat_id, st.providers_thread_id = chat, topic;audit(db, 'telegram_providers_bound', user.id, f'{chat}/{topic or ""}')
            return reply('Este ' + ('tema' if topic else 'grupo') + ' recibirá las fallas con proveedores (tránsito, transporte, IXP, peering). '
                         'Respondan a cada una con el ticket y las novedades del proveedor.')
        if chat != st.chat_id:return reply('Este no es el grupo configurado. Usa /grupo primero.')
        st.paused = command == '/pausar';audit(db, 'telegram_paused' if st.paused else 'telegram_resumed', user.id, str(chat))
        return reply('Mensajes en pausa.' if st.paused else 'Mensajes reanudados.')
    replied = (message.get('reply_to_message') or {}).get('message_id')
    followed = db.scalar(select(TelegramProviderMessage).where(TelegramProviderMessage.chat_id == chat,
        TelegramProviderMessage.message_id == replied)) if replied and text and not command and chat == st.providers_chat_id else None
    if followed:
        user = linked(db, sender)
        if not user:return reply('Vincula tu cuenta en ODIN → Mi acceso para registrar novedades.', message.get('message_id'))
        db.add(CaseNote(case_id=followed.case_id, author_id=user.id, text=('Proveedor (Telegram): ' + text)[:4000]))
        audit(db, 'case_note_added', user.id, followed.case_id)
        return reply(f'<b>[NOTA REGISTRADA]</b> en el caso, por {esc(user.username, 80)}.', message.get('message_id'))
    prompt = db.get(TelegramPrompt, replied) if replied and chat == st.chat_id and text else None
    user = linked(db, sender) if prompt else None
    if prompt and user and user.id == prompt.user_id:
        try:review(db, prompt.analysis_id, user.id, prompt.verdict, text)
        except (LookupError, ValueError):return reply('No se pudo registrar: ese análisis ya no existe.', message.get('message_id'))
        db.delete(prompt)
        return reply(f'{VERDICTS[prompt.verdict]} · {esc(user.username, 80)}. Corrección registrada para los próximos análisis.',
                     message.get('message_id'))
    # A reply to a case thread (the case or its diagnosis) is a question about that case.
    thread = db.scalar(select(TelegramMessage).where(TelegramMessage.chat_id == chat, or_(TelegramMessage.message_id == replied,
        TelegramMessage.diagnosis_message_id == replied)).limit(1)) if replied and chat == st.chat_id and text and not command and not prompt else None
    if thread:
        user = linked(db, sender)
        if not user:return reply('Vincula tu cuenta en ODIN → Mi acceso para preguntar.', message.get('message_id'))
        return ask(db, bot, st, user, text, message.get('message_id'), case_id=thread.case_id)


def inbound(db, bot, st):
    for item in bot.call('getUpdates', offset=st.update_offset, timeout=0, allowed_updates=['message', 'callback_query']):
        st.update_offset = item['update_id'] + 1
        try:
            with db.begin_nested():handle(db, bot, st, item)
        except ConnectorError:
            raise
        except Exception:
            logging.exception('telegram_update_failed')
        db.commit()


def deliver(db, bot, st):
    """Level-1 reports (in the case's thread when there is one) and shift handovers queued for Telegram."""
    from .models import CaseReport, ShiftReport
    for row in reports.pending(db, CaseReport, 'telegram'):
        case = db.get(Case, row.case_id);thread = db.get(TelegramMessage, row.case_id)
        try:
            sent = send(bot, st.chat_id, messages.telegram(messages.report(row, case, reports.author(db, row))),
                        thread.message_id if thread else None, thread=st.thread_id)
            reports.delivered(row, 'telegram', sent['message_id'])
        except ConnectorError as exc:
            reports.delivered(row, 'telegram', error=exc.code)
        db.commit()
    for row in reports.pending(db, ShiftReport, 'telegram'):
        try:reports.delivered(row, 'telegram', send(bot, st.chat_id, messages.telegram(messages.shift(row)), thread=st.thread_id)['message_id'])
        except ConnectorError as exc:reports.delivered(row, 'telegram', error=exc.code)
        db.commit()


def cycle():
    with session() as db:
        st = state(db);st.heartbeat_at = now();db.commit()
        bot = client(db)
        if not bot:return
        try:
            inbound(db, bot, st)
            if st.chat_id and not st.paused:outbound(db, bot, st);deliver(db, bot, st)
            if st.providers_chat_id and not st.paused:providers(db, bot, st)
            st.last_error = None
        except ConnectorError as exc:
            db.rollback();st = state(db);st.last_error = exc.code
            logging.warning('telegram_failed %s', exc.code)
        db.commit()


def main():
    logging.basicConfig(level=logging.INFO)
    # httpx logs every request URL at INFO, and Telegram's URL carries the bot token.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    from .noc import whatsapp
    while not stop.is_set():
        for name, step in (('telegram', cycle), ('whatsapp', whatsapp.cycle), ('shifts', shifts)):
            try:step()
            except Exception:logging.error('%s_cycle_failed', name)
        stop.wait(3)


def shifts():
    with session() as db:reports.tick(db)


if __name__ == '__main__':
    main()
