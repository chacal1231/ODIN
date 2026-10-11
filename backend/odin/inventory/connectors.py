"""Bounded, read-only adapters. Provider payloads and credentials are never logged."""
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit, urlunsplit
import httpx

MAX_DEVICES = 5000
MAX_BYTES = 16 * 1024 * 1024


class ConnectorError(Exception):
    def __init__(self, code, detail=''):
        self.code = code
        # Bounded start of a provider error body, for adapters that can classify it; never logged.
        self.detail = detail
        super().__init__(code)


def allowed_address(host, port, networks):
    try:
        permitted = [ipaddress.ip_network(n, strict=False) for n in networks]
        addresses = sorted({x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except (ValueError, OSError):
        raise ConnectorError('dns_or_network_invalid')
    if not addresses or not permitted:
        raise ConnectorError('network_not_allowed')
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ConnectorError('network_not_allowed')
        if not any(ip in net for net in permitted):
            raise ConnectorError('network_not_allowed')
    # Prefer IPv4 on dual-stack hosts: the Compose network is IPv4-only.
    # Validate every DNS answer above before selecting a pinned destination.
    return min(addresses, key=lambda address: (ipaddress.ip_address(address).version, address))


class Transport:
    def __init__(self, endpoint, networks, heartbeat=lambda: None):
        self.endpoint = endpoint.rstrip('/')
        self.networks = networks
        self.heartbeat = heartbeat
        self.deadline = time.monotonic() + 180

    def request(self, method, url, headers=None, payload=None):
        if time.monotonic() >= self.deadline:
            raise ConnectorError('inventory_timeout')
        self.heartbeat()
        parsed = urlsplit(url)
        origin = urlsplit(self.endpoint)
        if (parsed.scheme, parsed.hostname, parsed.port) != (origin.scheme, origin.hostname, origin.port):
            raise ConnectorError('redirect_not_allowed')
        if parsed.scheme not in ('https','http') or parsed.username or parsed.password or parsed.fragment:
            raise ConnectorError('endpoint_invalid')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        ip = allowed_address(parsed.hostname, port, self.networks)
        netloc = (f'[{ip}]' if ':' in ip else ip) + f':{port}'
        pinned = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ''))
        host = parsed.hostname if ':' not in parsed.hostname else f'[{parsed.hostname}]'
        if parsed.port:
            host += f':{parsed.port}'
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                # Pin the validated address while retaining the original TLS hostname.
                with client.stream(method, pinned, headers={**(headers or {}), 'Host':host},
                                   json=payload, extensions={'sni_hostname':parsed.hostname}) as response:
                    if response.status_code in (401,403):
                        raise ConnectorError('access_denied')
                    if 300 <= response.status_code < 400:
                        raise ConnectorError('redirect_not_allowed')
                    if response.status_code != 200:
                        detail = b''
                        for chunk in response.iter_bytes():
                            detail += chunk
                            if len(detail) >= 2048:
                                break
                        raise ConnectorError('http_error', detail[:2048].decode('utf-8', 'replace'))
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data)>MAX_BYTES:
                            raise ConnectorError('response_too_large')
                        if time.monotonic() >= self.deadline:
                            raise ConnectorError('inventory_timeout')
            result=json.loads(data)
            if not isinstance(result,dict):
                raise ConnectorError('invalid_response')
            return result
        except (httpx.HTTPError, OSError):
            raise ConnectorError('connection_failed')
        except (ValueError, TypeError):
            raise ConnectorError('invalid_response')


def clean(value, length=200):
    return str(value or '')[:length]


def address_list(values):
    result=[]
    for value in values:
        if not value:
            continue
        try:
            ip=ipaddress.ip_address(str(value))
            if not ip.is_unspecified and not ip.is_loopback:
                result.append(str(ip))
        except ValueError:
            pass
    return sorted(set(result))


class Zabbix:
    def __init__(self, endpoint, secret, networks, heartbeat=lambda:None):
        self.http=Transport(endpoint, networks, heartbeat)
        self.url=endpoint.rstrip('/')
        if not self.url.endswith('/api_jsonrpc.php'):
            self.url+='/api_jsonrpc.php'
        self.secret=secret

    def rpc(self, method, params, auth=True):
        body={'jsonrpc':'2.0','method':method,'params':params,'id':1}
        if auth:
            body['auth']=self.secret
        data=self.http.request('POST',self.url,payload=body)
        if 'error' in data:
            raise ConnectorError('rpc_access_or_parameters')
        if 'result' not in data:
            raise ConnectorError('invalid_response')
        return data['result']

    def test(self):
        version=self.rpc('apiinfo.version',{},False)
        rows=self.rpc('host.get',{'output':['hostid'],'limit':1})
        if not isinstance(version,str) or not isinstance(rows,list):
            raise ConnectorError('invalid_response')
        return {'version':clean(version,40),'scope':'visible_to_token'}

    def fetch(self):
        rows=self.rpc('host.get',{'output':['hostid'],'sortfield':'hostid','sortorder':'ASC','limit':MAX_DEVICES+1})
        if not isinstance(rows,list) or len(rows)>MAX_DEVICES:
            raise ConnectorError('inventory_limit')
        try:
            ids=[str(x['hostid']) for x in rows]
            if len(set(ids))!=len(ids):
                raise ConnectorError('incomplete_snapshot')
            devices=[]
            for start in range(0,len(ids),200):
                chunk=ids[start:start+200]
                hosts=self.rpc('host.get',{'hostids':chunk,'output':['hostid','host','name','status'],
                    'selectInterfaces':['interfaceid','ip','dns','type','main'],
                    'selectInventory':['location','location_lat','location_lon','vendor','model','serialno_a'],
                    'selectTags':['tag','value']})
                if not isinstance(hosts,list) or {str(x['hostid']) for x in hosts}!=set(chunk):
                    raise ConnectorError('incomplete_snapshot')
                for row in hosts:
                    interfaces=row.get('interfaces',[])
                    inv=row.get('inventory') or {}
                    if not isinstance(inv,dict):
                        inv={}
                    devices.append({'external_id':str(row['hostid']),'name':clean(row.get('name') or row.get('host')),
                        'addresses':address_list(x.get('ip') for x in interfaces),
                        'details':{'hostname':clean(row.get('host')),'location':clean(inv.get('location')),
                            'serial':clean(inv.get('serialno_a')),'vendor':clean(inv.get('vendor')),
                            'model':clean(inv.get('model')),'monitoring_status':clean(row.get('status')),
                            'tags':[{'tag':clean(t.get('tag')),'value':clean(t.get('value'))} for t in row.get('tags',[])[:50]]},
                        'interfaces':[{'external_id':str(x['interfaceid']),'name':clean(x.get('dns') or x.get('ip') or x['interfaceid']),
                            'kind':'management','addresses':address_list([x.get('ip')])} for x in interfaces]})
            final=self.rpc('host.get',{'output':['hostid'],'sortfield':'hostid','sortorder':'ASC','limit':MAX_DEVICES+1})
            if {str(x['hostid']) for x in final}!=set(ids):
                raise ConnectorError('inventory_changed_retry')
            return devices
        except (KeyError, TypeError, AttributeError):
            raise ConnectorError('invalid_response')


def port_networks(data):
    """Addresses with their prefix per port ('10.0.9.1/30'), from LibreNMS /devices/{id}/ip. A device without addresses answers with an error status."""
    result={}
    if data.get('status')!='ok' or not isinstance(data.get('addresses'),list):return result
    for row in data['addresses'][:5000]:
        address=row.get('ipv4_address') or row.get('ipv6_compressed') or row.get('ipv6_address')
        length=row.get('ipv4_prefixlen') or row.get('ipv6_prefixlen')
        try:value=str(ipaddress.ip_interface(f'{address}/{length}'))
        except ValueError:continue
        result.setdefault(str(row.get('port_id')),[]).append(value)
    return result


class LibreNMS:
    def __init__(self, endpoint, secret, networks, heartbeat=lambda:None):
        self.http=Transport(endpoint, networks, heartbeat)
        base=endpoint.rstrip('/')
        self.base=base if base.endswith('/api/v0') else base+'/api/v0'
        self.headers={'X-Auth-Token':secret}

    def rows(self,path,key):
        data=self.http.request('GET',self.base+path,headers=self.headers)
        if data.get('status')!='ok' or not isinstance(data.get(key),list):
            raise ConnectorError('invalid_response')
        rows=data[key]
        if data.get('next') or data.get('next_page_url') or (data.get('links') or {}).get('next'):
            raise ConnectorError('unsupported_pagination')
        if data.get('count') is not None and int(data['count'])!=len(rows):
            raise ConnectorError('incomplete_snapshot')
        return rows

    def test(self):
        rows=self.rows('/devices','devices')
        return {'count':len(rows),'scope':'visible_to_token'}

    def fetch(self):
        try:
            rows=self.rows('/devices','devices')
            if len(rows)>MAX_DEVICES:
                raise ConnectorError('inventory_limit')
            devices=[]
            ids=set()
            for row in rows:
                external=str(row['device_id'])
                if not external.isdigit() or external in ids:
                    raise ConnectorError('invalid_response')
                ids.add(external)
                ports=self.rows('/devices/'+external+'/ports?columns=port_id,ifName,ifDescr,ifAlias','ports')
                if len(ports)>10000:
                    raise ConnectorError('inventory_limit')
                try:
                    networks=port_networks(self.http.request('GET',self.base+'/devices/'+external+'/ip',headers=self.headers))
                except ConnectorError as exc:
                    if exc.code!='http_error':raise
                    networks={}  # LibreNMS answers 404 for a device without addresses
                devices.append({'external_id':external,'name':clean(row.get('sysName') or row.get('hostname') or external),
                    'addresses':address_list([row.get('ip'),row.get('hostname')]),
                    'details':{'hostname':clean(row.get('hostname')),'location':clean(row.get('location')),
                        'serial':clean(row.get('serial')),'model':clean(row.get('hardware')),
                        'os':clean(row.get('os')),'monitoring_status':clean(row.get('status'))},
                    'interfaces':[{'external_id':str(x['port_id']),'name':clean(x.get('ifName') or x.get('ifDescr') or x['port_id']),
                        'kind':'port','addresses':networks.get(str(x['port_id']),[]),'description':clean(x.get('ifAlias'),500)} for x in ports]})
            if {str(x['device_id']) for x in self.rows('/devices','devices')}!=ids:
                raise ConnectorError('inventory_changed_retry')
            return devices
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ConnectorError('invalid_response')


def connector(kind, endpoint, secret, networks, heartbeat=lambda:None, model=None):
    if not secret:
        raise ConnectorError('token_missing')
    if kind=='deepseek':
        from ..analyst.llm import ApiLlm
        return ApiLlm(endpoint,secret,model,heartbeat)
    if kind=='telegram':
        from ..telegram import Telegram
        return Telegram(endpoint,secret,networks,heartbeat)
    if kind=='llama_server':
        from ..analyst.llm import Llm
        return Llm(endpoint,secret,networks,heartbeat)
    if kind=='graylog':
        from ..events.graylog import Graylog
        return Graylog(endpoint,secret,networks,heartbeat)
    if kind not in ('zabbix','librenms'):
        raise ConnectorError('unsupported_source')
    return (Zabbix if kind=='zabbix' else LibreNMS)(endpoint,secret,networks,heartbeat)
