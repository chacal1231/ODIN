import pyotp
from sqlalchemy import select
from fastapi.testclient import TestClient
from odin.main import app
from odin.models import User, AuthSession, Audit, Integration, now
from odin.db import session
from odin.security import digest
from conftest import PASSWORD

NEW_PASSWORD = 'new-long-password-1234'


def enroll(client, username='admin', password=PASSWORD):
    login = client.post('/api/v1/auth/login', json={'username':username,'password':password})
    assert login.status_code == 200, login.text
    payload = login.json()
    client.headers['X-CSRF-Token'] = payload['csrf_token']
    secret = payload.get('mfa_secret')
    code = {'code':pyotp.TOTP(secret).now()} if secret else {}
    result = client.post('/api/v1/auth/mfa/verify', json={**code, 'new_password':NEW_PASSWORD})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']
    return result.json(), secret


def test_mfa_required_and_password_change(client, administrator):
    assert client.get('/api/v1/admin/users').status_code == 401
    login = client.post('/api/v1/auth/login', json={'username':'admin','password':PASSWORD}).json()
    assert login['enrollment_required'] and login['password_change_required']
    assert client.get('/api/v1/me').status_code == 401
    client.headers['X-CSRF-Token'] = login['csrf_token']
    assert client.post('/api/v1/auth/mfa/verify',json={'code':pyotp.TOTP(login['mfa_secret']).now()}).status_code == 422
    data,_ = enroll(client)
    assert len(data['recovery_codes']) == 8
    with session() as db:
        user = db.get(User, administrator)
        assert user.mfa_secret != login['mfa_secret']
        assert not user.must_change_password
        assert data['recovery_codes'][0] not in str(user.recovery_hashes)
    assert client.get('/api/v1/me').status_code == 200


def test_origin_csrf_and_roles(client, administrator):
    enroll(client)
    assert client.post('/api/v1/admin/users',json={'username':'operator'},headers={'Origin':'https://evil.invalid'}).status_code == 403
    assert client.post('/api/v1/admin/users',json={'username':'operator'},headers={'X-CSRF-Token':'bad'}).status_code == 403
    result = client.post('/api/v1/admin/users',json={'username':'operator'}).json()
    with TestClient(app,base_url='http://localhost:8080',headers={'Origin':'http://localhost:8080'}) as other:
        enroll(other,'operator',result['temporary_password'])
        assert other.get('/api/v1/admin/users').status_code == 403
        assert other.get('/api/v1/admin/audit').status_code == 403
        assert other.put('/api/v1/admin/integrations/zabbix',json={'endpoint':'https://internal.invalid'}).status_code == 403
        assert other.get('/api/v1/overview').status_code == 200
        client.post('/api/v1/admin/users/'+result['user']['id']+'/revoke-sessions')
        assert other.get('/api/v1/me').status_code == 401


def test_recovery_code_one_use_and_totp_replay(client, administrator):
    data,secret = enroll(client)
    client.post('/api/v1/auth/logout')
    def challenge():
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':NEW_PASSWORD}).json()
        assert 'mfa_secret' not in login
        client.headers['X-CSRF-Token']=login['csrf_token']
    challenge()
    assert client.post('/api/v1/auth/mfa/verify',json={'code':pyotp.TOTP(secret).now()}).status_code == 401
    result=client.post('/api/v1/auth/mfa/verify',json={'code':data['recovery_codes'][0]})
    assert result.status_code == 200
    client.headers['X-CSRF-Token']=result.json()['csrf_token']
    client.post('/api/v1/auth/logout')
    challenge()
    assert client.post('/api/v1/auth/mfa/verify',json={'code':data['recovery_codes'][0]}).status_code == 401


def test_session_expiry_and_restart(client, administrator):
    enroll(client)
    # A fresh application client using persisted opaque cookie is still authenticated.
    with TestClient(app,base_url='http://localhost:8080') as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get('/api/v1/me').status_code == 200
    with session() as db:
        auth=db.scalar(select(AuthSession))
        auth.last_seen=now()-1900
        db.commit()
    assert client.get('/api/v1/me').status_code == 401


def test_secrets_encrypted_and_audit_redacted(client, administrator):
    enroll(client)
    secret='sensitive-example-token'
    response=client.put('/api/v1/admin/integrations/zabbix',json={'endpoint':'https://zabbix.internal','secret':secret})
    assert response.status_code == 200
    assert secret not in client.get('/api/v1/admin/integrations').text
    assert secret not in client.get('/api/v1/admin/audit').text
    with session() as db:
        assert secret not in db.get(Integration,'zabbix').encrypted_secret
    status=client.get('/api/v1/integration-health').json()
    assert next(x for x in status if x['kind']=='zabbix')['status']=='configured_not_validated'


def test_rate_limit_persistent(client, administrator):
    for _ in range(8):
        assert client.post('/api/v1/auth/login',json={'username':'admin','password':'bad'}).status_code == 401
    assert client.post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD}).status_code == 429


def test_mfa_attempt_budget(client, administrator):
    login=client.post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD}).json()
    client.headers['X-CSRF-Token']=login['csrf_token']
    for _ in range(5):
        assert client.post('/api/v1/auth/mfa/verify',json={'code':'wrong-code','new_password':NEW_PASSWORD}).status_code == 401
    assert client.post('/api/v1/auth/mfa/verify',json={'code':pyotp.TOTP(login['mfa_secret']).now(),'new_password':NEW_PASSWORD}).status_code == 401


def test_no_operational_data_fabricated(client, administrator):
    enroll(client)
    summary=client.get('/api/v1/overview').json()
    assert summary['stage']=='E3' and summary['incidents']==0 and summary['targets']==0
    assert summary['coverage'] is None and summary['origin']['state']=='no_data'
    assert all(x['status']=='not_configured' for x in client.get('/api/v1/integration-health').json())


def test_deactivate_revokes_access(client, administrator):
    enroll(client)
    created=client.post('/api/v1/admin/users',json={'username':'operator'}).json()
    with TestClient(app,base_url='http://localhost:8080',headers={'Origin':'http://localhost:8080'}) as other:
        enroll(other,'operator',created['temporary_password'])
        assert client.post('/api/v1/admin/users/'+created['user']['id']+'/deactivate').status_code == 200
        assert other.get('/api/v1/me').status_code == 401


def test_password_validation_does_not_echo_secret(client, administrator):
    password='private-'+'x'*300
    result=client.post('/api/v1/auth/login',json={'username':'admin','password':password})
    assert result.status_code==422 and password not in result.text


def test_recovery_rotation_invalidates_previous_codes(client, administrator):
    data,_=enroll(client)
    result=client.post('/api/v1/auth/recovery-codes',json={'password':NEW_PASSWORD,'code':data['recovery_codes'][0]})
    assert result.status_code==200 and len(result.json()['recovery_codes'])==8
    client.post('/api/v1/auth/logout')
    login=client.post('/api/v1/auth/login',json={'username':'admin','password':NEW_PASSWORD}).json()
    client.headers['X-CSRF-Token']=login['csrf_token']
    assert client.post('/api/v1/auth/mfa/verify',json={'code':data['recovery_codes'][1]}).status_code==401
    assert client.post('/api/v1/auth/mfa/verify',json={'code':result.json()['recovery_codes'][0]}).status_code==200


def test_self_deactivation_blocked_and_csrf_stable(client, administrator):
    enroll(client)
    before=client.headers['X-CSRF-Token']
    assert client.get('/api/v1/me').json()['csrf_token']==before
    assert client.get('/api/v1/me').json()['csrf_token']==before
    assert client.post('/api/v1/admin/users/'+administrator+'/deactivate').status_code==409
    assert client.get('/api/v1/admin/users').status_code==200


def test_local_reset_revokes_and_requires_new_enrollment(client, administrator, monkeypatch):
    from odin.cli import main
    enroll(client)
    monkeypatch.setattr('sys.argv',['odin.cli','reset-access','admin'])
    monkeypatch.setattr('getpass.getpass',lambda _:PASSWORD)
    main()
    assert client.get('/api/v1/me').status_code==401
    login=client.post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD}).json()
    assert login['enrollment_required'] and login['password_change_required']


def test_branding_is_public_and_isp_name_is_bounded(client, monkeypatch):
    import pytest
    from odin.config import Settings
    assert client.get('/api/v1/branding').json() == {'isp_name': ''}
    monkeypatch.setenv('ODIN_ISP_NAME', 'Red Andina')
    assert Settings().isp_name == 'Red Andina'
    for bad in ('x' * 61, 'ISP\nfalso'):
        monkeypatch.setenv('ODIN_ISP_NAME', bad)
        with pytest.raises(ValueError):Settings()


def other_client():
    return TestClient(app, base_url='http://localhost:8080', headers={'Origin':'http://localhost:8080'})


def test_new_accounts_start_without_mfa(client, administrator):
    enroll(client)
    created = client.post('/api/v1/admin/users', json={'username':'operador'}).json()
    assert created['user']['mfa_required'] is False
    with other_client() as other:
        login = other.post('/api/v1/auth/login', json={'username':'operador','password':created['temporary_password']}).json()
        assert login['password_change_required'] and not login['mfa_required'] and not login['enrollment_required']
        assert 'mfa_secret' not in login
        other.headers['X-CSRF-Token'] = login['csrf_token']
        assert other.post('/api/v1/auth/mfa/verify', json={}).status_code == 422
        result = other.post('/api/v1/auth/mfa/verify', json={'new_password':NEW_PASSWORD})
        assert result.status_code == 200 and result.json()['recovery_codes'] == []
        assert other.get('/api/v1/me').status_code == 200
    with other_client() as other:
        # Once the password is changed, it alone opens the session.
        login = other.post('/api/v1/auth/login', json={'username':'operador','password':NEW_PASSWORD})
        assert login.status_code == 200 and login.json()['user']['username'] == 'operador'
        other.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert other.get('/api/v1/me').status_code == 200
        assert other.post('/api/v1/auth/recovery-codes', json={'password':NEW_PASSWORD,'code':'000000'}).status_code == 409
    with session() as db:
        user = db.scalar(select(User).where(User.username == 'operador'))
        assert user.mfa_secret is None and not user.mfa_confirmed


def test_admin_requires_and_removes_mfa(client, administrator):
    enroll(client)
    created = client.post('/api/v1/admin/users', json={'username':'operador'}).json()
    user_id = created['user']['id']
    with other_client() as other:
        enroll(other, 'operador', created['temporary_password'])
        assert client.post('/api/v1/admin/users/'+user_id+'/mfa', json={'required':True}).json()['mfa_required']
        # Its session was opened without a second factor.
        assert other.get('/api/v1/me').status_code == 401
        login = other.post('/api/v1/auth/login', json={'username':'operador','password':NEW_PASSWORD}).json()
        assert login['enrollment_required'] and login['mfa_secret']
        other.headers['X-CSRF-Token'] = login['csrf_token']
        assert other.post('/api/v1/auth/mfa/verify', json={}).status_code == 401
        result = other.post('/api/v1/auth/mfa/verify', json={'code':pyotp.TOTP(login['mfa_secret']).now()})
        assert result.status_code == 200 and len(result.json()['recovery_codes']) == 8
    assert not client.post('/api/v1/admin/users/'+user_id+'/mfa', json={'required':False}).json()['mfa_required']
    with session() as db:
        user = db.get(User, user_id)
        assert user.mfa_secret is None and not user.mfa_confirmed and user.recovery_hashes == []
    with other_client() as other:
        assert other.post('/api/v1/auth/login', json={'username':'operador','password':NEW_PASSWORD}).json()['user']
    actions = [a['action'] for a in client.get('/api/v1/admin/audit').json()]
    assert 'mfa_enabled' in actions and 'mfa_disabled' in actions
    assert client.post('/api/v1/admin/users', json={'username':'con2fa','mfa_required':True}).json()['user']['mfa_required']


def test_operator_cannot_change_mfa(client, administrator):
    enroll(client)
    created = client.post('/api/v1/admin/users', json={'username':'operador'}).json()
    with other_client() as other:
        enroll(other, 'operador', created['temporary_password'])
        assert other.post('/api/v1/admin/users/'+administrator+'/mfa', json={'required':False}).status_code == 403


def test_cli_admin_mfa_flag(monkeypatch):
    from odin.cli import main
    monkeypatch.setattr('getpass.getpass', lambda _: PASSWORD)
    for argv in (['create-admin', 'sin2fa'], ['create-admin', 'con2fa', '--mfa']):
        monkeypatch.setattr('sys.argv', ['odin.cli', *argv])
        main()
    with session() as db:
        assert {u.username: u.mfa_required for u in db.scalars(select(User))} == {'sin2fa': False, 'con2fa': True}
