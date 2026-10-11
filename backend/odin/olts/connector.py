"""SmartOLT (cloud) by its API, read-only by construction.

Only the GET endpoints in READS can be called and the client has no other method: ODIN cannot reboot, disable,
move or delete an ONU, nor change an OLT, even by mistake.
Budget (SmartOLT, per account): 1,000 calls per hour and 15 per second; the full ONU export has its own limit, so
ONUs are read in pages. The X-RateLimit headers are kept, and a 429 stops every read until its Retry-After.
Customer data: ONUs are asked for an explicit list of fields and only those are kept. Address, contact, PPPoE and
management credentials and addresses never reach ODIN.
HTTPS to public addresses of <subdomain>.smartolt.com only; the address is pinned and redirects are refused.
"""
import json
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
import httpx
from ..inventory.connectors import ConnectorError, public_address

READS = {'/api/system/get_olts', '/api/olt/get_olts_uptime_and_env_temperature', '/api/system/get_olt_pon_ports_details/{}',
         '/api/system/get_olt_uplink_ports_details/{}', '/api/system/get_outage_pons/{}', '/api/onu/get_all_onus_details'}
ONU_FIELDS = ('unique_external_id', 'sn', 'olt_id', 'board', 'port', 'onu', 'onu_type_name', 'zone_name', 'odb_name', 'name',
              'administrative_status', 'status', 'last_status_change', 'signal', 'signal_1310', 'signal_1490')
MAX_BYTES = 16 * 1024 * 1024
# Well under 15 calls per second, the limit that blocks the whole account for 10 seconds.
SPACING = 0.25


class RateLimited(ConnectorError):
    def __init__(self, retry_after):
        super().__init__('rate_limited')
        self.retry_after = retry_after


def endpoint_valid(endpoint):
    parsed = urlsplit(endpoint)
    return (parsed.scheme == 'https' and bool(parsed.hostname) and parsed.hostname.endswith('.smartolt.com') and not parsed.port
            and not parsed.username and not parsed.password and parsed.path in ('', '/'))


class SmartOLT:
    def __init__(self, endpoint, secret, heartbeat=lambda: None):
        if not secret:raise ConnectorError('token_missing')
        if not endpoint_valid(endpoint):raise ConnectorError('endpoint_invalid')
        self.host, self.secret, self.heartbeat = urlsplit(endpoint).hostname, secret, heartbeat
        self.calls, self.remaining, self.limit, self.reset, self.last = 0, None, None, None, 0.0

    def get(self, template, *args, **params):
        if template not in READS:raise ConnectorError('endpoint_not_allowed')
        wait = SPACING - (time.monotonic() - self.last)
        if wait > 0:time.sleep(wait)
        self.heartbeat()
        path = template.format(*[quote(str(a), safe='') for a in args])
        ip = public_address(self.host, 443)
        netloc = (f'[{ip}]' if ':' in ip else ip) + ':443'
        target = urlunsplit(('https', netloc, path, urlencode(params), ''))
        try:
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                with client.stream('GET', target, headers={'Host': self.host, 'X-Token': self.secret, 'Accept': 'application/json'},
                                   extensions={'sni_hostname': self.host}) as response:
                    self.calls += 1;self.last = time.monotonic()
                    for header, attribute in (('x-ratelimit-remaining', 'remaining'), ('x-ratelimit-limit', 'limit'), ('x-ratelimit-reset', 'reset')):
                        value = response.headers.get(header, '')
                        if value.isdigit():setattr(self, attribute, int(value))
                    if response.status_code == 429:
                        after = response.headers.get('retry-after', '')
                        raise RateLimited(min(int(after), 3600) if after.isdigit() else 60)
                    if response.status_code in (401, 403):raise ConnectorError('access_denied')
                    if 300 <= response.status_code < 400:raise ConnectorError('redirect_not_allowed')
                    if response.status_code >= 500:raise ConnectorError('server_unavailable')
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:raise ConnectorError('response_too_large')
        except ConnectorError:
            raise
        except httpx.TimeoutException:
            raise ConnectorError('timeout')
        except (httpx.HTTPError, OSError):
            raise ConnectorError('connection_failed')
        try:data = json.loads(body)
        except ValueError:raise ConnectorError('invalid_response')
        if not isinstance(data, dict):raise ConnectorError('invalid_response')
        if response.status_code != 200 or data.get('status') is not True:
            raise ConnectorError('smartolt_error', str(data.get('error_code') or data.get('error') or response.status_code)[:200])
        return data

    def test(self):
        olts = self.get('/api/system/get_olts').get('response')
        if not isinstance(olts, list):raise ConnectorError('invalid_response')
        return {'olts': len(olts)}

    def onus(self, max_pages=100):
        """Every ONU, in pages of 100 (paged reads do not spend the full-export budget), with technical fields only."""
        result, page = [], 1
        while True:
            data = self.get('/api/onu/get_all_onus_details', page=page, page_size=100, fields=','.join(ONU_FIELDS))
            items = data.get('onus')
            if not isinstance(items, list):raise ConnectorError('invalid_response')
            result += [{k: item.get(k) for k in ONU_FIELDS} for item in items if isinstance(item, dict)]
            try:pages = int(data.get('total_pages') or 1)
            except (TypeError, ValueError):raise ConnectorError('invalid_response')
            if page >= pages or not items:return result
            if page >= max_pages:raise ConnectorError('too_many_onus')
            page += 1
