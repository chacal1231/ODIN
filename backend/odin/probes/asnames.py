"""Names of autonomous systems, and the organization each belongs to, for the people reading paths and verdicts.

Names come from Team Cymru over DNS (AS<n>.asn.cymru.com, the source mtr already uses for hop origins) and are kept
in `autonomous_systems`; the tracer asks for the ones its traces meet. An organization joins the systems of one company
(EdgeUno 7195 and 273993, Amazon 16509 and 14618): a failure there is one exit, not two. The key is the first
significant word of the registered organization, so unrelated companies sharing it would join; NAMES fixes the
display of well-known ones.
"""
import logging
import random
import re
import socket
import struct
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from ..models import AutonomousSystem, now

REFRESH = 30 * 86400
RETRY = 86400          # a failed lookup is retried after a day
PER_TRACE = 10         # lookups per trace, so a long path never delays the tracer
TIMEOUT = 2
# Display names for well-known networks; registered names are uppercase handles.
NAMES = {7195: 'EdgeUno', 273993: 'EdgeUno', 174: 'Cogent', 3356: 'Lumen', 3549: 'Lumen', 1299: 'Arelion', 6939: 'Hurricane Electric',
         3257: 'GTT', 6762: 'Sparkle', 12956: 'Telxius', 23520: 'Columbus Networks', 52320: 'GlobeNet', 6461: 'Zayo',
         15169: 'Google', 13335: 'Cloudflare', 209242: 'Cloudflare', 32934: 'Meta', 20940: 'Akamai', 16625: 'Akamai',
         16509: 'Amazon', 14618: 'Amazon', 8075: 'Microsoft', 6507: 'Riot', 32590: 'Valve', 2906: 'Netflix', 40027: 'Netflix',
         19551: 'Imperva', 54113: 'Fastly', 36692: 'OpenDNS'}
LEGAL = {'S', 'A', 'SA', 'SAS', 'SAC', 'INC', 'LLC', 'LTD', 'LTDA', 'CORP', 'CORPORATION', 'CO', 'COM', 'COMPANY', 'LIMITED', 'GMBH',
         'BV', 'SRL', 'SPA', 'DE', 'CV', 'PARENT', 'PLC', 'AG', 'NV', 'FKA', 'THE'}


def organization(name):
    """(key, display) from a registered name such as 'COGENT-174 - Cogent Communications, LLC, US'."""
    text = re.sub(r',\s*[A-Z]{2}$', '', name.strip())
    if ' - ' in text:text = text.split(' - ', 1)[1]
    else:
        words = text.split()
        # 'CLOUDFLARESPECTRUM Cloudflare, Inc.' and 'TWELVE99 Arelion': a handle, then the organization.
        if len(words) > 1 and (re.search(r'[\d-]', words[0]) or any(w != w.upper() for w in words[1:])):text = ' '.join(words[1:])
    tokens = [t for t in re.sub(r'[^A-Z0-9 ]', ' ', text.upper().replace('&', '')).split() if t not in LEGAL]
    key = next((t for t in tokens if len(t) >= 3), tokens[0] if tokens else '')
    display = ' '.join(w for w in re.split(r'[\s,]+', text) if w.upper().replace('.', '') not in LEGAL and w.strip('.'))
    return key[:80], (display.title() if display.isupper() else display)[:120]


def label(asn, row=None):
    if asn in NAMES:return NAMES[asn]
    return row.display if row and row.display else 'AS%d' % asn


def lookup(db, asns):
    """{asn: {'org': key, 'label': name}} for the systems given; unknown ones are their own organization."""
    asns = {a for a in asns if a}
    rows = {r.asn: r for r in db.scalars(select(AutonomousSystem).where(AutonomousSystem.asn.in_(asns)))} if asns else {}
    result = {}
    for asn in asns:
        row = rows.get(asn)
        # The registry decides the organization; until it answers, a well-known name stands in for it.
        org = 'org:' + row.org if row and row.org else 'name:' + NAMES[asn] if asn in NAMES else 'as:%d' % asn
        result[asn] = {'org': org, 'label': label(asn, row)}
    return result


def _name(packet, offset):
    while True:
        length = packet[offset]
        if length == 0:return offset + 1
        if length & 0xC0 == 0xC0:return offset + 2
        offset += length + 1


def txt(query, server=None, timeout=TIMEOUT, port=53):
    """First TXT string of a DNS answer, over UDP to the system resolver; None when there is none."""
    server = server or next((line.split()[1] for line in open('/etc/resolv.conf') if line.startswith('nameserver')), '127.0.0.53')
    ident = random.randint(0, 0xFFFF)
    packet = struct.pack('>HHHHHH', ident, 0x0100, 1, 0, 0, 0)
    packet += b''.join(bytes([len(p)]) + p.encode() for p in query.split('.')) + b'\0' + struct.pack('>HH', 16, 1)
    with socket.socket(socket.AF_INET6 if ':' in server else socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout);sock.sendto(packet, (server, port))
        answer, _ = sock.recvfrom(4096)
    got, flags, questions, answers = struct.unpack('>HHHH', answer[:8])
    if got != ident or flags & 0x000F:return None
    offset = 12
    for _ in range(questions):offset = _name(answer, offset) + 4
    for _ in range(answers):
        offset = _name(answer, offset)
        kind, _, _, length = struct.unpack('>HHIH', answer[offset:offset + 10]);offset += 10
        if kind == 16:return answer[offset + 1:offset + 1 + answer[offset]].decode('utf-8', 'replace')
        offset += length
    return None


def refresh(db, asns, stamp=None):
    """Looks up the systems without a recent name; returns how many were stored."""
    stamp = stamp or now();asns = {a for a in asns if a}
    if not asns:return 0
    rows = {r.asn: r for r in db.scalars(select(AutonomousSystem).where(AutonomousSystem.asn.in_(asns)))}
    due = sorted(a for a in asns if a not in rows or rows[a].resolved_at < stamp - (REFRESH if rows[a].name else RETRY))[:PER_TRACE]
    for asn in due:
        try:value = txt('AS%d.asn.cymru.com' % asn)
        except (OSError, ValueError, struct.error, IndexError):
            logging.warning('asn_name_lookup_failed');value = None
        name = value.split(' | ')[-1].strip() if value and ' | ' in value else None
        org, display = organization(name) if name else (None, None)
        country = value.split(' | ')[1].strip()[:4] if name else None
        db.execute(insert(AutonomousSystem).values(asn=asn, name=name, org=org, display=display, country=country, resolved_at=stamp)
                   .on_conflict_do_update(index_elements=['asn'], set_=dict(name=name, org=org, display=display, country=country, resolved_at=stamp)))
    return len(due)


def route(hops, names, reached=True):
    """'TV&MAS → EdgeUno → Netflix' from the answered hops: one name per network crossed, in order."""
    steps = []
    for hop in hops:
        asn = hop.get('asn')
        if hop.get('address') and asn:
            name = names.get(asn, {}).get('label') or 'AS%d' % asn
            if not steps or steps[-1] != name:steps.append(name)
    return ' → '.join(steps) + ('' if reached else ' (no llegó al destino)') if steps else 'sin saltos con red identificada'
