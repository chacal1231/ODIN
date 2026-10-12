"""Analyst API: status and activation, case analyses, NOC reviews and private conversations."""
import json
import time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from ..db import get_db, session
from ..models import Analysis, AnalysisReview, AnalystState, Case, ChatMessage, Conversation, Integration, Knowledge, Pop, User, now
from ..security import audit, rate_limit
from .service import request, configured, ready, active
from .llm import PROVIDERS
from .packs import case_version


class AnalystSettings(BaseModel):
    version: int = Field(ge=1)
    enabled: bool
    provider: str = Field(default='llama_server', pattern='^(llama_server|deepseek)$')


class ReviewBody(BaseModel):
    verdict: str = Field(pattern='^(correct|partial|incorrect)$')
    correction: str = Field(default='', max_length=3000)


MAX_PERIOD = 31 * 86400
OLDEST = 90 * 86400
STREAM_SECONDS = 900


class ConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class MessageBody(BaseModel):
    client_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    content: str = Field(min_length=1, max_length=2000)
    start: int
    end: int
    case_id: str | None = Field(default=None, max_length=36)
    pop_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode='after')
    def period(self):
        stamp = now()
        if self.end <= self.start or self.end - self.start > MAX_PERIOD or self.end > stamp + 300 or self.start < stamp - OLDEST:
            raise ValueError('Período no válido')
        return self


class KnowledgeBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    case_kind: str | None = Field(default=None, pattern='^(bgp_session|interface|latency|access)$')
    keywords: list[str] = Field(default_factory=list, max_length=20)
    source_case_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode='after')
    def clean(self):
        self.keywords = sorted({k.strip()[:80] for k in self.keywords if k.strip()})
        return self


def knowledge_view(row, names):
    return {'id': row.id, 'title': row.title, 'body': row.body, 'case_kind': row.case_kind, 'keywords': row.keywords,
            'source_case_id': row.source_case_id, 'created_by': names.get(row.created_by), 'updated_at': row.updated_at, 'archived': row.archived}


def own_conversation(db, conversation_id, user, lock=False):
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.owner_id == user.id)
    row = db.scalar(query.with_for_update() if lock else query)
    # Somebody else's conversation does not exist for this user, administrators included.
    if not row:raise HTTPException(404, 'Conversación no encontrada')
    return row


def own_run(db, analysis_id, user):
    row = db.get(Analysis, analysis_id)
    if not row or row.kind != 'chat' or row.requested_by != user.id:raise HTTPException(404, 'Consulta no encontrada')
    return row


def conversation_view(db, row, full=False):
    result = {'id': row.id, 'title': row.title, 'context': row.context, 'created_at': row.created_at, 'updated_at': row.updated_at}
    if full:
        messages = list(db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == row.id).order_by(ChatMessage.created_at, ChatMessage.role.desc())))
        runs = {a.id: a for a in db.scalars(select(Analysis).where(Analysis.conversation_id == row.id))}
        result['messages'] = [{'id': m.id, 'role': m.role, 'content': m.content, 'created_at': m.created_at, 'client_id': m.client_id,
                               'run': analysis_view(db, runs[m.analysis_id]) if m.analysis_id in runs else None} for m in messages]
    return result


def queue_position(db, row):
    if row.status != 'pending':return None
    return db.scalar(select(func.count()).select_from(Analysis).where(Analysis.status.in_(['pending', 'running']), Analysis.created_at <= row.created_at, Analysis.id != row.id)) + 1


def analysis_view(db, row, names=None):
    pack = row.pack or {}
    reviews = db.execute(select(AnalysisReview, User.username).join(User, User.id == AnalysisReview.author_id, isouter=True)
                         .where(AnalysisReview.analysis_id == row.id).order_by(AnalysisReview.created_at)).all()
    return {'id': row.id, 'kind': row.kind, 'status': row.status, 'stage': row.stage, 'progress': row.progress, 'error': row.error,
            'created_at': row.created_at, 'started_at': row.started_at, 'finished_at': row.finished_at, 'model': row.model,
            'prompt_tokens': row.prompt_tokens, 'completion_tokens': row.completion_tokens, 'duration_ms': row.duration_ms,
            'input_version': row.input_version, 'result': row.result, 'refs': pack.get('refs', {}), 'omitted': pack.get('omitted', []),
            'coverage': pack.get('coverage', []), 'pack_tokens': pack.get('tokens'), 'request': pack.get('request'), 'queue_position': queue_position(db, row),
            'requested_by': (names or {}).get(row.requested_by),
            'reviews': [{'verdict': r.verdict, 'correction': r.correction, 'created_at': r.created_at, 'author': name} for r, name in reviews]}


def router(current, admin):
    api = APIRouter(prefix='/api/v1')

    @api.get('/analyst/status')
    def status(user=Depends(current), db: Session = Depends(get_db)):
        rows = {kind: db.get(Integration, kind) for kind in PROVIDERS};row = db.get(AnalystState, 'analyst')
        current = active(db)
        # The provider shown: the active one, else the first one ready to be turned on.
        integration = current or next((x for x in rows.values() if ready(x)), None) or rows['llama_server']
        queued = db.scalar(select(func.count()).select_from(Analysis).where(Analysis.status == 'pending'))
        running = db.scalar(select(func.count()).select_from(Analysis).where(Analysis.status == 'running'))
        return {'configured': any(ready(x) for x in rows.values()), 'enabled': current is not None,
                'provider': integration.kind if integration else 'llama_server', 'version': integration.version if integration else None,
                'providers': [{'kind': kind, 'configured': ready(x), 'version': x.version if x else None, 'model': x.model if x else None}
                              for kind, x in rows.items()],
                'queued': queued, 'running': running, 'observed_at': now(),
                **{k: getattr(row, k) if row else None for k in ('model', 'context', 'last_success_at', 'last_failure_at', 'last_error', 'heartbeat_at')}}

    @api.put('/analyst/settings')
    def settings(body: AnalystSettings, user=Depends(admin), db: Session = Depends(get_db)):
        rows = {x.kind: x for x in db.scalars(select(Integration).where(Integration.kind.in_(PROVIDERS)).order_by(Integration.kind).with_for_update())}
        integration = rows.get(body.provider)
        if body.enabled and (not integration or not integration.encrypted_secret):raise HTTPException(422, 'Configura URL y token del modelo')
        if not integration:raise HTTPException(404, 'El modelo no está configurado')
        if integration.version != body.version:raise HTTPException(409, 'Actualiza la configuración')
        if body.enabled and not ready(integration):raise HTTPException(422, 'Faltan las redes permitidas')
        # One provider at a time: turning one on turns the other off.
        for other in rows.values():
            if other.kind != body.provider and other.events_enabled:other.events_enabled = False;other.version += 1
        integration.events_enabled = body.enabled;integration.version += 1
        audit(db, 'analyst_enabled' if body.enabled else 'analyst_disabled', user.id, body.provider);db.commit()
        return {'ok': True}

    @api.get('/cases/{case_id}/analyses')
    def case_analyses(case_id: str, user=Depends(current), db: Session = Depends(get_db)):
        case = db.get(Case, case_id)
        if not case:raise HTTPException(404, 'Caso no encontrado')
        rows = list(db.scalars(select(Analysis).where(Analysis.case_id == case_id).order_by(Analysis.created_at.desc(), Analysis.started_at.desc().nulls_first()).limit(10)))
        names = dict(db.execute(select(User.id, User.username).where(User.id.in_({r.requested_by for r in rows} or {''}))).all())
        return {'items': [analysis_view(db, r, names) for r in rows], 'version': case_version(db, case), 'available': configured(db)}

    @api.post('/cases/{case_id}/analyses', status_code=202)
    def analyze(case_id: str, user=Depends(current), db: Session = Depends(get_db)):
        if not db.get(Case, case_id):raise HTTPException(404, 'Caso no encontrado')
        if not configured(db):raise HTTPException(409, 'El analista no está activado')
        rate_limit(db, 'analyze:' + user.id, 20)
        row = request(db, 'case', user.id, case_id=case_id)
        audit(db, 'case_analysis_requested', user.id, case_id);db.commit()
        return analysis_view(db, row)

    @api.post('/analyses/{analysis_id}/cancel')
    def cancel(analysis_id: str, user=Depends(current), db: Session = Depends(get_db)):
        row = db.scalar(select(Analysis).where(Analysis.id == analysis_id).with_for_update())
        if not row or (row.kind == 'chat' and row.requested_by != user.id):raise HTTPException(404, 'Análisis no encontrado')
        if row.requested_by != user.id and user.role != 'admin':raise HTTPException(403, 'Solo quien lo pidió o un administrador')
        if row.status in ('pending', 'running'):
            row.cancel_requested = True
            if row.status == 'pending':row.status = 'cancelled';row.finished_at = now()
            audit(db, 'analysis_cancelled', user.id, analysis_id)
        db.commit();return {'ok': True}

    @api.post('/analyses/{analysis_id}/reviews', status_code=201)
    def review(analysis_id: str, body: ReviewBody, user=Depends(current), db: Session = Depends(get_db)):
        from .service import review as add_review
        try:add_review(db, analysis_id, user.id, body.verdict, body.correction)
        except LookupError:raise HTTPException(404, 'Análisis no encontrado')
        except ValueError:raise HTTPException(422, 'Explica qué es incorrecto')
        db.commit();return {'ok': True}

    @api.get('/knowledge')
    def knowledge(archived: bool = False, user=Depends(current), db: Session = Depends(get_db)):
        query = select(Knowledge).order_by(Knowledge.updated_at.desc()).limit(200)
        if not archived:query = query.where(Knowledge.archived.is_(False))
        rows = list(db.scalars(query))
        names = dict(db.execute(select(User.id, User.username).where(User.id.in_({r.created_by for r in rows} or {''}))).all())
        return [knowledge_view(r, names) for r in rows]

    @api.post('/knowledge', status_code=201)
    def create_knowledge(body: KnowledgeBody, user=Depends(admin), db: Session = Depends(get_db)):
        if body.source_case_id and not db.get(Case, body.source_case_id):raise HTTPException(422, 'Caso no encontrado')
        row = Knowledge(title=body.title.strip(), body=body.body.strip(), case_kind=body.case_kind, keywords=body.keywords,
                        source_case_id=body.source_case_id, created_by=user.id)
        db.add(row);db.flush();audit(db, 'knowledge_created', user.id, row.id);db.commit()
        return knowledge_view(row, {user.id: user.username})

    @api.put('/knowledge/{knowledge_id}')
    def edit_knowledge(knowledge_id: str, body: KnowledgeBody, user=Depends(admin), db: Session = Depends(get_db)):
        row = db.scalar(select(Knowledge).where(Knowledge.id == knowledge_id).with_for_update())
        if not row:raise HTTPException(404, 'Conocimiento no encontrado')
        row.title, row.body, row.case_kind, row.keywords = body.title.strip(), body.body.strip(), body.case_kind, body.keywords
        row.updated_at = now();audit(db, 'knowledge_updated', user.id, row.id);db.commit()
        return {'ok': True}

    @api.post('/knowledge/{knowledge_id}/archive')
    def archive_knowledge(knowledge_id: str, user=Depends(admin), db: Session = Depends(get_db)):
        row = db.scalar(select(Knowledge).where(Knowledge.id == knowledge_id).with_for_update())
        if not row:raise HTTPException(404, 'Conocimiento no encontrado')
        row.archived = True;row.updated_at = now();audit(db, 'knowledge_archived', user.id, row.id);db.commit()
        return {'ok': True}

    @api.get('/conversations')
    def conversations(user=Depends(current), db: Session = Depends(get_db)):
        rows = db.scalars(select(Conversation).where(Conversation.owner_id == user.id).order_by(Conversation.updated_at.desc()).limit(100))
        return [conversation_view(db, r) for r in rows]

    @api.post('/conversations', status_code=201)
    def create_conversation(body: ConversationBody, user=Depends(current), db: Session = Depends(get_db)):
        rate_limit(db, 'conversation:' + user.id, 30)
        row = Conversation(owner_id=user.id, title=(body.title or 'Nueva conversación').strip()[:200] or 'Nueva conversación', context={})
        db.add(row);db.commit()
        return conversation_view(db, row, True)

    @api.get('/conversations/{conversation_id}')
    def conversation(conversation_id: str, user=Depends(current), db: Session = Depends(get_db)):
        return conversation_view(db, own_conversation(db, conversation_id, user), True)

    @api.delete('/conversations/{conversation_id}')
    def delete_conversation(conversation_id: str, user=Depends(current), db: Session = Depends(get_db)):
        row = own_conversation(db, conversation_id, user, lock=True)
        # Cases, incidents and evidence are not touched; only this user's questions and answers go.
        db.execute(ChatMessage.__table__.delete().where(ChatMessage.conversation_id == row.id))
        db.execute(Analysis.__table__.delete().where(Analysis.conversation_id == row.id))
        db.delete(row);audit(db, 'conversation_deleted', user.id, conversation_id);db.commit()
        return {'ok': True}

    @api.post('/conversations/{conversation_id}/messages', status_code=202)
    def ask(conversation_id: str, body: MessageBody, user=Depends(current), db: Session = Depends(get_db)):
        row = own_conversation(db, conversation_id, user, lock=True)
        existing = db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == row.id, ChatMessage.client_id == body.client_id))
        if existing:
            # The same message sent twice (retry after a network error) is not a second question.
            run = db.get(Analysis, existing.analysis_id) if existing.analysis_id else None
            return {'message_id': existing.id, 'run': analysis_view(db, run) if run else None}
        if not configured(db):raise HTTPException(409, 'El analista no está activado')
        if db.scalar(select(Analysis.id).where(Analysis.conversation_id == row.id, Analysis.status.in_(['pending', 'running']))):
            raise HTTPException(409, 'Espera la respuesta anterior o cancélala')
        if body.case_id and not db.get(Case, body.case_id):raise HTTPException(422, 'Caso no encontrado')
        if body.pop_id and not db.get(Pop, body.pop_id):raise HTTPException(422, 'POP no encontrado')
        rate_limit(db, 'chat:' + user.id, 30)
        message = ChatMessage(conversation_id=row.id, role='user', content=body.content.strip(), client_id=body.client_id)
        db.add(message);db.flush()
        context = {'start': body.start, 'end': body.end, 'case_id': body.case_id, 'pop_id': None if body.case_id else body.pop_id}
        run = request(db, 'chat', user.id, conversation_id=row.id, request_info={'message_id': message.id, **context})
        message.analysis_id = run.id
        row.context = context;row.updated_at = now()
        if row.title == 'Nueva conversación':row.title = body.content.strip()[:80]
        db.commit()
        return {'message_id': message.id, 'run': analysis_view(db, run)}

    @api.post('/conversations/{conversation_id}/messages/{message_id}/retry', status_code=202)
    def retry(conversation_id: str, message_id: str, user=Depends(current), db: Session = Depends(get_db)):
        row = own_conversation(db, conversation_id, user, lock=True)
        message = db.get(ChatMessage, message_id)
        if not message or message.conversation_id != row.id or message.role != 'user':raise HTTPException(404, 'Mensaje no encontrado')
        previous = db.get(Analysis, message.analysis_id) if message.analysis_id else None
        if not previous or previous.status not in ('failed', 'cancelled'):raise HTTPException(409, 'Esta pregunta no necesita reintento')
        if not configured(db):raise HTTPException(409, 'El analista no está activado')
        if db.scalar(select(Analysis.id).where(Analysis.conversation_id == row.id, Analysis.status.in_(['pending', 'running']))):
            raise HTTPException(409, 'Espera la respuesta anterior o cancélala')
        rate_limit(db, 'chat:' + user.id, 30)
        run = request(db, 'chat', user.id, conversation_id=row.id, request_info=(previous.pack or {}).get('request'))
        message.analysis_id = run.id;db.commit()
        return {'message_id': message.id, 'run': analysis_view(db, run)}

    @api.get('/chat-runs/{analysis_id}')
    def chat_run(analysis_id: str, user=Depends(current), db: Session = Depends(get_db)):
        return analysis_view(db, own_run(db, analysis_id, user))

    @api.get('/chat-runs/{analysis_id}/events')
    def chat_events(analysis_id: str, user=Depends(current), db: Session = Depends(get_db)):
        own_run(db, analysis_id, user)
        owner = user.id

        def stream():
            last, started, beat = None, time.monotonic(), time.monotonic()
            while time.monotonic() - started < STREAM_SECONDS:
                with session() as s:
                    row = s.get(Analysis, analysis_id)
                    if not row or row.requested_by != owner:
                        yield 'event: gone\ndata: {}\n\n';return
                    state = {'status': row.status, 'stage': row.stage, 'progress': row.progress, 'error': row.error,
                             'queue_position': queue_position(s, row)}
                if state != last:
                    yield 'data: ' + json.dumps(state) + '\n\n';last = state;beat = time.monotonic()
                if state['status'] not in ('pending', 'running'):return
                if time.monotonic() - beat > 15:
                    yield ': sigue\n\n';beat = time.monotonic()
                time.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    return api
