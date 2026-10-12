"""NOC communication API: report deadline and shifts, guides, level-1 reports per case, WhatsApp pairing, shift handovers."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Case, CaseReport, NocGuide, NocSettings, ShiftReport, TelegramState, User, now
from ..security import audit
from . import guides, reports
from .whatsapp import Service, WhatsAppError, state as whatsapp_state


class Shift(BaseModel):
    start: str
    end: str


class SettingsBody(BaseModel):
    report_minutes: int = Field(ge=1, le=240)
    shifts: list[Shift] = Field(min_length=1, max_length=6)


class GuideBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    verify: list[str] = Field(max_length=8)
    template: str = Field(min_length=1, max_length=1000)
    escalate: str = Field(min_length=1, max_length=500)
    escalate_minutes: int | None = Field(default=None, ge=0, le=1440)


class ReportBody(BaseModel):
    kind: str = Field(pattern='^(inicial|actualizacion|restablecido|escalado)$')
    text: str = Field(min_length=1, max_length=2000)


class GroupBody(BaseModel):
    group_id: str = Field(min_length=5, max_length=120)


class PauseBody(BaseModel):
    paused: bool


def report_view(db, row):
    user = db.get(User, row.author_id)
    return {'id': row.id, 'kind': row.kind, 'text': row.text, 'author': user.username if user else None, 'created_at': row.created_at,
            'deliveries': row.deliveries}


def router(current, admin):
    api = APIRouter(prefix='/api/v1')

    @api.get('/noc/settings')
    def get_settings(user=Depends(current), db: Session = Depends(get_db)):
        telegram, whatsapp = db.get(TelegramState, 'telegram'), whatsapp_state(db);db.commit()
        return {**reports.load(db), 'channels': reports.channels(db),
                'telegram': {'group': bool(telegram and telegram.chat_id), 'paused': bool(telegram and telegram.paused)},
                'whatsapp': {'group': whatsapp.group_name, 'paused': whatsapp.paused, 'last_error': whatsapp.last_error}}

    @api.put('/noc/settings')
    def put_settings(body: SettingsBody, user=Depends(admin), db: Session = Depends(get_db)):
        try:shifts = reports.validate_shifts([s.model_dump() for s in body.shifts])
        except ValueError as exc:raise HTTPException(422, str(exc))
        row = db.get(NocSettings, 'noc') or NocSettings(id='noc')
        row.report_minutes, row.shifts, row.updated_at, row.updated_by = body.report_minutes, shifts, now(), user.id
        db.add(row);audit(db, 'noc_settings_saved', user.id);db.commit()
        return reports.load(db)

    @api.get('/noc/guides')
    def list_guides(user=Depends(current), db: Session = Depends(get_db)):
        return guides.every(db)

    @api.put('/noc/guides/{situation}')
    def put_guide(situation: str, body: GuideBody, user=Depends(admin), db: Session = Depends(get_db)):
        if situation not in guides.DEFAULTS:raise HTTPException(404, 'Situación desconocida')
        try:body.template.format_map(guides.Blank())
        except (ValueError, IndexError):raise HTTPException(422, 'Plantilla no válida: revisa las llaves { }')
        row = db.get(NocGuide, situation) or NocGuide(situation=situation)
        row.title, row.verify, row.template = body.title, [v.strip() for v in body.verify if v.strip()], body.template
        row.escalate, row.escalate_minutes, row.updated_at, row.updated_by = body.escalate, body.escalate_minutes, now(), user.id
        db.add(row);audit(db, 'noc_guide_saved', user.id, situation);db.commit()
        return guides.current(db, situation)

    @api.delete('/noc/guides/{situation}')
    def reset_guide(situation: str, user=Depends(admin), db: Session = Depends(get_db)):
        row = db.get(NocGuide, situation)
        if row:db.delete(row);audit(db, 'noc_guide_reset', user.id, situation);db.commit()
        return guides.current(db, situation)

    @api.get('/cases/{case_id}/communication')
    def communication(case_id: str, user=Depends(current), db: Session = Depends(get_db)):
        case = db.get(Case, case_id)
        if not case:raise HTTPException(404, 'Caso no encontrado')
        rows = db.scalars(select(CaseReport).where(CaseReport.case_id == case_id).order_by(CaseReport.created_at))
        stamp = now()
        return {'situation': guides.situation(case), 'guide': guides.current(db, guides.situation(case)),
                'status': reports.status(db, [case], stamp)[case.id], 'channels': reports.channels(db),
                'drafts': {kind: guides.draft(db, case, kind, stamp) for kind in guides.KINDS},
                'reports': [report_view(db, r) for r in rows]}

    @api.post('/cases/{case_id}/reports', status_code=201)
    def create_report(case_id: str, body: ReportBody, user=Depends(current), db: Session = Depends(get_db)):
        case = db.get(Case, case_id)
        if not case:raise HTTPException(404, 'Caso no encontrado')
        try:row = reports.create(db, case, body.kind, body.text, user)
        except ValueError as exc:raise HTTPException(422, str(exc))
        audit(db, 'case_report_' + body.kind, user.id, case.id);db.commit()
        return report_view(db, row)

    @api.get('/noc/whatsapp')
    def whatsapp(user=Depends(admin), db: Session = Depends(get_db)):
        st = whatsapp_state(db);db.commit()
        result = {'group_id': st.group_id, 'group_name': st.group_name, 'paused': st.paused, 'last_error': st.last_error}
        try:
            service = Service();result['service'] = service.status()
            result['groups'] = service.groups() if result['service'].get('status') == 'ready' else []
        except WhatsAppError as exc:
            result['service'] = {'status': 'unreachable', 'error': exc.code};result['groups'] = []
        return result

    @api.put('/noc/whatsapp')
    def choose_group(body: GroupBody, user=Depends(admin), db: Session = Depends(get_db)):
        try:groups = {g['id']: g['name'] for g in Service().groups()}
        except WhatsAppError as exc:raise HTTPException(409, f'WhatsApp no está listo ({exc.code})')
        if body.group_id not in groups:raise HTTPException(422, 'Ese grupo no está en la cuenta vinculada')
        st = whatsapp_state(db);st.group_id, st.group_name, st.last_error = body.group_id, groups[body.group_id][:200], None
        audit(db, 'whatsapp_group_bound', user.id, body.group_id);db.commit()
        return {'group_id': st.group_id, 'group_name': st.group_name}

    @api.post('/noc/whatsapp/pause')
    def pause(body: PauseBody, user=Depends(admin), db: Session = Depends(get_db)):
        st = whatsapp_state(db);st.paused = body.paused
        audit(db, 'whatsapp_paused' if body.paused else 'whatsapp_resumed', user.id);db.commit()
        return {'paused': st.paused}

    @api.post('/noc/whatsapp/logout')
    def logout(user=Depends(admin), db: Session = Depends(get_db)):
        try:Service().logout()
        except WhatsAppError as exc:raise HTTPException(409, f'No se pudo cerrar la sesión ({exc.code})')
        st = whatsapp_state(db);st.group_id = st.group_name = None
        audit(db, 'whatsapp_logged_out', user.id);db.commit()
        return {'ok': True}

    @api.get('/noc/shifts')
    def shifts(limit: int = Query(10, ge=1, le=50), user=Depends(current), db: Session = Depends(get_db)):
        stamp = now();config = reports.load(db)
        spans = [s for s in reports.intervals(config['shifts'], stamp) if s[0] <= stamp < s[1]]
        preview = None
        if spans:
            text, _ = reports.summarize(db, 'cierre', spans[0][0], stamp)
            preview = {'starts_at': spans[0][0], 'ends_at': spans[0][1], 'text': text}
        rows = db.scalars(select(ShiftReport).order_by(ShiftReport.ends_at.desc()).limit(limit))
        return {'current': preview, 'reports': [{'id': r.id, 'kind': r.kind, 'starts_at': r.starts_at, 'ends_at': r.ends_at, 'text': r.text,
                                                  'deliveries': r.deliveries} for r in rows]}

    return api
