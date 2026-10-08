import hashlib
import secrets
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from .config import settings
from .models import Audit, AuthSession, Challenge, RateBucket, now

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def verify_password(encoded, password):
    try:
        return hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def encrypt(value):
    return settings().cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return settings().cipher().decrypt(value.encode()).decode()


def audit(db, action, actor=None, target=None):
    db.add(Audit(action=action, actor_id=actor, target=target))


def revoke(db, user_id):
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    db.execute(delete(Challenge).where(Challenge.user_id == user_id))


def rate_limit(db, name, limit, seconds=300):
    stamp = now()
    key = digest(f'{name}:{stamp // seconds}')
    statement = insert(RateBucket).values(key=key, count=1, expires_at=stamp + seconds)
    count = db.execute(statement.on_conflict_do_update(
        index_elements=['key'], set_={'count': RateBucket.count + 1}
    ).returning(RateBucket.count)).scalar_one()
    db.commit()
    if count > limit:
        raise HTTPException(429, 'Demasiados intentos. Intenta más tarde.', headers={'Retry-After': str(seconds)})


def consume_mfa(user, code):
    current = now() // 30
    totp = pyotp.TOTP(decrypt(user.mfa_secret))
    for step in (current - 1, current, current + 1):
        if step > user.last_totp_step and secrets.compare_digest(totp.at(step * 30), code):
            user.last_totp_step = step
            return True
    if user.mfa_confirmed and digest(code) in user.recovery_hashes:
        user.recovery_hashes = [x for x in user.recovery_hashes if x != digest(code)]
        return True
    return False


def new_recovery_codes(user):
    codes = [secrets.token_hex(10) for _ in range(8)]
    user.recovery_hashes = [digest(code) for code in codes]
    return codes
