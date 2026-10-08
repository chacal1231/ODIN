import errno
import json
import socket
import ssl
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select,func
from odin.db import session
from odin.models import ProbeCatalog,ProbeTarget
from odin.probes import transport,transport_worker,resolver,import_catalog
from odin.probes.config import ProbeConfig,TransportConfig,measurement_config
from odin.probes.detector import window_metrics,transition


def config(protocol='https'):
    return measurement_config(ProbeConfig(interval_seconds=60,packets=1,period_ms=2000,timeout_ms=2000).model_dump(),protocol,{})


def document():
    return json.loads((Path(import_catalog.__file__).parent/'data/hyperscalers-v1.json').read_text())


def test_catalog_persistence_idempotent_and_preserves_edits():
    source=document();ids=import_catalog.persist(source)
    assert len(ids)==105
    with session() as db:
        assert db.get(ProbeCatalog,'hyperscalers').document==source
        targets=db.scalars(select(ProbeTarget)).all()
        assert sum(t.protocol=='tcp' for t in targets)==91
        assert sum(t.protocol=='https' for t in targets)==10
        assert sum(t.family=='ipv6' for t in targets)==2
        assert all(not t.enabled for t in targets)
        target=targets[0];target.description='Edición NOC';target.archived=True;db.commit()
    assert len(import_catalog.persist(source))==104
    with session() as db:
        assert db.scalar(select(func.count()).select_from(ProbeTarget))==105
        assert db.get(ProbeTarget,target.id).description=='Edición NOC'
    source['version']=2
    with pytest.raises(ValueError):import_catalog.persist(source)


def test_catalog_activation_and_failed_validation(monkeypatch):
    ids=import_catalog.persist(document())
    monkeypatch.setattr(import_catalog,'resolve_task',lambda task:['8.8.8.8'])
    monkeypatch.setattr(import_catalog.transport,'run',lambda *args:{'8.8.8.8':{'sent':1,'received':1,'samples':[1]}})
    assert import_catalog.validate_one(ids[0],False)['status']=='validated'
    assert import_catalog.validate_one(ids[0],True)['enabled']
    with session() as db:
        t=db.get(ProbeTarget,ids[0]);t.enabled=False;db.commit()
    assert import_catalog.validate_one(ids[0],True)['status']=='preserved'
    monkeypatch.setattr(import_catalog.transport,'run',lambda *args:{'8.8.8.8':{'sent':1,'received':0,'samples':[None]}})
    result=import_catalog.validate_one(ids[1],True)
    assert not result['enabled'] and result['error']=='endpoint_check_failed'


def test_public_dns_rejects_private_and_accepts_rotation(monkeypatch):
    task={'address':'cloud.example.com','family':'ipv4','dns_policy':'public_dns','approved_ips':['8.8.8.8']}
    monkeypatch.setattr(resolver,'resolve',lambda *args:['10.1.2.3'])
    with pytest.raises(resolver.ProbeError,match='dns_non_public_address'):resolver.resolve_task(task)
    monkeypatch.setattr(resolver,'resolve',lambda *args:['1.1.1.1'])
    assert resolver.resolve_task(task)==['1.1.1.1']
    task['dns_policy']='approved_ips'
    with pytest.raises(resolver.ProbeError,match='dns_approval_required'):resolver.resolve_task(task)


def test_transport_failure_is_not_packet_loss():
    c=config('tcp')
    rows=[SimpleNamespace(status='valid',slot=i*60,sent=1,received=0,samples=[None]) for i in range(6)]
    m=window_metrics(rows,c,300,0)
    assert m['failure_percent']==100 and m['loss_percent'] is None
    conditions,counters={},{}
    for _ in range(3):conditions,counters=transition(conditions,counters,m,c,0)
    assert conditions['endpoint_unavailable']=='critical'
    assert 'loss' not in conditions and 'unreachable' not in conditions


def test_transport_timeout_and_local_error_are_no_data(monkeypatch):
    monkeypatch.setattr(transport.subprocess,'run',Mock(side_effect=subprocess.TimeoutExpired('worker',5)))
    with pytest.raises(resolver.ProbeError,match='probe_timeout'):transport.run(['8.8.8.8'],'dns.google',config())
    data={'8.8.8.8':{'sent':1,'received':0,'samples':[None],'details':{'checks':[{'status':'invalid'}]}}}
    monkeypatch.setattr(transport.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(data)))
    with pytest.raises(resolver.ProbeError,match='probe_local_network_error'):transport.run(['8.8.8.8'],'dns.google',config())


def test_numeric_tcp_connection():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen()
        port=listener.getsockname()[1]
        result=transport_worker.attempt('127.0.0.1','unused.example.com','tcp',{'port':listener.getsockname()[1]},1)
        assert result['status']=='ok' and result['tcp_ms']>=0
        conn,_=listener.accept();conn.close()
    assert transport_worker.attempt('127.0.0.1','unused.example.com','tcp',{'port':port},.1)['status']=='failed'


def test_local_route_error_is_not_remote_failure(monkeypatch):
    mock=Mock();mock.connect.side_effect=OSError(errno.ENETUNREACH,'unreachable')
    monkeypatch.setattr(transport_worker.socket,'socket',lambda *a:mock)
    assert transport_worker.attempt('8.8.8.8','dns.google','tcp',{'port':443},1)['status']=='invalid'


@pytest.mark.parametrize('trusted,status,expected',[(True,302,'ok'),(True,503,'failed'),(False,200,'failed')])
def test_https_certificate_sni_host_and_no_redirect(tmp_path,monkeypatch,trusted,status,expected):
    # A local TLS endpoint exercises the actual handshake and HTTP parser.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime,timedelta,timezone
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'cloud.example.com')])
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1)).not_valid_after(datetime.now(timezone.utc)+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('cloud.example.com')]),critical=False).sign(key,hashes.SHA256())
    certfile=tmp_path/'cert.pem';keyfile=tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM));keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certfile,keyfile)
    observed={};context.set_servername_callback(lambda sock,name,ctx:observed.update(sni=name))
    if trusted:monkeypatch.setenv('SSL_CERT_FILE',str(certfile))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
        port=listener.getsockname()[1]
        def serve():
            conn,_=listener.accept()
            try:
                with context.wrap_socket(conn,server_side=True) as tls:
                    observed['request']=tls.recv(4096).decode()
                    tls.sendall(f'HTTP/1.1 {status} Test\r\nLocation: https://redirect.example.com/\r\nContent-Length: 0\r\n\r\n'.encode())
            except ssl.SSLError:conn.close()
        thread=threading.Thread(target=serve);thread.start()
        result=transport_worker.attempt('127.0.0.1','cloud.example.com','https',TransportConfig(port=port).model_dump(),2)
        thread.join(4)
    assert not thread.is_alive() and result['status']==expected
    assert observed['sni']=='cloud.example.com'
    if trusted:
        assert f'Host: cloud.example.com:{port}' in observed['request']
        assert observed['request'].startswith('HEAD / HTTP/1.1')
        assert result['http_status']==status
    else:assert result['reason']=='tls_certificate_invalid'


def test_transport_limits_and_header_injection():
    with pytest.raises(ValueError):TransportConfig(path='/\r\nHost: other')
    with pytest.raises(ValueError):TransportConfig(method='POST')
    with pytest.raises(ValueError):measurement_config(ProbeConfig().model_dump(),'tcp',{})
    with pytest.raises(resolver.ProbeError,match='probe_round_budget'):
        transport.run(['8.8.8.8']*16,'dns.google',{**config(), 'interval_seconds':30})


def test_icmp_missing_route_is_no_data(monkeypatch):
    from odin.probes import fping
    route=Mock();route.__enter__=Mock(return_value=route);route.__exit__=Mock(return_value=False)
    route.connect.side_effect=OSError(errno.ENETUNREACH,'unreachable')
    monkeypatch.setattr(fping.socket,'socket',lambda *a:route)
    with pytest.raises(resolver.ProbeError,match='probe_local_network_error'):
        fping.run(['2001:4860:4860::8888'],ProbeConfig().model_dump())
