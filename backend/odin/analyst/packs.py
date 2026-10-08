"""Evidence packs: what the model reads, grouped, annotated and fitted to a token budget.

Repeated evidence becomes one line with its count and first/last time, so a case with hundreds
of flaps stays readable. Every line carries a short label (E1, N1, R1, ...) mapped to the rows it
stands for; the model may only cite those labels. Deterministic readings of known log patterns
(who sent a BGP NOTIFICATION, what a configuration log means) are added as ODIN's notes, and
groups close to the first drop or the final recovery say so. Nothing is cut silently: groups that
do not fit are listed as omitted.
"""
import re
import datetime as dt
from dataclasses import dataclass, field
from sqlalchemy import select
from ..models import (Case, CaseEvidence, CaseNote, EventCollector, Integration, User, now)

SOURCES = {'graylog': 'logs', 'zabbix': 'Zabbix', 'librenms': 'LibreNMS', 'probe': 'latencia'}
ROLES = {'trigger': 'cambio de estado', 'context': 'contexto', 'impact': 'impacto'}
KINDS = {'bgp_session': 'sesión BGP', 'interface': 'interfaz', 'latency': 'latencia hacia'}
NEAR = 300
TITLE = 400


def stamp_text(stamp):
    return dt.datetime.fromtimestamp(stamp, dt.UTC).strftime('%Y-%m-%d %H:%M:%S UTC') if stamp else '—'


def span_text(seconds):
    seconds = abs(int(seconds))
    if seconds < 120:return f'{seconds} s'
    if seconds < 7200:return f'{seconds // 60} min'
    return f'{seconds / 3600:.1f} h'.replace('.', ',')


def normalize(title):
    """Same message with other numbers (sequence, repeat counts, byte counts) groups together."""
    return re.sub(r'\(message repeated[^)]*\)|\b\d+\b', '#', title)


def reading(title):
    """ODIN's deterministic reading of known messages; the model gets it as a note, not as a fact of the source."""
    notes = []
    lower = title.lower()
    if 'notification' in lower and 'received from' in lower:
        notes.append('aviso BGP enviado por el vecino a este router: fue el vecino quien cerró la sesión')
    elif 'notification' in lower and 'sent to' in lower:
        notes.append('aviso BGP enviado por este router al vecino: fue este router quien cerró la sesión')
    if 'maximum number of prefixes' in lower:
        if 'received from' in lower:
            notes.append('el vecino limita los prefijos que recibe y este router le anunció más de los permitidos')
        elif 'sent to' in lower:
            notes.append('este router limita los prefijos que recibe y el vecino le anunció más de los permitidos')
    if 'recvhardreset' in lower or 'recvnotification' in lower:
        notes.append('caída por un aviso recibido del vecino')
    if 'holdtimerexpired' in lower or 'hold timer expired' in lower:
        notes.append('no llegaron mensajes del vecino dentro del hold time')
    if 'transporterror' in lower:
        notes.append('falló la conexión TCP de la sesión')
    if 'config_startup' in lower or 'startup config saved' in lower:
        notes.append('se guardó la configuración en ejecución: indica que hubo cambios aplicados antes de este momento')
    elif 'config_i' in lower or 'configured from' in lower:
        notes.append('cambio de configuración')
    return notes


@dataclass
class Line:
    label: str
    first: int
    priority: int
    text: str
    ref: dict


@dataclass
class Pack:
    header: str
    lines: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    omitted: list = field(default_factory=list)
    version: str = ''

    def text(self, lines=None):
        lines = self.lines if lines is None else lines
        parts = [self.header, 'Cobertura de fuentes en la ventana:'] + ['- ' + c for c in self.coverage]
        parts.append('Evidencias (cada línea empieza por su identificador):')
        parts += [l.text for l in sorted(lines, key=lambda l: (l.first, l.label))]
        if self.omitted:
            parts.append('Evidencias que existen pero no se incluyen por espacio: ' + '; '.join(self.omitted) + '.')
        return '\n'.join(parts)

    def refs(self, lines=None):
        return {l.label: l.ref for l in (self.lines if lines is None else lines)}


@dataclass
class Frozen:
    """A context already sent in this conversation, sent again verbatim.

    The server keeps the prompt it last read: an identical beginning is not read again, so a
    follow-up only costs the earlier answer and the new question.
    """
    rendered: str
    labels: dict
    coverage: list
    omitted: list
    version: str
    built_at: int

    def text(self, lines=None):
        return self.rendered

    def refs(self, lines=None):
        return self.labels


# Readers trail the live edge by design: events by up to a few minutes.
EVENT_LAG = 300


def coverage(db, start, end):
    """Whether each source was read through the end of the window; an unread source is not a quiet one."""
    result = []
    integrations = {i.kind: i for i in db.scalars(select(Integration))}
    for source, name in (('graylog', 'Logs (Graylog)'), ('zabbix', 'Zabbix'), ('librenms', 'LibreNMS')):
        integration = integrations.get(source)
        if not integration or not integration.events_enabled:
            result.append(f'{name}: no se lee');continue
        rows = list(db.scalars(select(EventCollector).where(EventCollector.source == source)))
        last = min((r.last_success_at or 0 for r in rows), default=0)
        result.append(f'{name}: leído hasta {stamp_text(last)}' + ('' if last >= end - EVENT_LAG else ' (no cubre todo el período)'))
    return result


def group_lines(evidence, moments, prefix='E', start_index=1):
    """One line per (source, role, reason, message pattern), labelled in order of first occurrence."""
    groups = {}
    for e in sorted(evidence, key=lambda e: (e.occurred_at, e.id)):
        groups.setdefault((e.source, e.role, e.reason, normalize(e.title)), []).append(e)
    lines = []
    for index, ((source, role, reason, _), items) in enumerate(sorted(groups.items(), key=lambda g: g[1][0].occurred_at), start_index):
        first, last = items[0], items[-1]
        label = f'{prefix}{index}'
        when = (f'{stamp_text(first.occurred_at)}' if len(items) == 1 else
                f'{len(items)} veces entre {stamp_text(first.occurred_at)} y {stamp_text(last.occurred_at)}')
        notes = reading(first.title)
        for name, moment in moments:
            closest = min(items, key=lambda e: abs(e.occurred_at - moment))
            gap = closest.occurred_at - moment
            if abs(gap) <= NEAR:
                notes.append(f'{span_text(gap)} {"después" if gap >= 0 else "antes"} de {name}' if gap else f'en el mismo segundo que {name}')
        text = f'{label} · {when} · {SOURCES.get(source, source)}, {ROLES.get(role, role)} ({reason}): «{first.title[:TITLE]}»'
        if notes:text += ' [Lectura de ODIN: ' + '; '.join(notes) + ']'
        # Session/interface state first, then evidence about the same subject, impact, and device-wide context.
        priority = 0 if role == 'trigger' else 1 if first.strength == 'entity' else 2 if role == 'impact' else 3
        lines.append(Line(label, first.occurred_at, priority, text,
                          {'kind': 'evidence', 'source': source, 'ids': [e.id for e in items][:500], 'count': len(items)}))
    return lines


def neighbor_line(db, case, related):
    """What the neighbor or the interface is, from the router port descriptions (kept on the case by the correlator)."""
    from ..cases.api import isolation
    if case.kind == 'interface':
        port = case.detail.get('port')
        return f'Interfaz según el inventario: {port["label"] or "sin clasificar"}, descripción «{port["description"]}».' if port else ''
    if case.kind != 'bgp_session':return ''
    found = case.detail.get('neighbor')
    if not found:
        return 'Vecino según el inventario: ninguna interfaz conocida tiene una subred que lo contenga (puede ser multihop o de un equipo sin IP en LibreNMS).'
    alone = isolation(db, case, related)
    return f'Vecino según el inventario: {found["summary"]}.' + (f' {alone}' if alone else '')


def case_pack(db, case):
    from ..cases.correlator import window, case_entity, LATENCY
    from ..cases.entities import Resolver
    from ..cases.api import related_cases
    start, end = window(case)
    resolver = Resolver(db)
    entity = case_entity(resolver, case)
    related = related_cases(db, case, resolver)[:20]
    evidence = list(db.scalars(select(CaseEvidence).where(CaseEvidence.case_id == case.id)))
    final = case.last_state_at if case.last_state == 'up' else None
    latency = case.kind == LATENCY
    moments = ([('el inicio de la degradación', case.opened_at)] + ([('la normalización', final)] if final else []) if latency else
               [('la primera caída', case.opened_at)] + ([('la recuperación final', final)] if final else []))
    lines = group_lines(evidence, moments)
    notes = db.execute(select(CaseNote, User.username).join(User, User.id == CaseNote.author_id, isouter=True)
                       .where(CaseNote.case_id == case.id).order_by(CaseNote.created_at)).all()
    for index, (note, author) in enumerate(notes, 1):
        lines.append(Line(f'N{index}', note.created_at, 1, f'N{index} · {stamp_text(note.created_at)} · nota del NOC ({author or "usuario"}): «{note.text[:600]}»',
                          {'kind': 'note', 'ids': [note.id], 'count': 1}))
    for index, other in enumerate(related, 1):
        lines.append(Line(f'R{index}', other['opened_at'], 2,
            f'R{index} · caso relacionado ({other["relation"]}): {KINDS.get(other["kind"], other["kind"])} {other["subject"]} en {other["entity_label"]}, '
            f'{other["drops"]} caídas, desde {stamp_text(other["opened_at"])} hasta {stamp_text(other.get("closed_at") or other["last_activity_at"])}',
            {'kind': 'case', 'ids': [other['id']], 'count': 1}))
    identity = resolver.explain(entity) or 'solo el nombre de los logs'
    from ..cases.correlator import CONTEXT_CAP
    totals = case.detail.get('context_total') or {}
    capped = [f'{n} logs {"del mismo sujeto" if k == "entity" else "del mismo equipo"}' for k, n in totals.items() if n > CONTEXT_CAP]
    external = latency and case.detail.get('scope') == 'external'
    subject = (f'Caso: degradación de latencia o pérdida hacia el servicio externo {case.subject} (todas sus regiones), medida desde el servidor ODIN. '
               'No comparte equipo con la red propia: los casos relacionados solo coinciden en el tiempo.' if external else
               f'Caso: degradación de latencia o pérdida hacia el destino {case.subject} del equipo {case.entity_label}, medida desde el servidor ODIN. '
               'Las líneas de latencia son el síntoma; logs, alertas y casos relacionados del mismo equipo o POP son posibles causas.' if latency else
               f'Caso: {KINDS.get(case.kind, case.kind)} {case.subject} en el equipo {case.entity_label} (los logs son de este equipo).')
    header = '\n'.join(filter(None, [
        subject,
        '' if external else f'Identidad del equipo: {identity}' + ('' if entity.device_id else ' (sin vínculo confirmado con el inventario)') + '.',
        f'VRF: {case.detail["vrf"]}.' if case.detail.get('vrf') else '',
        neighbor_line(db, case, related),
        f'Período: desde {stamp_text(case.opened_at)} hasta {stamp_text(case.closed_at) if case.closed_at else "ahora (en curso)"}; '
        f'ventana de evidencias {stamp_text(start)} — {stamp_text(end)}.',
        (f'Logs de contexto: hay {" y ".join(capped)}; se guardaron los {CONTEXT_CAP // 2} primeros y los {CONTEXT_CAP // 2} últimos de cada tipo, '
         'así que los recuentos de esas líneas son parciales.') if capped else '',
        (f'Estado del caso: {case.state}; severidad {case.severity}; {case.drops} incidentes de latencia, {case.recoveries} cerrados; '
         f'último estado: {"degradado" if case.last_state == "down" else "normal"} desde {stamp_text(case.last_state_at)}.') if latency else
        (f'Estado del caso: {case.state}; severidad {case.severity}; {case.drops} caídas y {case.recoveries} recuperaciones; '
         f'último estado: {"caído" if case.last_state == "down" else "restablecido"} desde {stamp_text(case.last_state_at)}.'),
    ]))
    from .memory import case_memory, knowledge_lines
    lines += case_memory(db, case)
    lines += knowledge_lines(db, case.kind, header + '\n' + '\n'.join(l.text for l in lines))
    pack = Pack(header, lines, coverage(db, start, end))
    pack.version = case_version(db, case)
    return pack


def case_version(db, case):
    """Changes when the case gains activity, evidence or notes: an older analysis is then marked as outdated."""
    from sqlalchemy import func
    evidence = db.scalar(select(func.count()).select_from(CaseEvidence).where(CaseEvidence.case_id == case.id))
    notes = db.scalar(select(func.count()).select_from(CaseNote).where(CaseNote.case_id == case.id))
    return f'{case.last_activity_at}:{evidence}:{notes}'


def fit(pack, count, budget):
    """Drop the least important groups until the rendered pack fits; record what was left out."""
    lines = list(pack.lines);dropped = []
    tokens = count(pack.text(lines))
    for _ in range(6):
        if tokens <= budget:break
        # Estimate per-line tokens from characters, then confirm with the exact count.
        ratio = tokens / max(1, len(pack.text(lines)))
        excess = tokens - budget
        ordered = sorted(lines, key=lambda l: (-l.priority, -l.first))
        removed = []
        for line in ordered:
            if line.priority == 0 and any(l.priority > 0 for l in lines if l not in removed):continue
            removed.append(line);excess -= len(line.text) * ratio
            if excess <= 0:break
        lines = [l for l in lines if l not in removed]
        dropped.extend(removed)
        totals = {}
        for line in dropped:
            name = SOURCES.get(line.ref.get('source'), {'note': 'notas', 'case': 'casos relacionados'}.get(line.ref['kind'], line.ref['kind']))
            totals[name] = totals.get(name, 0) + line.ref['count']
        pack.omitted = [f'{count} de {name}' for name, count in totals.items()]
        tokens = count(pack.text(lines))
    if tokens > budget:raise ValueError('pack_too_large')
    pack.lines = lines
    return tokens
