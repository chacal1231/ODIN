import re
import ipaddress
import secrets
import uuid
from urllib.parse import urlsplit
import pyotp
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, delete, text
from sqlalchemy.orm import Session
from .config import settings
from .db import get_db
from .models import User, AuthSession, Challenge, Audit, Integration, SourceIdentity, now
from .security import (hasher, DUMMY_HASH, verify_password, token, digest, encrypt, decrypt,
                       consume_mfa, new_recovery_codes, rate_limit, audit, revoke)

app = FastAPI(title='ODIN', version='0.1.0', docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = 'odin_session'
CHALLENGE = 'odin_challenge'
KINDS = ('zabbix', 'librenms', 'graylog', 'llama_server', 'deepseek', 'telegram')


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({'detail': 'Revisa los campos y sus límites'}, status_code=422)


@app.middleware('http')
async def protection(request: Request, call_next):
    request_id = str(uuid.uuid4())
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') != settings().origin:
        response = JSONResponse({'detail': 'Origen no permitido'}, status_code=403)
    else:
        try:
            response = await call_next(request)
        except Exception:
            # No bodies, tokens or exception strings in client responses/logs.
            import logging
            logging.error('request_failed request_id=%s', request_id)
            response = JSONResponse({'detail': 'Error interno', 'request_id': request_id}, status_code=500)
    response.headers['X-Request-ID'] = request_id
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def set_cookie(response, name, value, seconds):
    response.set_cookie(name, value, max_age=seconds, httponly=True,
                        secure=settings().secure_cookies, samesite='strict', path='/api')


def current(request: Request, db: Session = Depends(get_db)):
    raw = request.cookies.get(COOKIE, '')
    auth = db.get(AuthSession, digest(raw))
    stamp = now()
    if not auth or auth.expires_at <= stamp or auth.last_seen + settings().idle_seconds <= stamp:
        raise HTTPException(401, 'Sesión expirada o no válida')
    user = db.get(User, auth.user_id)
    if not user or not user.active or not user.mfa_confirmed or user.must_change_password:
        raise HTTPException(401, 'Sesión no válida')
    if request.method not in ('GET', 'HEAD'):
        if not secrets.compare_digest(auth.csrf_hash, digest(request.headers.get('x-csrf-token', ''))):
            raise HTTPException(403, 'Protección CSRF')
    auth.last_seen = stamp
    db.commit()
    request.state.auth = auth
    return user


def admin(user: User = Depends(current)):
    if user.role != 'admin':
        raise HTTPException(403, 'Requiere administrador')
    return user


def user_view(user):
    return {'id': user.id, 'username': user.username, 'role': user.role, 'active': user.active,
            'mfa_confirmed': user.mfa_confirmed, 'must_change_password': user.must_change_password}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class VerifyBody(BaseModel):
    code: str = Field(min_length=6, max_length=64)
    new_password: str | None = Field(default=None, min_length=14, max_length=256)


class CreateUserBody(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r'^[a-zA-Z0-9._-]+$')
    role: str = 'operator'

    @field_validator('role')
    @classmethod
    def role_valid(cls, value):
        if value not in ('admin', 'operator'):
            raise ValueError('Rol no válido')
        return value


class RecoveryBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=64)


class IntegrationBody(BaseModel):
    endpoint: str = Field(max_length=2048)
    secret: str | None = Field(default=None, max_length=8192)
    allowed_networks: list[str] | None = Field(default=None, max_length=20)
    auto_sync: bool | None = None
    model: str | None = Field(default=None, min_length=1, max_length=120, pattern=r'^[A-Za-z0-9._:/-]+$')
    version: int | None = None

    @field_validator('allowed_networks')
    @classmethod
    def valid_networks(cls, value):
        if value is None:
            return None
        return [str(ipaddress.ip_network(x.strip(), strict=False)) for x in value]


    @field_validator('endpoint')
    @classmethod
    def endpoint_valid(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('URL HTTP(S) sin credenciales, query ni fragmento')
        return value.rstrip('/')


@app.get('/api/v1/health/live')
def live():
    return {'status': 'alive'}


@app.get('/api/v1/branding')
def branding():
    # Public: the login screen shows whose NOC this is.
    return {'isp_name': settings().isp_name}


@app.get('/api/v1/health/ready')
def ready(db: Session = Depends(get_db)):
    try:
        db.execute(text('SELECT version_num FROM alembic_version'))
        return {'status': 'ready'}
    except Exception:
        raise HTTPException(503, 'Persistencia no disponible')


@app.post('/api/v1/auth/login')
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    username = body.username.lower().strip()
    rate_limit(db, 'ip:' + request.client.host, 30)
    rate_limit(db, 'login:' + username, 8)
    user = db.scalar(select(User).where(User.username == username).with_for_update())
    encoded = user.password_hash if user else DUMMY_HASH
    valid = verify_password(encoded, body.password)
    if not valid or not user or not user.active:
        audit(db, 'login_failed')
        db.commit()
        raise HTTPException(401, 'Credenciales no válidas')
    if not user.mfa_secret:
        user.mfa_secret = encrypt(pyotp.random_base32())
    db.execute(delete(Challenge).where(Challenge.user_id == user.id))
    challenge, csrf = token(), token()
    db.add(Challenge(token_hash=digest(challenge), user_id=user.id, csrf_hash=digest(csrf), expires_at=now() + 300))
    audit(db, 'login_password_verified', user.id)
    db.commit()
    set_cookie(response, CHALLENGE, challenge, 300)
    result = {'csrf_token': csrf, 'enrollment_required': not user.mfa_confirmed,
              'password_change_required': user.must_change_password}
    if not user.mfa_confirmed:
        secret = decrypt(user.mfa_secret)
        result['mfa_secret'] = secret
        result['provisioning_uri'] = pyotp.TOTP(secret).provisioning_uri(user.username, issuer_name='ODIN')
    return result


@app.post('/api/v1/auth/mfa/verify')
def verify(body: VerifyBody, request: Request, response: Response, db: Session = Depends(get_db)):
    rate_limit(db, 'mfa-ip:' + request.client.host, 30)
    challenge = db.scalar(select(Challenge).where(Challenge.token_hash == digest(request.cookies.get(CHALLENGE, ''))).with_for_update())
    if not challenge or challenge.expires_at <= now() or challenge.attempts >= 5:
        raise HTTPException(401, 'Vuelve a iniciar sesión')
    if not secrets.compare_digest(challenge.csrf_hash, digest(request.headers.get('x-csrf-token', ''))):
        raise HTTPException(403, 'Protección CSRF')
    user = db.scalar(select(User).where(User.id == challenge.user_id).with_for_update())
    if not user.active:
        raise HTTPException(401, 'Vuelve a iniciar sesión')
    if user.must_change_password and (not body.new_password or verify_password(user.password_hash, body.new_password)):
        raise HTTPException(422, 'Define una contraseña nueva de al menos 14 caracteres')
    challenge.attempts += 1
    if not consume_mfa(user, body.code.strip()):
        audit(db, 'mfa_failed', user.id)
        db.commit()
        raise HTTPException(401, 'Código no válido o ya utilizado')
    codes = []
    if not user.mfa_confirmed:
        codes = new_recovery_codes(user)
        user.mfa_confirmed = True
    if user.must_change_password:
        user.password_hash = hasher.hash(body.new_password)
        user.must_change_password = False
    # Prevent another preauthentication challenge from surviving enrollment.
    db.execute(delete(Challenge).where(Challenge.user_id == user.id))
    raw = token()
    csrf = digest('csrf:' + raw)
    db.add(AuthSession(token_hash=digest(raw), user_id=user.id, csrf_hash=digest(csrf), expires_at=now() + settings().session_seconds))
    audit(db, 'login_success', user.id)
    db.commit()
    response.delete_cookie(CHALLENGE, path='/api')
    set_cookie(response, COOKIE, raw, settings().session_seconds)
    return {'user': user_view(user), 'csrf_token': csrf, 'recovery_codes': codes}


@app.get('/api/v1/me')
def me(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    # Stable per session, safe across tabs; returned only to same-origin clients.
    csrf = digest('csrf:' + request.cookies[COOKIE])
    return {'user': user_view(user), 'csrf_token': csrf}


@app.post('/api/v1/auth/logout')
def logout(request: Request, response: Response, user: User = Depends(current), db: Session = Depends(get_db)):
    db.execute(delete(AuthSession).where(AuthSession.token_hash == request.state.auth.token_hash))
    audit(db, 'logout', user.id)
    db.commit()
    response.delete_cookie(COOKIE, path='/api')
    return {'ok': True}


@app.post('/api/v1/auth/recovery-codes')
def recovery(body: RecoveryBody, user: User = Depends(current), db: Session = Depends(get_db)):
    rate_limit(db, 'recovery:' + user.id, 5)
    user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not verify_password(user.password_hash, body.password) or not consume_mfa(user, body.code):
        db.commit()
        raise HTTPException(401, 'Verificación no válida')
    codes = new_recovery_codes(user)
    audit(db, 'recovery_codes_rotated', user.id)
    db.commit()
    return {'recovery_codes': codes}


@app.get('/api/v1/me/telegram')
def telegram_link(user: User = Depends(current), db: Session = Depends(get_db)):
    from .models import TelegramLink
    row = db.get(TelegramLink, user.id)
    return {'linked': bool(row and row.telegram_user_id), 'linked_at': row.linked_at if row else None,
            'configured': bool(db.get(Integration, 'telegram'))}


@app.post('/api/v1/me/telegram', status_code=201)
def telegram_code(user: User = Depends(current), db: Session = Depends(get_db)):
    from .telegram import new_code, CODE_SECONDS
    rate_limit(db, 'telegram-code:' + user.id, 10)
    code = new_code(db, user.id);db.commit()
    return {'code': code, 'expires_in': CODE_SECONDS}


@app.delete('/api/v1/me/telegram')
def telegram_unlink(user: User = Depends(current), db: Session = Depends(get_db)):
    from .models import TelegramLink
    row = db.get(TelegramLink, user.id)
    if row:db.delete(row);audit(db, 'telegram_unlinked', user.id, user.id)
    db.commit();return {'ok': True}


@app.get('/api/v1/integration-health')
def integration_health(user: User = Depends(current), db: Session = Depends(get_db)):
    from .inventory.service import integration_view
    configured = {x.kind:x for x in db.scalars(select(Integration))}
    return [{'kind': kind, **integration_view(configured.get(kind))} for kind in KINDS]


@app.get('/api/v1/admin/users')
def users(user: User = Depends(admin), db: Session = Depends(get_db)):
    return [user_view(x) for x in db.scalars(select(User).order_by(User.username).limit(200))]


@app.post('/api/v1/admin/users', status_code=201)
def create_user(body: CreateUserBody, user: User = Depends(admin), db: Session = Depends(get_db)):
    name = body.username.lower()
    if db.scalar(select(User).where(User.username == name)):
        raise HTTPException(409, 'El usuario ya existe')
    password = token()
    created = User(username=name, role=body.role, password_hash=hasher.hash(password))
    db.add(created)
    db.flush()
    audit(db, 'user_created', user.id, created.id)
    db.commit()
    return {'user': user_view(created), 'temporary_password': password}


@app.post('/api/v1/admin/users/{user_id}/revoke-sessions')
def revoke_sessions(user_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(404, 'Usuario no encontrado')
    revoke(db, user_id)
    audit(db, 'sessions_revoked', user.id, user_id)
    db.commit()
    return {'ok': True}


@app.post('/api/v1/admin/users/{user_id}/deactivate')
def deactivate(user_id: str, user: User = Depends(admin), db: Session = Depends(get_db)):
    if user_id == user.id:
        raise HTTPException(409, 'No puedes desactivar tu propia cuenta')
    db.execute(text('SELECT pg_advisory_xact_lock(712001)'))
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not target:
        raise HTTPException(404, 'Usuario no encontrado')
    # Serialize administrative deactivation and preserve at least one active admin.
    if target.role == 'admin':
        others = db.scalars(select(User.id).where(User.role == 'admin', User.active.is_(True), User.id != target.id)).all()
        if not others:
            raise HTTPException(409, 'Debe permanecer un administrador activo')
    target.active = False
    revoke(db, user_id)
    audit(db, 'user_deactivated', user.id, user_id)
    db.commit()
    return {'ok': True}


@app.get('/api/v1/admin/integrations')
def integrations(user: User = Depends(admin), db: Session = Depends(get_db)):
    from .inventory.service import integration_view
    return [{'kind': x.kind, 'endpoint': x.endpoint, 'has_secret': bool(x.encrypted_secret),
             'version':x.version,'allowed_networks':x.allowed_networks,'auto_sync':x.auto_sync,'model':x.model,
             **integration_view(x)} for x in db.scalars(select(Integration))]


@app.put('/api/v1/admin/integrations/{kind}')
def save_integration(kind: str, body: IntegrationBody, user: User = Depends(admin), db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(404, 'Integración desconocida')
    if kind == 'deepseek' and not body.endpoint.startswith('https://'):
        raise HTTPException(422, 'La API del modelo requiere HTTPS')
    from .inventory.service import lock_inventory
    lock_inventory(db)
    row = db.get(Integration, kind)
    if row and body.version is not None and body.version != row.version:
        raise HTTPException(409, 'La configuración cambió. Actualiza la página.')
    if row and row.endpoint != body.endpoint and db.scalar(select(SourceIdentity.id).where(SourceIdentity.source==kind).limit(1)):
        raise HTTPException(409, 'La fuente ya tiene inventario. No se puede sustituir por otra instancia.')
    if not row:
        row = Integration(kind=kind)
        db.add(row)
    row.endpoint = body.endpoint
    if body.secret is not None:
        row.encrypted_secret = encrypt(body.secret) if body.secret else None
    row.updated_at = now()
    row.version = (row.version or 0) + 1
    row.checked_at = None
    row.synced_at = None
    row.last_error = None
    if body.allowed_networks is not None:
        row.allowed_networks = body.allowed_networks
    if body.auto_sync is not None:
        row.auto_sync = body.auto_sync
    if kind == 'deepseek':
        from .analyst.llm import DEFAULT_MODEL
        row.model = body.model or row.model or DEFAULT_MODEL
    audit(db, 'integration_saved', user.id, kind)
    db.commit()
    return {'kind': kind, 'status': 'configured_not_validated'}


@app.get('/api/v1/admin/audit')
def audits(before: int | None = None, user: User = Depends(admin), db: Session = Depends(get_db)):
    query = select(Audit).order_by(Audit.created_at.desc(), Audit.id.desc()).limit(100)
    if before is not None:
        query = query.where(Audit.created_at < before)
    return [{'id': x.id, 'actor_id': x.actor_id, 'action': x.action, 'target': x.target,
             'created_at': x.created_at} for x in db.scalars(query)]


from .inventory.api import router as inventory_router
app.include_router(inventory_router(current, admin))

from .probes.api import router as probes_router
app.include_router(probes_router(current, admin))

from .events.api import router as events_router
app.include_router(events_router(current, admin))

from .cases.api import router as cases_router
app.include_router(cases_router(current, admin))

from .analyst.api import router as analyst_router
app.include_router(analyst_router(current, admin))
