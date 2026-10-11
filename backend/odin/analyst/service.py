"""Model runs on the job queue, executed by the analyst process one at a time.

The server serves one generation at a time and reads ~150-200 tokens/s, so the evidence is
fitted to INPUT_BUDGET and the answer to OUTPUT_TOKENS (about 1-3 minutes per run). No SQL
transaction stays open while the model works. An unreachable server delays the run (it stays
queued and is retried) until MAX_DELAY; detection, cases and readers never depend on it.
"""
import logging
import time
from sqlalchemy import select
from ..db import session
from ..jobs import enqueue, heartbeat
from ..models import Analysis, AnalystState, Case, Integration, Job, now, uid
from ..security import decrypt
from ..inventory.connectors import ConnectorError
from .llm import PROVIDERS, Llm, ApiLlm
from . import analysis as prompts

KINDS = ('analyze',)
SYSTEM = 'odin'  # requested_by of the analyses ODIN starts by itself
INPUT_BUDGET = 9000
OUTPUT_TOKENS = 2500
MAX_DELAY = 3600
RETRY_DELAY = 120
# Server unavailable: the run waits instead of failing.
UNAVAILABLE = ('connection_failed', 'llm_timeout', 'server_unavailable')


class Cancelled(Exception):
    pass


def ready(integration):
    """Credentials in place; the local server also needs its allowed networks (an API is reached by public address only)."""
    return bool(integration and integration.encrypted_secret and (integration.kind == 'deepseek' or integration.allowed_networks))


def active(db):
    """The one model provider the administrator turned on, or None."""
    for kind in PROVIDERS:
        integration = db.get(Integration, kind)
        if integration and integration.events_enabled and ready(integration):return integration
    return None


def configured(db):
    return active(db) is not None


def request(db, kind, requested_by, case_id=None, conversation_id=None, request_info=None):
    """Queue a run. A case keeps at most one pending or running analysis."""
    if case_id:
        current = db.scalar(select(Analysis).where(Analysis.case_id == case_id, Analysis.status.in_(['pending', 'running'])).with_for_update())
        if current:return current
    row = Analysis(id=uid(), kind=kind, case_id=case_id, conversation_id=conversation_id, requested_by=requested_by, progress={},
                   pack={'request': request_info} if request_info else {})
    db.add(row);db.flush()
    row.job_id = enqueue(db, 'analyze', {'analysis_id': row.id}, 'analyze:' + row.id).id
    return row


def review(db, analysis_id, author_id, verdict, correction=''):
    """Record the NOC's verdict on a case analysis; partial and incorrect need what really happened."""
    from ..models import AnalysisReview
    from ..security import audit
    row = db.get(Analysis, analysis_id)
    if not row or row.kind != 'case' or row.status != 'done':raise LookupError('analysis_not_found')
    if verdict not in ('correct', 'partial', 'incorrect'):raise ValueError('invalid_verdict')
    if verdict != 'correct' and not correction.strip():raise ValueError('correction_required')
    db.add(AnalysisReview(analysis_id=analysis_id, author_id=author_id, verdict=verdict, correction=correction.strip()[:3000]))
    audit(db, 'analysis_reviewed', author_id, analysis_id)


def auto_analyze(db):
    """Cases that alert get their analysis without anyone asking, once: the diagnosis is ready when someone looks."""
    from ..cases.summary import alerting
    if not configured(db):return
    for case in alerting(db):
        if not db.scalar(select(Analysis.id).where(Analysis.case_id == case.id, Analysis.kind == 'case').limit(1)):
            request(db, 'case', SYSTEM, case_id=case.id)


def state(db):
    row = db.get(AnalystState, 'analyst')
    if not row:row = AnalystState(id='analyst');db.add(row)
    return row


def build(db, row):
    """(pack, messages_for(pack_text), schema, parse(content, refs)) for one run."""
    if row.kind == 'case':
        from .packs import case_pack
        case = db.get(Case, row.case_id)
        return case_pack(db, case), prompts.messages, prompts.SCHEMA, prompts.parse
    from .chat import chat_pack, parse
    return (*chat_pack(db, row), parse)


def save(analysis_id, **values):
    with session() as db:
        row = db.get(Analysis, analysis_id)
        if not row:raise Cancelled()
        for key, value in values.items():setattr(row, key, value)
        db.commit()


def execute(job_id, token):
    with session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status != 'running' or job.lease_token != token or job.lease_until <= now():return
        row = db.get(Analysis, job.payload['analysis_id'])
        integration = active(db)
        if not row:
            # Its conversation was deleted by the owner.
            job.status = 'done';job.lease_token = None;job.lease_until = None;db.commit();return
        if row.status in ('done', 'failed', 'cancelled') or row.cancel_requested:
            if row.status in ('pending', 'running'):row.status = 'cancelled';row.finished_at = now()
            job.status = 'done';job.lease_token = None;job.lease_until = None;db.commit();return
        if not configured(db):
            row.status = 'failed';row.error = 'analyst_not_configured';row.finished_at = now()
            job.status = 'done';job.lease_token = None;job.lease_until = None;db.commit();return
        provider, endpoint, secret, networks, model = (integration.kind, integration.endpoint, decrypt(integration.encrypted_secret),
                                                       integration.allowed_networks, integration.model)
        row.status = 'running';row.stage = 'gathering';row.started_at = row.started_at or now();row.error = None
        analysis_id, created_at, kind = row.id, row.created_at, row.kind
        try:
            pack, messages_for, schema, parse = build(db, row)
        except Exception:
            logging.exception('analysis_pack_failed')
            row.status = 'failed';row.error = 'pack_failed';row.finished_at = now()
            job.status = 'done';job.lease_token = None;job.lease_until = None;db.commit();return
        # Building a chat pack may settle its period on the one of the context it reuses.
        request_info = (row.pack or {}).get('request')
        db.commit()
    last_write = [0.0]

    def renew():
        with session() as db:
            if not heartbeat(db, job_id, token):raise ConnectorError('lease_expired')

    def progress(stage, done, total):
        if time.monotonic() - last_write[0] < 2:return
        last_write[0] = time.monotonic()
        with session() as db:
            row = db.get(Analysis, analysis_id)
            if not row or row.cancel_requested:raise Cancelled()
            row.stage = stage;row.progress = {'done': done, 'total': total};db.commit()

    started = time.monotonic();error = None;result = None;usage = {};props = {}
    client = ApiLlm(endpoint, secret, model, renew) if provider == 'deepseek' else Llm(endpoint, secret, networks, renew)
    try:
        props = client.props()
        count = lambda text: client.count(messages_for(text))
        from .packs import Frozen, fit
        from .chat import BUDGET as CHAT_BUDGET
        if isinstance(pack, Frozen):
            # Already fitted when it was first sent; only the conversation grew, within HISTORY_TURNS.
            tokens = count(pack.text())
        else:
            try:
                tokens = fit(pack, count, CHAT_BUDGET if kind == 'chat' else INPUT_BUDGET)
            except ValueError:
                raise ConnectorError('pack_too_large')
        refs = pack.refs()
        stored = {'refs': refs, 'omitted': pack.omitted, 'coverage': pack.coverage, 'tokens': tokens, 'request': request_info}
        if kind == 'chat':
            # What the next question of the conversation may send again verbatim.
            stored.update(text=pack.text(), built_at=getattr(pack, 'built_at', None) or now())
        save(analysis_id, stage='reading', input_version=pack.version, prompt_version=prompts.PROMPT_VERSION, model=props['model'], pack=stored)
        conversation = messages_for(pack.text())
        content, usage, _ = client.chat(conversation, schema, OUTPUT_TOKENS, progress)
        save(analysis_id, stage='validating')
        result, problem = parse(content, refs)
        if problem:
            # One bounded repair; the server reuses the cached prompt, so only the answer is regenerated.
            logging.info('analysis_repair problem=%s', problem.split(':')[0])
            content, usage2, _ = client.chat(conversation + prompts.repair_messages(content, problem), schema, OUTPUT_TOKENS, progress)
            usage = {k: usage.get(k, 0) + usage2.get(k, 0) for k in ('prompt_tokens', 'completion_tokens')}
            result, problem = parse(content, refs)
            if problem:error = 'invalid_output' if problem == 'invalid_json' else 'unknown_references'
    except Cancelled:
        error = 'cancelled'
    except ConnectorError as exc:
        error = exc.code
    except Exception:
        logging.exception('analysis_failed');error = 'analysis_failed'
    with session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.lease_token != token:return
        row = db.get(Analysis, analysis_id);stamp = now();status = state(db)
        if not row:
            job.status = 'done';job.lease_token = None;job.lease_until = None;db.commit();return
        status.heartbeat_at = stamp
        if props:status.model = props['model'];status.context = props['context']
        retry = error in UNAVAILABLE and stamp - created_at < MAX_DELAY and not row.cancel_requested
        if retry:
            # Waiting for the server is not a failed attempt of this run.
            row.status = 'pending';row.stage = 'delayed';row.error = error;row.progress = {}
            job.status = 'pending';job.attempts = max(0, job.attempts - 1);job.run_after = stamp + RETRY_DELAY
            status.last_failure_at = stamp;status.last_error = error
        else:
            row.finished_at = stamp;row.duration_ms = int((time.monotonic() - started) * 1000)
            row.prompt_tokens = usage.get('prompt_tokens');row.completion_tokens = usage.get('completion_tokens')
            if error:
                row.status = 'cancelled' if error == 'cancelled' else 'failed';row.error = error
                if error != 'cancelled':status.last_failure_at = stamp;status.last_error = error
            else:
                row.status = 'done';row.stage = 'done';row.result = result;row.error = None
                status.last_success_at = stamp;status.last_error = None
                if row.kind == 'chat':
                    from .chat import answered
                    answered(db, row)
            job.status = 'done'
        job.lease_token = None;job.lease_until = None
        job.result = {'status': row.status, 'error': error}
        db.commit()
