"""ODIN self-supervision: failures of its own pipeline must be visible to the NOC, not found days later."""
from sqlalchemy import select, func
from .models import ProbeTarget, ProbeBatch, ProbeOrigin, EventCollector, Integration, Job, now
from .probes.service import origin_view, slot_at
from .events.collector import INTERVALS

RANK = {'healthy': 0, 'warning': 1, 'critical': 2}
# Rounds still running are not missing: the window ends one minute before evaluation.
RATE_WINDOW = 540
RATE_SETTLE = 60
SOURCE_NAMES = {'zabbix': 'Zabbix', 'librenms': 'LibreNMS', 'graylog': 'Graylog'}
TOPIC_NAMES = {'events': 'eventos', 'alerts': 'alertas actuales', 'alert_history': 'historial de alertas', 'logs': 'logs'}


def check(id, state, title, detail, page):
    return {'id': id, 'state': state, 'title': title, 'detail': detail, 'page': page}


def expected_slots(target, start, end):
    """Scheduled slots of a target within [start, end)."""
    interval = target.effective['interval_seconds']
    last = slot_at(target, end - 1)
    return 0 if last < start else (last - start) // interval + 1


def probe_checks(db, stamp):
    origin = origin_view(db, stamp)
    if origin['state'] != 'healthy':
        age = stamp - origin['heartbeat'] if origin['heartbeat'] else None
        detail = ('Sin señal desde hace ' + str(age) + ' s.' if age is not None else 'El sondeador no se ha iniciado.')
        if origin['error_code'] and origin['error_code'] != 'probe_stale':
            detail += ' Error: ' + origin['error_code'] + '.'
        return [check('probe', 'critical', 'Sondeador sin señal', detail, 'Red propia')]
    end = stamp - RATE_SETTLE
    start = end - RATE_WINDOW
    # Targets changed inside the window restart their schedule; judging them would raise false alarms.
    targets = list(db.scalars(select(ProbeTarget).where(ProbeTarget.enabled.is_(True), ProbeTarget.archived.is_(False),
        ProbeTarget.changed_at <= start, ProbeTarget.created_at <= start)))
    expected = sum(expected_slots(t, start, end) for t in targets)
    if not expected:
        return []
    produced = db.scalar(select(func.count()).select_from(ProbeBatch).where(
        ProbeBatch.target_id.in_([t.id for t in targets]), ProbeBatch.mode == 'scheduled',
        ProbeBatch.slot >= start, ProbeBatch.slot < end, ProbeBatch.status.in_(['done', 'failed'])))
    ratio = produced / expected
    state = 'critical' if ratio < 0.5 else 'warning' if ratio < 0.9 else 'healthy'
    if state == 'healthy':
        return []
    return [check('probe_rate', state, 'Sondeador atrasado',
        f'{produced} de {expected} rondas programadas en los últimos {RATE_WINDOW // 60} minutos '
        f'({round(ratio * 100)} %). Las rondas omitidas no se recuperan.', 'Red propia')]


def trace_checks(db, stamp):
    """Traces are diagnostics, not measurements: a stopped tracer is a warning."""
    if not db.scalar(select(ProbeTarget.id).where(ProbeTarget.enabled.is_(True), ProbeTarget.archived.is_(False)).limit(1)):
        return []
    origin = db.get(ProbeOrigin, 'tracer')
    if origin and origin.heartbeat >= stamp - 90 and not origin.error_code:
        return []
    detail = ('Sin señal desde hace ' + duration(stamp - origin.heartbeat) + '.' if origin else 'El trazador no se ha iniciado.')
    if origin and origin.error_code:
        detail += ' Error: ' + origin.error_code + '.'
    return [check('tracer', 'warning', 'Trazas detenidas', detail + ' Las mediciones siguen; no hay trazas nuevas.', 'Red propia')]


def smartolt_checks(db, stamp):
    """SmartOLT is read every 5 minutes; a stop or the account running out of API budget must show."""
    from .models import SmartoltState
    integration = db.get(Integration, 'smartolt')
    if not integration or not integration.events_enabled:
        return []
    row = db.get(SmartoltState, 'smartolt');result = []
    info = ((row.tasks or {}).get('outages') or {}) if row else {}
    since = info.get('last_success_at') or integration.updated_at
    if stamp - since > 1200:
        detail = ('Sin lectura de caídas de PON desde hace ' + duration(stamp - since) + '.' if info.get('last_success_at')
                  else 'Sin lecturas exitosas desde su activación hace ' + duration(stamp - since) + '.')
        result.append({**check('smartolt', 'critical' if stamp - since > 3600 else 'warning', 'Lectura de SmartOLT detenida', detail, 'OLTs'),
                       'error_code': info.get('last_error'), 'failures': info.get('failures', 0)})
    if row and row.blocked_until and row.blocked_until > stamp:
        result.append(check('smartolt_rate', 'warning', 'SmartOLT limitó las llamadas',
                            f'La API pidió esperar; las lecturas se reanudan en {duration(row.blocked_until - stamp)}.', 'OLTs'))
    elif row and row.rate_limit and row.rate_remaining is not None and row.rate_observed_at and stamp - row.rate_observed_at < 3600 \
            and row.rate_remaining < row.rate_limit * 0.1:
        result.append(check('smartolt_rate', 'warning', 'Presupuesto de SmartOLT casi agotado',
                            f'Quedan {row.rate_remaining} de {row.rate_limit} llamadas en esta hora. Otro sistema puede estar usando la misma cuenta.', 'OLTs'))
    return result


def event_checks(db, stamp):
    result = []
    rows = db.execute(select(EventCollector, Integration).join(Integration, Integration.kind == EventCollector.source)
        .where(Integration.events_enabled.is_(True)).order_by(EventCollector.id)).all()
    for row, integration in rows:
        interval = INTERVALS.get(row.source, 60)
        warn_after = max(300, interval * 10)
        # Never read successfully: measure from the moment the source was configured.
        since = row.last_success_at or integration.updated_at
        age = stamp - since
        state = 'critical' if age > 1800 else 'warning' if age > warn_after else 'healthy'
        if state == 'healthy':
            continue
        name = SOURCE_NAMES.get(row.source, row.source) + ' · ' + TOPIC_NAMES.get(row.topic, row.topic)
        detail = ('Sin lectura exitosa desde hace ' + duration(age) + '.' if row.last_success_at
                  else 'Sin lecturas exitosas desde su activación hace ' + duration(age) + '.')
        result.append({**check('events:' + row.id, state, 'Lectura detenida: ' + name, detail, 'Eventos'),
                       'error_code': row.last_error, 'failures': row.failure_count})
    return result


def correlation_checks(db, stamp):
    from .models import CorrelationState
    state = db.get(CorrelationState, 'cases')
    if not state or not state.last_run_at:
        return []
    age = stamp - (state.last_success_at or 0)
    if age <= 600:
        return []
    return [{**check('correlation', 'critical' if age > 1800 else 'warning', 'Correlador detenido',
        f'Sin una pasada correcta desde hace {duration(age)}. Los casos no se actualizan.', 'Casos'),
        'error_code': state.last_error, 'failures': 0}]


def analyst_checks(db, stamp):
    from .models import Analysis, AnalystState
    from .analyst.service import active
    if not active(db):
        return []
    state = db.get(AnalystState, 'analyst')
    beat = state.heartbeat_at if state else None
    if not beat or stamp - beat > 120:
        return [check('analyst', 'warning', 'Analista detenido',
            'El proceso del analista no da señales' + (f' desde hace {duration(stamp - beat)}' if beat else '') + '. '
            'Los análisis y el chat quedan en cola; casos y lecturas siguen funcionando.', 'Casos')]
    oldest = db.scalar(select(func.min(Analysis.created_at)).where(Analysis.status == 'pending'))
    if oldest is not None and stamp - oldest > 900:
        detail = f'Hay análisis en cola desde hace {duration(stamp - oldest)}.'
        if state.last_error and (state.last_failure_at or 0) > (state.last_success_at or 0):
            detail += f' Último error del modelo: {state.last_error}.'
        return [{**check('analyst_queue', 'warning', 'El modelo no responde' if state.last_error else 'Análisis demorados', detail, 'Casos'),
                 'error_code': state.last_error, 'failures': 0}]
    return []


def job_checks(db, stamp):
    # Model runs queue behind each other by design; the analyst check covers them.
    scope = Job.kind.not_in(('analyze',))
    oldest = db.scalar(select(func.min(Job.run_after)).where(Job.status == 'pending', Job.run_after <= stamp, scope))
    lag = stamp - oldest if oldest is not None else 0
    state = 'critical' if lag > 900 else 'warning' if lag > 300 else 'healthy'
    if state == 'healthy':
        return []
    count = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'pending', Job.run_after <= stamp - 300, scope))
    return [check('jobs', state, 'Cola de trabajos atrasada',
        (f'{count} trabajos esperan' if count != 1 else '1 trabajo espera') + f' más de 5 minutos; el más antiguo, {duration(lag)}. '
        'Inventario y lectura de eventos se demoran.', 'Eventos')]


def duration(seconds):
    if seconds < 120:
        return f'{seconds} s'
    if seconds < 7200:
        return f'{seconds // 60} min'
    if seconds < 172800:
        return f'{seconds // 3600} h'
    return f'{seconds // 86400} días'


def evaluate(db, stamp=None):
    stamp = stamp or now()
    checks = probe_checks(db, stamp) + trace_checks(db, stamp) + smartolt_checks(db, stamp) + event_checks(db, stamp) + correlation_checks(db, stamp) + analyst_checks(db, stamp) + job_checks(db, stamp)
    state = max((c['state'] for c in checks), key=RANK.get, default='healthy')
    return {'state': state, 'checks': checks, 'evaluated_at': stamp}
