"""Level-1 reports to the NOC group, the deadline to send them, and the shift handover.

A report (inicial, actualizacion, restablecido, escalado to level 2) is written by a person from a draft of its guide,
kept on the case (also as a note the analyst reads) and delivered by every channel the NOC group has: Telegram and
WhatsApp. Each channel's process sends the pending ones and records the result per channel.
A case that needs a report (critical, or an access outage, which level 1 always reports) is late when it stays down
`report_minutes` without the first report. Its guide's escalation time marks when it should go to level 2.
Shifts are local times (ODIN_TIMEZONE), 06:00-20:00 by default. When a shift ends ODIN hands over a summary of it
(cierre); when one starts after hours nobody watched, a summary of those hours (apertura). Both go to the NOC group.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_, func
from ..config import settings
from ..models import (Case, CaseNote, CaseReport, NocSettings, ShiftReport, TelegramState, WhatsAppState, Integration, User, now, uid)
from . import guides
from .messages import title

DEFAULT_SHIFTS = [{'start': '06:00', 'end': '20:00'}]
RECENT = 3600       # a shift boundary older than this (ODIN was down) is not summarized afterwards
CLOCK = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def load(db):
    row = db.get(NocSettings, 'noc')
    return {'report_minutes': row.report_minutes if row else 10, 'shifts': row.shifts if row else DEFAULT_SHIFTS}


def validate_shifts(shifts):
    if not 1 <= len(shifts) <= 6:raise ValueError('Entre 1 y 6 turnos')
    for s in shifts:
        if not CLOCK.match(s.get('start', '')) or not CLOCK.match(s.get('end', '')) or s['start'] == s['end']:
            raise ValueError('Cada turno necesita inicio y fin HH:MM distintos')
    return [{'start': s['start'], 'end': s['end']} for s in shifts]


def intervals(shifts, stamp):
    """Shift intervals (start, end) in epoch seconds around `stamp`; a shift ending at or before its start crosses midnight."""
    zone = ZoneInfo(settings().timezone)
    today = dt.datetime.fromtimestamp(stamp, zone).date()
    result = []
    for offset in (-2, -1, 0, 1):
        day = today + dt.timedelta(days=offset)
        for s in shifts:
            start = dt.datetime.combine(day, dt.time.fromisoformat(s['start']), zone)
            end = dt.datetime.combine(day, dt.time.fromisoformat(s['end']), zone)
            if end <= start:end += dt.timedelta(days=1)
            result.append((int(start.timestamp()), int(end.timestamp())))
    return sorted(set(result))


def boundaries(shifts, stamp):
    """The latest shift that ended (cierre) and the latest unwatched hours that ended at a shift start (apertura)."""
    spans = intervals(shifts, stamp)
    closed = max((s for s in spans if s[1] <= stamp), key=lambda s: s[1], default=None)
    gaps = [(a[1], b[0]) for a, b in zip(spans, spans[1:]) if a[1] < b[0] and b[0] <= stamp]
    return closed, max(gaps, key=lambda g: g[1], default=None)


def reportable(case):
    """Level 1 reports critical cases and every access outage."""
    return case.severity == 'critical' or case.kind == 'access'


def status(db, cases, stamp=None):
    """Per case: first report, escalation, and whether either is due."""
    stamp = stamp or now();config = load(db)
    rows = {}
    for r in db.scalars(select(CaseReport).where(CaseReport.case_id.in_([c.id for c in cases] or [''])).order_by(CaseReport.created_at)):
        rows.setdefault(r.case_id, []).append(r)
    result = {}
    for case in cases:
        mine = rows.get(case.id, [])
        first = next((r for r in mine if r.kind == 'inicial'), None)
        escalated = next((r for r in mine if r.kind == 'escalado'), None)
        guide = guides.current(db, guides.situation(case))
        down = case.last_state == 'down' and case.state != 'closed'
        minutes = guide['escalate_minutes']
        result[case.id] = {
            'reported_at': first.created_at if first else None, 'escalated_at': escalated.created_at if escalated else None,
            'reports': len(mine),
            'overdue': bool(reportable(case) and down and not first and stamp - case.opened_at > config['report_minutes'] * 60),
            'report_due_at': case.opened_at + config['report_minutes'] * 60,
            'escalate_due': bool(down and not escalated and minutes is not None and stamp - case.opened_at >= minutes * 60),
            'escalate': guide['escalate']}
    return result


def channels(db):
    """Channels the NOC group has now; a report is queued on each of them."""
    found = []
    tg, integration = db.get(TelegramState, 'telegram'), db.get(Integration, 'telegram')
    if tg and tg.chat_id and not tg.paused and integration and integration.encrypted_secret:found.append('telegram')
    wa = db.get(WhatsAppState, 'whatsapp')
    if wa and wa.group_id and not wa.paused:found.append('whatsapp')
    return found


def create(db, case, kind, text, user):
    if kind not in guides.KINDS:raise ValueError('Tipo de informe no válido')
    text = text.strip()
    if not text or len(text) > 2000:raise ValueError('El informe necesita texto (hasta 2.000 caracteres)')
    if kind == 'escalado' and text == guides.draft(db, case, 'escalado', now()).strip():
        raise ValueError('Indica el motivo del escalamiento')
    stamp = now()
    row = CaseReport(id=uid(), case_id=case.id, kind=kind, text=text, author_id=user.id, created_at=stamp,
                     deliveries={c: {'status': 'pending'} for c in channels(db)})
    db.add(row)
    # As a note too: the analyst and the case timeline read what level 1 reported.
    db.add(CaseNote(case_id=case.id, author_id=user.id, created_at=stamp, text=f'Informe N1 ({kind}): {text}'))
    return row


def pending(db, model, channel):
    return [r for r in db.scalars(select(model).where(model.created_at > now() - 86400).order_by(model.created_at))
            if (r.deliveries or {}).get(channel, {}).get('status') == 'pending']


def delivered(row, channel, message_id=None, error=None):
    row.deliveries = {**row.deliveries, channel: {'status': 'failed' if error else 'sent', 'id': message_id, 'error': error, 'at': now()}}


def author(db, row):
    user = db.get(User, row.author_id)
    return user.username if user else 'usuario'


def summarize(db, kind, start, end):
    """What a shift (or the unwatched hours) leaves: cases still open, cases resolved, and the reports made."""
    cases = list(db.scalars(select(Case).where(Case.opened_at <= end, or_(Case.closed_at.is_(None), Case.closed_at >= start),
                                               or_(Case.severity == 'critical', Case.kind == 'access')).order_by(Case.opened_at)))
    info = status(db, cases, end)
    still = [c for c in cases if c.state != 'closed' and c.last_state == 'down']
    resolved = [c for c in cases if c not in still and start <= (c.resolved_at or c.closed_at or 0) <= end]
    reports = db.scalar(select(func.count()).select_from(CaseReport).where(CaseReport.created_at.between(start, end)))
    late = [c for c in cases if c.opened_at >= start and reportable(c) and
            (not info[c.id]['reported_at'] or info[c.id]['reported_at'] > info[c.id]['report_due_at'])]
    lines = [f'{"Casos abiertos al cierre" if kind == "cierre" else "Casos abiertos ahora"}: {len(still)}']
    for c in still:
        i = info[c.id]
        state = ', '.join(filter(None, [f'informado {guides.local(i["reported_at"])}' if i['reported_at'] else 'sin informar',
                                        f'escalado a N2 {guides.local(i["escalated_at"])}' if i['escalated_at'] else '']))
        impact = (c.detail.get('impact') or {}).get('text')
        lines.append(f'- [{"CRÍTICO" if c.severity == "critical" else "ADVERTENCIA"}] {title(c)} — desde {guides.local(c.opened_at)}'
                     + (f' — {impact}' if impact else '') + f' — {state}')
    lines.append(f'Resueltos {"en el turno" if kind == "cierre" else "fuera de turno"}: {len(resolved)}')
    for c in resolved:
        back = c.resolved_at or c.closed_at
        lines.append(f'- {title(c)} — {guides.span(back - c.opened_at)}')
    lines.append(f'Informes N1: {reports}' + (f'; {len(late)} caso(s) informados fuera de plazo o sin informar' if late else ''))
    data = {'open': [c.id for c in still], 'resolved': [c.id for c in resolved], 'reports': reports, 'late': [c.id for c in late]}
    return '\n'.join(lines), data


def tick(db, stamp=None):
    """Creates the handover of a shift that just ended and the summary of the unwatched hours when a shift starts."""
    stamp = stamp or now();shifts = load(db)['shifts']
    closed, gap = boundaries(shifts, stamp)
    made = []
    for kind, span_ in (('cierre', closed), ('apertura', gap)):
        if not span_ or span_[1] < stamp - RECENT:continue
        if db.scalar(select(ShiftReport.id).where(ShiftReport.kind == kind, ShiftReport.ends_at == span_[1])):continue
        text, data = summarize(db, kind, *span_)
        row = ShiftReport(id=uid(), kind=kind, starts_at=span_[0], ends_at=span_[1], text=text, data=data, created_at=stamp,
                          deliveries={c: {'status': 'pending'} for c in channels(db)})
        db.add(row);made.append(row)
    db.commit()
    return made
