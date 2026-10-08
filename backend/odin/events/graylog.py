"""Graylog 6.1 log reader: every message, windowed by Graylog reception time.

Device clocks are not trusted: several PEs send local time labelled as UTC and drift
by minutes. gl2_receive_timestamp is assigned by Graylog in UTC, so windows on it
cannot miss a message whose device timestamp is hours behind.
"""
import base64
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from ..inventory.connectors import Transport, ConnectorError, clean
from .contracts import EventRecord
from .connectors import Batch

PAGE_SIZE = 200
# OpenSearch refuses offset+limit above 10000; larger windows are split instead.
WINDOW_CAP = 5000
MAX_WINDOW = 6 * 3600
MIN_WINDOW = 60
# Reception stamps are assigned before indexing; a minute absorbs the indexing delay.
SETTLE = 60
OVERLAP = 120
FIELDS = 'source,message,gl2_remote_ip,gl2_receive_timestamp,timestamp,level,arista_event'
EVENT_CODE = re.compile(r'%([A-Z0-9_]+)-(\d)-([A-Z0-9_]+):')
BGP_ADJ = re.compile(r'peer (\S+) \(VRF (\S+) AS (\d+)\) old state (\w+) event \w+ new state (\w+)')
LINE_PROTOCOL = re.compile(r'Line protocol on Interface (\S+).*, changed state to (\w+)\s*$')
BFD = re.compile(r'peer \(vrf:([^,]+), ip:([^,]+), intf:([^,]+),[^)]*\) changed state from (\w+) to (\w+)')
CATEGORIES = {'BGP': 'bgp', 'LINEPROTO': 'interface', 'BFD': 'bfd', 'SYS': 'system'}
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
UNREADABLE = 'Contenido no legible: no es un mensaje syslog de texto'


def readable(text):
    """The syslog input also receives non-syslog traffic (e.g. TLS probes); PostgreSQL rejects NUL bytes."""
    cleaned = CONTROL.sub('', text)
    odd = len(text) - len(cleaned) + cleaned.count('\ufffd')
    return UNREADABLE if text and odd > len(text) * 0.1 else cleaned


def stamp_text(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%d %H:%M:%S.000')


def received_at(value):
    try:
        return int(datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        raise ConnectorError('invalid_source_time')


def severity(level):
    if level is None:
        return 'unknown'
    return 'critical' if level <= 2 else 'warning' if level <= 4 else 'info'


def classify(source, text):
    """Status and condition only for formats verified against the installed PEs; the rest stays informational."""
    code = EVENT_CODE.search(text)
    facility, level, mnemonic = (code.group(1), int(code.group(2)), code.group(3)) if code else (None, None, None)
    kind = 'log_' + CATEGORIES.get(facility, 'other')
    if facility == 'SYS' and mnemonic.startswith('CONFIG'):
        kind = 'log_config'
    status, condition, state = 'info', None, ''
    if facility == 'BGP' and mnemonic == 'ADJCHANGE' and (m := BGP_ADJ.search(text)):
        peer, vrf, _, old, new = m.groups()
        condition, state = f'bgp:{source}:{vrf}:{peer}', new
        status = 'recovered' if new == 'Established' else 'open' if old == 'Established' else 'info'
    elif facility == 'LINEPROTO' and mnemonic == 'UPDOWN' and (m := LINE_PROTOCOL.search(text)):
        interface, new = m.groups()
        condition, state = f'interface:{source}:{interface}', new
        status = 'recovered' if new == 'up' else 'open'
    elif facility == 'BFD' and mnemonic == 'STATE_CHANGE' and (m := BFD.search(text)):
        vrf, ip, interface, old, new = m.groups()
        condition, state = f'bfd:{source}:{vrf}:{ip}:{interface}', new
        status = 'recovered' if new == 'Up' else 'open' if old == 'Up' else 'info'
    event = f'{facility}-{level}-{mnemonic}' if code else ''
    return {'kind': kind, 'status': status, 'condition_key': condition[:200] if condition else None,
            'original_state': clean(state, 40), 'level': level, 'event': event}


def batch(events, progress, coverage, reason, senders):
    result = Batch(events, progress, coverage, reason)
    # Latest sender address per source; not part of the stream checkpoint.
    result.senders = senders
    return result


def window(progress, stamp):
    """Next window to read; an unfinished window keeps its bounds so offsets stay valid."""
    if progress.get('offset'):
        return progress
    start = progress['start']
    end = min(start + MAX_WINDOW, stamp - SETTLE, progress.get('bound') or stamp)
    return {'start': start, 'end': max(end, start + 1), 'offset': 0, 'live': end >= stamp - SETTLE}


class Graylog:
    def __init__(self, endpoint, secret, networks, heartbeat=lambda: None):
        self.http = Transport(endpoint, networks, heartbeat)
        base = endpoint.rstrip('/')
        self.base = base if base.endswith('/api') else base + '/api'
        token = base64.b64encode((secret + ':token').encode()).decode()
        self.headers = {'Authorization': 'Basic ' + token, 'Accept': 'application/json', 'X-Requested-By': 'ODIN'}

    def get(self, path, params=None):
        return self.http.request('GET', self.base + path + ('?' + urlencode(params) if params else ''), headers=self.headers)

    def test(self):
        system = self.get('/system')
        self.search(0, 1, 0, 1)
        return {'version': clean(system.get('version'), 40), 'scope': 'visible_to_token'}

    def search(self, start, end, offset, limit, query=None):
        query = query or f'gl2_receive_timestamp:["{stamp_text(start)}" TO "{stamp_text(end)}"}}'
        data = self.get('/search/universal/relative', {'query': query, 'range': 0, 'limit': limit, 'offset': offset,
            'sort': 'gl2_receive_timestamp:asc', 'fields': FIELDS, 'decorate': 'false'})
        total, messages = data.get('total_results'), data.get('messages')
        if not isinstance(total, int) or not isinstance(messages, list) or len(messages) > limit:
            raise ConnectorError('invalid_response')
        return total, messages

    def read(self, progress):
        if progress.get('bootstrap'):
            # Start at the oldest message Graylog still retains.
            _, rows = self.search(0, 0, 0, 1, '*')
            oldest = received_at(rows[0].get('message', {}).get('gl2_receive_timestamp')) if rows else progress['start']
            return batch([], {'start': oldest, 'offset': 0}, 'partial', 'bootstrap', {})
        start, end, offset = progress['start'], progress['end'], progress['offset']
        total, rows = self.search(start, end, offset, PAGE_SIZE)
        if offset == 0 and total > WINDOW_CAP and end - start > MIN_WINDOW:
            # Too dense to page within the search limit: halve the window, ingest nothing yet.
            half = start + max(MIN_WINDOW, (end - start) // 2)
            return batch([], {'start': start, 'bound': half, 'offset': 0}, 'partial', 'window_split', {})
        events, senders = [], {}
        for row in rows:
            message, index = row.get('message'), row.get('index')
            if not isinstance(message, dict) or not isinstance(index, str):
                raise ConnectorError('invalid_response')
            key, source = message.get('_id'), readable(clean(message.get('source'), 200))
            text = readable(str(message.get('message') or ''))
            if not key or not source:
                raise ConnectorError('invalid_response')
            stamp = received_at(message.get('gl2_receive_timestamp'))
            if not start <= stamp < end:
                raise ConnectorError('invalid_pagination')
            info = classify(source, text)
            level = info['level'] if info['level'] is not None else message.get('level')
            level = level if isinstance(level, int) and 0 <= level <= 7 else None
            events.append(EventRecord(external_id=str(key)[:200], entity_id=source, occurred_at=stamp, source_updated_at=stamp,
                kind=info['kind'], status=info['status'], severity=severity(level),
                original_severity=str(level) if level is not None else '', original_state=info['original_state'],
                title=clean(text, 500) or 'Log sin texto', condition_key=info['condition_key'],
                reference=clean(f'graylog:{index}:{key}', 300)))
            address = clean(message.get('gl2_remote_ip'), 45)
            if address:
                senders[source] = {'address': address, 'last_seen': stamp}
        offset += len(rows)
        capped = offset >= WINDOW_CAP
        if offset < total and not capped and len(rows) == PAGE_SIZE:
            return batch(events, {**progress, 'offset': offset}, 'partial', 'pagination', senders)
        # Window done. Only the live window can still receive late-indexed messages: it overlaps the
        # next one and waits for the next cycle. Settled history continues immediately, without overlap,
        # so a truncated or minimal window can never be read again.
        live = bool(progress.get('live'))
        following = {'start': max(start, end - OVERLAP) if live else end, 'offset': 0, 'caught_up': live}
        if capped and offset < total:
            return batch(events, following, 'partial', 'truncated', senders)
        return batch(events, following, 'complete', None, senders)
