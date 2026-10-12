import argparse
import getpass
import re
from sqlalchemy import select
from .db import session
from .models import User
from .security import hasher, audit, revoke


def main():
    parser = argparse.ArgumentParser(description='Administración local ODIN')
    parser.add_argument('action', choices=['create-admin', 'reset-access'])
    parser.add_argument('username')
    parser.add_argument('--mfa', action='store_true', help='create-admin: exigir 2FA (TOTP) desde el primer ingreso')
    args = parser.parse_args()
    name = args.username.lower()
    if not re.fullmatch(r'[a-z0-9._-]{3,80}', name):
        parser.error('Nombre no válido')
    password = getpass.getpass('Contraseña temporal (mínimo 14 caracteres): ')
    if len(password) < 14 or len(password) > 256 or password != getpass.getpass('Repetir contraseña: '):
        parser.error('Contraseñas inválidas o diferentes')
    with session() as db:
        user = db.scalar(select(User).where(User.username == name).with_for_update())
        if args.action == 'create-admin':
            if user:
                parser.error('Usuario existente')
            user = User(username=name, role='admin', password_hash=hasher.hash(password), mfa_required=args.mfa)
            db.add(user)
            db.flush()
        else:
            if not user:
                parser.error('Usuario inexistente')
            revoke(db, user.id)
            user.password_hash = hasher.hash(password)
            user.mfa_secret = None
            user.mfa_confirmed = False
            user.last_totp_step = -1
            user.recovery_hashes = []
            user.must_change_password = True
            user.active = True
        audit(db, 'local_' + args.action, target=user.id)
        mfa = user.mfa_required
        db.commit()
    print('Cuenta preparada. Debe cambiar la contraseña al ingresar' + (' y registrar 2FA.' if mfa else '.'))


if __name__ == '__main__':
    main()
