import ipaddress
from sqlalchemy import select
from fastapi.testclient import TestClient
from odin.main import app
from odin.db import session
from odin.models import (Pop,Device,Integration,SourceIdentity,Candidate,Assignment,InventoryInterface,SyncRun,Job,now)
from odin.inventory.service import apply_snapshot,queue_run,execute,lock_inventory
from odin.inventory.connectors import Zabbix,LibreNMS,Transport,ConnectorError,allowed_address
from odin.security import encrypt
from odin.jobs import claim
from test_access import enroll
import pytest


def sample(external='1', name='PE-NORTE', ip='10.10.1.1'):
    return {'external_id':external,'name':name,'addresses':[ip],'details':{'hostname':name,'location':'Norte'},
            'interfaces':[{'external_id':'1','name':'Ethernet1','kind':'port','addresses':[]}]}


def source(db,kind):
    row=Integration(kind=kind,endpoint='https://example.test',encrypted_secret=encrypt('test-token'),allowed_networks=['10.0.0.0/8'])
    db.add(row);db.flush();return row


def test_pop_crud_coordinates_and_stale_version(client,administrator):
    enroll(client)
    response=client.post('/api/v1/pops',json={'name':'Norte'})
    assert response.status_code==201
    pop=response.json();assert pop['latitude'] is None
    assert client.post('/api/v1/pops',json={'name':'norte'}).status_code==409
    assert client.post('/api/v1/pops',json={'name':'Bad','latitude':4}).status_code==422
    assert client.put('/api/v1/pops/'+pop['id'],json={'name':'Norte','latitude':4.6,'longitude':-74.1,'version':99}).status_code==409
    result=client.put('/api/v1/pops/'+pop['id'],json={'name':'Norte','latitude':4.6,'longitude':-74.1,'version':1})
    assert result.status_code==200
    assert client.post('/api/v1/pops/'+pop['id']+'/archive',json={'version':2}).status_code==200
    assert client.get('/api/v1/pops').json()==[]


def test_sync_preserves_manual_assignment_and_missing_devices(client,administrator):
    enroll(client)
    pop=client.post('/api/v1/pops',json={'name':'Norte'}).json()
    with session() as db:
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample()],now());db.commit()
        device=db.scalar(select(Device));device_id=device.id
    assert client.put('/api/v1/devices/'+device_id,json={'pop_id':pop['id'],'name':'Nombre manual','version':1}).status_code==200
    with session() as db:
        apply_snapshot(db,'zabbix',[sample(name='Nombre cambiado en Zabbix')],now());db.commit()
        device=db.get(Device,device_id);assert device.name=='Nombre manual' and device.pop_id==pop['id']
        apply_snapshot(db,'zabbix',[],now());db.commit()
        assert db.get(Device,device_id).pop_id==pop['id']
        assert db.scalar(select(SourceIdentity)).present is False
        assert db.scalar(select(InventoryInterface)).present is False
        assert len(list(db.scalars(select(Assignment))))==1
    assert client.post('/api/v1/pops/'+pop['id']+'/archive',json={'version':1}).status_code==409


def test_confirmed_merge_survives_resync_and_dismissal(client,administrator):
    enroll(client)
    with session() as db:
        source(db,'zabbix');source(db,'librenms')
        apply_snapshot(db,'zabbix',[sample()],now());apply_snapshot(db,'librenms',[sample('22')],now());db.commit()
    candidates=client.get('/api/v1/inventory/reconciliation').json();assert len(candidates)==1
    c=candidates[0]
    result=client.post('/api/v1/inventory/reconciliation/'+c['id'],json={'action':'merge','keep_id':c['left']['id'],'left_version':1,'right_version':1})
    assert result.status_code==200,result.text
    with session() as db:
        apply_snapshot(db,'zabbix',[sample()],now());apply_snapshot(db,'librenms',[sample('22')],now());db.commit()
        devices=list(db.scalars(select(Device).where(Device.archived.is_(False))))
        assert len(devices)==1
        assert len(list(db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==devices[0].id))))==2
    assert client.get('/api/v1/inventory/reconciliation').json()==[]


def test_conflicting_pop_assignments_block_merge(client,administrator):
    enroll(client)
    with session() as db:
        source(db,'zabbix');source(db,'librenms');apply_snapshot(db,'zabbix',[sample()],now());apply_snapshot(db,'librenms',[sample('22')],now());db.commit()
        devices=list(db.scalars(select(Device)))
        for i,d in enumerate(devices):
            p=Pop(name=str(i));db.add(p);db.flush();d.pop_id=p.id
        db.commit()
    c=client.get('/api/v1/inventory/reconciliation').json()[0]
    assert client.post('/api/v1/inventory/reconciliation/'+c['id'],json={'action':'merge','keep_id':c['left']['id'],'left_version':1,'right_version':1}).status_code==409
    assert client.post('/api/v1/inventory/reconciliation/'+c['id'],json={'action':'dismiss','left_version':1,'right_version':1}).status_code==200
    with session() as db:
        apply_snapshot(db,'zabbix',[sample()],now());db.commit()
    assert client.get('/api/v1/inventory/reconciliation').json()==[]


def test_worker_failure_preserves_snapshot_and_marks_error(monkeypatch):
    with session() as db:
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample()],now());queue_run(db,'zabbix','sync');db.commit();job=claim(db)
    def fail(*args,**kwargs):raise ConnectorError('connection_failed')
    monkeypatch.setattr('odin.inventory.service.connector',fail)
    execute(job.id,job.lease_token)
    with session() as db:
        assert db.scalar(select(SourceIdentity)).present
        assert db.get(Integration,'zabbix').last_error=='connection_failed'
        assert db.scalar(select(SyncRun)).status=='retrying'


def test_config_change_cancels_old_work():
    with session() as db:
        row=source(db,'zabbix');queue_run(db,'zabbix','sync');db.commit();job=claim(db)
        row.version+=1;db.commit()
    execute(job.id,job.lease_token)
    with session() as db:
        assert db.scalar(select(SyncRun)).status=='cancelled'
        assert not db.scalar(select(Device))


def test_worker_success_and_duplicate_requests(monkeypatch):
    class Fake:
        def fetch(self):return [sample()]
    monkeypatch.setattr('odin.inventory.service.connector',lambda *a:Fake())
    with session() as db:
        source(db,'librenms');a=queue_run(db,'librenms','sync');b=queue_run(db,'librenms','sync');assert a.id==b.id
        db.commit();job=claim(db)
    execute(job.id,job.lease_token)
    with session() as db:
        assert db.scalar(select(SyncRun)).status=='done'
        assert db.get(Integration,'librenms').synced_at
        assert db.get(Job,job.id).status=='done'
        assert db.scalar(select(SourceIdentity)).source=='librenms'


def test_operator_reads_but_cannot_modify_or_sync(client,administrator):
    enroll(client)
    created=client.post('/api/v1/admin/users',json={'username':'reader'}).json()
    with TestClient(app,base_url='http://localhost:8080',headers={'Origin':'http://localhost:8080'}) as other:
        enroll(other,'reader',created['temporary_password'])
        assert other.get('/api/v1/pops').status_code==200
        assert other.get('/api/v1/devices').status_code==200
        assert other.post('/api/v1/pops',json={'name':'Forbidden'}).status_code==403
        assert other.post('/api/v1/admin/integrations/zabbix/sync').status_code==403


def test_device_pagination(client,administrator):
    enroll(client)
    with session() as db:
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample(str(i),f'PE-{i}',f'10.1.0.{i+1}') for i in range(5)],now());db.commit()
    first=client.get('/api/v1/devices?limit=2').json();assert len(first['items'])==2
    second=client.get('/api/v1/devices?limit=2&after='+first['next_cursor']).json()
    assert not {x['id'] for x in first['items']} & {x['id'] for x in second['items']}


def test_zabbix_batches_and_redacts_payload(monkeypatch):
    calls=[]
    def request(self,method,url,headers=None,payload=None):
        calls.append(payload)
        params=payload['params']
        if 'hostids' not in params:return {'result':[{'hostid':'1'}]}
        return {'result':[{'hostid':'1','host':'PE','name':'PE','interfaces':[{'interfaceid':'2','ip':'10.0.0.1','details':{'community':'SECRET'}}],
                          'inventory':{'serialno_a':'SERIAL','location':'Norte','password':'SECRET'},'tags':[]}]}
    monkeypatch.setattr(Transport,'request',request)
    result=Zabbix('https://zabbix.test','TOKEN',['10.0.0.0/8']).fetch()
    assert len(calls)==3 and calls[1]['params']['hostids']==['1']
    assert result[0]['interfaces'][0]['kind']=='management'
    assert 'SECRET' not in str(result) and 'TOKEN' not in str(result)


def test_librenms_full_snapshot_and_ports(monkeypatch):
    def request(self,method,url,**kwargs):
        assert method=='GET' and kwargs['headers']['X-Auth-Token']=='TOKEN'
        if '/ports?' in url:return {'status':'ok','count':1,'ports':[{'port_id':1,'ifName':'Ethernet1'}]}
        return {'status':'ok','count':1,'devices':[{'device_id':9,'hostname':'10.0.0.1','sysName':'PE','community':'SECRET'}]}
    monkeypatch.setattr(Transport,'request',request)
    result=LibreNMS('https://nms.test','TOKEN',['10.0.0.0/8']).fetch()
    assert result[0]['external_id']=='9' and result[0]['interfaces'][0]['kind']=='port'
    assert 'SECRET' not in str(result)


def test_librenms_ports_bring_addresses_with_prefix_and_description(monkeypatch):
    def request(self,method,url,**kwargs):
        if url.endswith('/ip'):
            if '/devices/8/' in url:raise ConnectorError('http_error','No IP addresses')  # LibreNMS answers 404
            return {'status':'ok','addresses':[{'port_id':1,'ipv4_address':'10.172.0.18','ipv4_prefixlen':30},
                                               {'port_id':1,'ipv6_compressed':'2001:db8:4890:1::8','ipv6_prefixlen':126},{'port_id':2,'ipv4_address':'x'}]}
        if '/ports?' in url:return {'status':'ok','count':1,'ports':[{'port_id':1,'ifName':'Vlan1003','ifAlias':'PTP-PROV'}]}
        return {'status':'ok','count':2,'devices':[{'device_id':8,'sysName':'SW'},{'device_id':9,'sysName':'PE'}]}
    monkeypatch.setattr(Transport,'request',request)
    sw,pe=LibreNMS('https://nms.test','TOKEN',['10.0.0.0/8']).fetch()
    assert sw['interfaces'][0]['addresses']==[]
    assert pe['interfaces'][0]['addresses']==['10.172.0.18/30','2001:db8:4890:1::8/126'] and pe['interfaces'][0]['description']=='PTP-PROV'


@pytest.mark.parametrize('description,expected',[
    ('TRANSITO-CARRIER-100G',('transit','CARRIER',{})),
    ('TRANS-TX-POP-OESTE-RUTA-01',('transport','TX',{})),
    ('TRANSPORTE-CENTRO-OESTE-INTXA',('transport','CENTRO',{})),
    ('PIT-IXP',('ixp','IXP',{})),
    ('PTP-PROV',('peering','PROV',{})),
    ('PNI-CDN-01-100G',('peering','CDN',{})),
    ('CUST: ALFA (RIO-01) [500Mb] {DIA:DIA-0102 | ASN:64510 | CT:ISP}',('customer','ALFA',{'dia':'DIA-0102','asn':'64510'})),
    ('BRAVO (ESTE-PE) [1G] {DIA:DIA-0103 | ASN:AS64511 | CT:ISP}',('customer','BRAVO',{'dia':'DIA-0103','asn':'64511'})),
    ('CUST: VALLE-ISP (Cali) [3G] {DIA:DIA-0111 | ASN:NA | CT:ISP}',('customer','VALLE-ISP',{'dia':'DIA-0111'})),
    ('DIA-0009-CHARLIE-OESTE',('customer','CHARLIE-OESTE',{'dia':'DIA-0009'})),
    ('OCA01-NETFLIX',('cdn','NETFLIX',{})),('CDN-AKAMAI-01',('cdn','AKAMAI',{})),
    ('REDNET-PLAYON',(None,'',{})),('',(None,'',{}))])
def test_port_descriptions_classify_by_the_noc_convention(description,expected):
    from odin.inventory.roles import classify
    assert classify(description)==expected


def test_neighbor_is_found_by_subnet_and_internal_links_name_the_far_end():
    from odin.inventory.roles import Network, describe
    with session() as db:
        db.add(Integration(kind='librenms',endpoint='http://x',allowed_networks=[]));db.flush()
        centro,oeste=Device(name='pe-centro01'),Device(name='pe-oeste');db.add_all([centro,oeste]);db.flush()
        for device,ports in ((centro,[('Ethernet51/1','TRANSITO-CARRIER-100G',['198.51.100.133/30']),('Vlan1545','TRANS-TX-POP-OESTE-RUTA-01',['10.254.3.2/30']),
                                     ('Loopback0','',['10.0.0.1/32'])]),
                             (oeste,[('Vlan500','TRANS-TX-POP-CENTRO',['10.254.3.3/30'])])):
            identity=SourceIdentity(source='librenms',external_id=device.name,device_id=device.id,name=device.name,addresses=[],details={},last_seen=1)
            db.add(identity);db.flush()
            for n,(name,description,addresses) in enumerate(ports):
                db.add(InventoryInterface(identity_id=identity.id,external_id=str(n),name=name,kind='port',addresses=addresses,description=description,last_seen=1))
        db.flush();network=Network(db)
        carrier=network.neighbor('198.51.100.134')
        assert (carrier['role'],carrier['provider'],carrier['interface'],carrier['remote_device_id'])==('transit','CARRIER','Ethernet51/1',None)
        assert describe(carrier).startswith('tránsito CARRIER; interfaz Ethernet51/1 de pe-centro01 (198.51.100.132/30')
        link=network.neighbor('10.254.3.3',centro.id)
        assert (link['device_name'],link['remote_device_name'],link['remote_interface'],link['role'])==('pe-centro01','pe-oeste','Vlan500','transport')
        assert describe(link).startswith('enlace interno con pe-oeste (Vlan500) por transporte; interfaz Vlan1545')
        assert network.neighbor('10.0.0.2') is None and network.neighbor('no-ip') is None


def test_incomplete_provider_response_rejected(monkeypatch):
    monkeypatch.setattr(Transport,'request',lambda *a,**k:{'status':'ok','count':2,'devices':[{'device_id':1}]})
    with pytest.raises(ConnectorError,match='incomplete_snapshot'):
        LibreNMS('https://nms.test','token',[]).fetch()


def test_outbound_dns_validation(monkeypatch):
    def resolve(ip):monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',(ip,443))])
    resolve('10.0.0.10');assert allowed_address('nms.test',443,['10.0.0.0/24'])=='10.0.0.10'
    with pytest.raises(ConnectorError,match='network_not_allowed'):allowed_address('nms.test',443,['10.2.0.0/24'])
    for ip in ('127.0.0.1','169.254.169.254','224.0.0.1'):
        resolve(ip)
        with pytest.raises(ConnectorError,match='network_not_allowed'):allowed_address('nms.test',443,['0.0.0.0/0'])


def test_saved_source_change_conflict_and_connections_role(client,administrator):
    enroll(client)
    response=client.put('/api/v1/admin/integrations/zabbix',json={'endpoint':'https://zabbix.test','secret':'token','allowed_networks':['10.0.0.15/32']})
    assert response.status_code==200
    assert client.post('/api/v1/admin/integrations/zabbix/test').status_code==202
    with session() as db:
        apply_snapshot(db,'zabbix',[sample()],now());db.commit()
    assert client.put('/api/v1/admin/integrations/zabbix',json={'endpoint':'https://another.test'}).status_code==409


def test_invalid_snapshot_rolls_back_entire_import(monkeypatch):
    class Fake:
        def fetch(self):
            return [sample('2'), sample('2')]
    monkeypatch.setattr('odin.inventory.service.connector',lambda *a:Fake())
    with session() as db:
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample()],now())
        queue_run(db,'zabbix','sync');db.commit();job=claim(db)
    execute(job.id,job.lease_token)
    with session() as db:
        assert len(list(db.scalars(select(Device))))==1
        assert db.scalar(select(SourceIdentity)).external_id=='1'
        assert db.scalar(select(SourceIdentity)).present
        assert db.scalar(select(SyncRun)).error_code=='incomplete_snapshot'
        assert db.get(Integration,'zabbix').last_error=='incomplete_snapshot'


def test_dual_stack_prefers_ipv4_after_validating_all_answers(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:db8:100:2::3a', 9001, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.43', 9001)),
    ])
    assert allowed_address('nms.test', 9001, ['203.0.113.43/32', '2001:db8:100:2::3a/128']) == '203.0.113.43'
    with pytest.raises(ConnectorError, match='network_not_allowed'):
        allowed_address('nms.test', 9001, ['203.0.113.43/32'])


def physical_fixture(db):
    source(db,'librenms')
    apply_snapshot(db,'librenms',[sample('a','PE-A','10.1.0.1'),sample('b','PE-B','10.1.0.2')],now())
    db.flush()
    identities={i.external_id:i for i in db.scalars(select(SourceIdentity))}
    result={}
    for key,identity in identities.items():
        port=db.scalar(select(InventoryInterface).where(InventoryInterface.identity_id==identity.id))
        result[key]=(identity.device_id,port.id)
    return result


def test_physical_link_ports_vlans_and_map(client,administrator):
    enroll(client)
    a=client.post('/api/v1/pops',json={'name':'A','latitude':4,'longitude':-74}).json()
    b=client.post('/api/v1/pops',json={'name':'B','latitude':5,'longitude':-75}).json()
    with session() as db:
        ends=physical_fixture(db)
        db.get(Device,ends['a'][0]).pop_id=a['id'];db.get(Device,ends['b'][0]).pop_id=b['id'];db.commit()
    body={'left_id':ends['a'][0],'right_id':ends['b'][0],'left_port_id':ends['a'][1],'right_port_id':ends['b'][1],'vlans':[200,100,200]}
    response=client.post('/api/v1/inventory/links',json=body)
    assert response.status_code==201,response.text
    row=client.get('/api/v1/inventory/links').json()[0]
    assert row['vlans']==[100,200] and row['left_port_name']=='Ethernet1'
    assert row['map_points']==[[4,-74],[5,-75]] and row['map_reason'] is None
    assert client.post('/api/v1/inventory/links',json={**body,'left_id':body['right_id'],'right_id':body['left_id'],
        'left_port_id':body['right_port_id'],'right_port_id':body['left_port_id']}).status_code==409
    with session() as db:
        apply_snapshot(db,'librenms',[],now());db.commit()
    row=client.get('/api/v1/inventory/links').json()[0]
    assert row['left_port_present'] is False and row['vlans']==[100,200]
    assert client.post('/api/v1/inventory/links/'+row['id']+'/archive').status_code==200
    assert client.get('/api/v1/inventory/links').json()==[]


def test_physical_link_rejects_foreign_missing_or_management_port(client,administrator):
    enroll(client)
    with session() as db:
        ends=physical_fixture(db);db.commit()
    body={'left_id':ends['a'][0],'right_id':ends['b'][0],'left_port_id':ends['a'][1],'right_port_id':ends['b'][1]}
    assert client.post('/api/v1/inventory/links',json={**body,'left_port_id':ends['b'][1]}).status_code==422
    assert client.post('/api/v1/inventory/links',json={**body,'right_port_id':'missing'}).status_code==422
    for vlans in [[0],[4095],['100'],[True]]:
        assert client.post('/api/v1/inventory/links',json={**body,'vlans':vlans}).status_code==422
    with session() as db:
        db.get(InventoryInterface,ends['a'][1]).kind='management';db.commit()
    assert client.get('/api/v1/devices/'+ends['a'][0]+'/ports').json()['items']==[]
    assert client.post('/api/v1/inventory/links',json=body).status_code==422


def test_link_geometry_follows_pop_assignment_and_legacy_records(client,administrator):
    from odin.models import InventoryLink
    enroll(client)
    with session() as db:
        ends=physical_fixture(db)
        a=Pop(name='A');b=Pop(name='B',latitude=5,longitude=-75)
        db.add_all([a,b]);db.flush()
        db.add(InventoryLink(left_id=ends['a'][0],right_id=ends['b'][0],actor_id=administrator))
        db.commit()
        row=client.get('/api/v1/inventory/links').json()[0]
        assert row['map_points'] is None and row['map_reason']=='unassigned' and row['vlans']==[]
        db.get(Device,ends['a'][0]).pop_id=a.id;db.get(Device,ends['b'][0]).pop_id=b.id;db.commit()
        assert client.get('/api/v1/inventory/links').json()[0]['map_reason']=='missing_coordinates'
        a.latitude=4;a.longitude=-74;db.commit()
        assert client.get('/api/v1/inventory/links').json()[0]['map_points']==[[4,-74],[5,-75]]
        db.get(Device,ends['b'][0]).pop_id=a.id;db.commit()
        assert client.get('/api/v1/inventory/links').json()[0]['map_reason']=='same_location'


def test_port_search_and_pagination(client,administrator):
    enroll(client)
    with session() as db:
        ends=physical_fixture(db)
        identity=db.get(InventoryInterface,ends['a'][1]).identity_id
        for i in range(105):db.add(InventoryInterface(identity_id=identity,external_id='port'+str(i),name='Et'+str(i),kind='port',last_seen=now()))
        db.commit()
    url='/api/v1/devices/'+ends['a'][0]+'/ports'
    first=client.get(url).json();assert len(first['items'])==100 and first['next_cursor']
    second=client.get(url,params={'after':first['next_cursor']}).json()
    assert len(second['items'])==6 and not second['next_cursor']
    assert not ({x['id'] for x in first['items']} & {x['id'] for x in second['items']})
    found=client.get(url,params={'q':'Et104'}).json()['items']
    assert len(found)==1 and found[0]['name']=='Et104'


def test_each_endpoint_lists_only_its_own_ports_and_marks_devices_without_ports(client,administrator):
    enroll(client)
    with session() as db:
        ends=physical_fixture(db)
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample('z','Zabbix-only','10.9.0.1')],now())
        z=db.scalar(select(SourceIdentity).where(SourceIdentity.source=='zabbix'))
        db.scalar(select(InventoryInterface).where(InventoryInterface.identity_id==z.id)).kind='management'
        db.commit();z_id=z.device_id
    a=client.get('/api/v1/devices/'+ends['a'][0]+'/ports').json()['items']
    b=client.get('/api/v1/devices/'+ends['b'][0]+'/ports').json()['items']
    assert [p['id'] for p in a]==[ends['a'][1]]
    assert [p['id'] for p in b]==[ends['b'][1]]
    assert client.get('/api/v1/devices/'+z_id+'/ports').json()['items']==[]
    devices={d['id']:d for d in client.get('/api/v1/devices').json()['items']}
    assert devices[z_id]['port_count']==0
    assert devices[ends['a'][0]]['port_count']==1 and devices[ends['b'][0]]['port_count']==1


def test_manual_alias_merge_keeps_ports_links_name_and_source_ids_after_resync(client,administrator):
    from odin.models import InventoryLink
    enroll(client)
    with session() as db:
        source(db,'zabbix');source(db,'librenms')
        z=sample('z','PE-OESTE-ARISTA','10.1.1.246');z['interfaces'][0]['kind']='management'
        n=sample('n','pe-oeste','10.1.1.17');other=sample('x','other','10.1.2.1')
        apply_snapshot(db,'zabbix',[z],now());apply_snapshot(db,'librenms',[n,other],now());db.commit()
        identities={(i.source,i.external_id):i for i in db.scalars(select(SourceIdentity))}
        keep=identities[('zabbix','z')].device_id;drop=identities[('librenms','n')].device_id
        port=db.scalar(select(InventoryInterface).where(InventoryInterface.identity_id==identities[('librenms','n')].id))
        peerport=db.scalar(select(InventoryInterface).where(InventoryInterface.identity_id==identities[('librenms','x')].id))
        db.add(InventoryLink(left_id=drop,right_id=identities[('librenms','x')].device_id,
            left_port_id=port.id,right_port_id=peerport.id,vlans=[100,200],actor_id=administrator));db.commit()
        port_id=port.id
    assert client.get('/api/v1/inventory/reconciliation').json()==[]
    result=client.post('/api/v1/inventory/reconciliation/manual',json={
        'left_id':drop,'right_id':keep,'keep_id':keep,'left_version':1,'right_version':1})
    assert result.status_code==200,result.text
    assert result.json()['device_id']==keep
    with session() as db:
        apply_snapshot(db,'zabbix',[z],now());apply_snapshot(db,'librenms',[n,other],now());db.commit()
        assert db.get(Device,keep).name=='PE-OESTE-ARISTA'
        assert db.get(Device,drop).archived and db.get(Device,drop).merged_into==keep
        assert db.scalar(select(InventoryLink)).left_id==keep
    detail=client.get('/api/v1/devices/'+keep).json()
    assert detail['port_count']==1 and {i['source'] for i in detail['sources']}=={'zabbix','librenms'}
    assert client.get('/api/v1/devices/'+keep+'/ports').json()['items'][0]['id']==port_id
    assert client.get('/api/v1/inventory/links').json()[0]['vlans']==[100,200]


def test_manual_merge_rejects_stale_versions_same_source_and_wrong_pop(client,administrator):
    enroll(client)
    with session() as db:
        ends=physical_fixture(db);db.commit()
    a,b=ends['a'][0],ends['b'][0]
    body={'left_id':a,'right_id':b,'keep_id':a,'left_version':1,'right_version':1}
    url='/api/v1/inventory/reconciliation/manual'
    assert client.post(url,json={**body,'right_id':a}).status_code==422
    assert client.post(url,json={**body,'right_version':99}).status_code==409
    assert client.post(url,json=body).status_code==409
    with session() as db:
        assert not db.scalar(select(Candidate))
        source(db,'zabbix');apply_snapshot(db,'zabbix',[sample('z','alias','10.9.9.9')],now());db.commit()
        c=db.scalar(select(SourceIdentity).where(SourceIdentity.source=='zabbix')).device_id
        for device_id,label in ((a,'POP A'),(c,'POP C')):
            pop=Pop(name=label);db.add(pop);db.flush();db.get(Device,device_id).pop_id=pop.id
        db.commit()
    assert client.post(url,json={**body,'right_id':c}).status_code==409
    with session() as db:
        assert not db.scalar(select(Candidate))
        assert not db.get(Device,a).archived and not db.get(Device,c).archived


def test_candidates_compare_display_names_and_show_sources_ports(client,administrator):
    enroll(client)
    with session() as db:
        source(db,'zabbix');source(db,'librenms')
        a=sample('a','PE-Example','10.0.0.1');a['details']['hostname']='monitor-name'
        b=sample('b','pe-example','10.0.0.2');b['details']['hostname']='10.0.0.2'
        apply_snapshot(db,'zabbix',[a],now());apply_snapshot(db,'librenms',[b],now());db.commit()
    candidate=client.get('/api/v1/inventory/reconciliation').json()[0]
    assert candidate['evidence']==['name: pe-example']
    assert candidate['left']['sources'] and candidate['right']['sources']
    assert candidate['left']['port_count']==1 and candidate['right']['port_count']==1
    response=client.post('/api/v1/inventory/reconciliation/'+candidate['id'],json={
        'action':'merge','keep_id':candidate['right']['id'],'left_version':1,'right_version':1})
    assert response.status_code==200
    assert client.get('/api/v1/devices/'+candidate['right']['id']).json()['name']==candidate['right']['name']


def test_explicit_same_source_merge_preserves_each_external_identity_on_resync(client,administrator):
    enroll(client)
    with session() as db:
        source(db,'zabbix');source(db,'librenms')
        zs=[sample('10691','PE-PUERTO-ARISTA'),sample('10692','PE-PUERTO-ARISTA-PROVA')]
        ns=[sample('3','pe-puerto')]
        for row in zs:row['interfaces'][0]['kind']='management'
        apply_snapshot(db,'zabbix',zs,now());apply_snapshot(db,'librenms',ns,now());db.commit()
        identities={(s.source,s.external_id):s for s in db.scalars(select(SourceIdentity))}
        keep=identities[('librenms','3')].device_id
        first=identities[('zabbix','10692')].device_id
        second=identities[('zabbix','10691')].device_id
    url='/api/v1/inventory/reconciliation/manual'
    body={'left_id':keep,'right_id':first,'keep_id':keep,'left_version':1,'right_version':1}
    assert client.post(url,json=body).status_code==200
    body.update(right_id=second,left_version=2)
    assert client.post(url,json=body).status_code==409
    assert client.post(url,json={**body,'allow_same_source':True}).status_code==200
    with session() as db:
        apply_snapshot(db,'zabbix',zs,now());apply_snapshot(db,'librenms',ns,now());db.commit()
        assert len(list(db.scalars(select(Device).where(Device.archived.is_(False)))))==1
        assert {(s.source,s.external_id) for s in db.scalars(select(SourceIdentity).where(SourceIdentity.device_id==keep))}=={('zabbix','10691'),('zabbix','10692'),('librenms','3')}
    detail=client.get('/api/v1/devices/'+keep).json()
    assert len(detail['sources'])==3 and detail['port_count']==1
