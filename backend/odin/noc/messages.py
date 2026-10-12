"""What ODIN posts to the NOC group, written as a technical report: a status tag, fixed fields, no emoji, no small talk.

A message is built once (tag, title, fields, optional body and link) and rendered per channel: HTML for Telegram,
WhatsApp's own markup for WhatsApp. Example:
    [CRÍTICO] Caída de acceso · OLT-NORTE · POP Centro
    Estado: activo desde 12/10 14:10
    Lectura: Corte de fibra común: 2 PON con LOS a la vez, 50 ONUs (Centro, Norte)
    Afectación: Confirmado: 300 ONUs afectadas de 512 de la OLT (zonas Centro, Norte)
    Ref: https://odin/#caso=...
"""
import html
from dataclasses import dataclass, field
from ..config import settings
from .guides import local, span

KINDS = {'bgp_session': 'Sesión BGP', 'interface': 'Interfaz', 'latency': 'Latencia', 'access': 'Caída de acceso'}
REPORTS = {'inicial': 'INFORME N1', 'actualizacion': 'ACTUALIZACIÓN N1', 'restablecido': 'RESTABLECIDO (N1)', 'escalado': 'ESCALADO A N2'}


@dataclass
class Message:
    tag: str
    title: str
    fields: list = field(default_factory=list)  # (label, value)
    body: str = ''
    link: str = ''


def kind(case):
    return 'Servicio externo' if case.kind == 'latency' and case.detail.get('scope') == 'external' else KINDS.get(case.kind, case.kind)


def title(case):
    return f'{kind(case)} · {case.subject}' + (f' · {case.entity_label}' if case.entity_label and case.entity_label != case.subject else '')


def ref(case):
    return f'{settings().origin}/#caso={case.id}'


def reading(case):
    d = case.detail or {}
    role = d.get('neighbor') or d.get('port') or {}
    return ((d.get('reading') or {}).get('title') or (d.get('verdict') or {}).get('title') or role.get('summary')
            or (f'{role.get("label") or "sin clasificar"} «{role.get("description")}»' if role.get('description') else None))


def case_alert(case, isolation=None):
    fields = [('Estado', f'activo desde {local(case.opened_at)}')]
    if reading(case):fields.append(('Lectura', reading(case)))
    if (case.detail.get('impact') or {}).get('text'):fields.append(('Afectación', case.detail['impact']['text']))
    if isolation:fields.append(('Aislamiento', isolation))
    return Message('CRÍTICO' if case.severity == 'critical' else 'ADVERTENCIA', title(case), fields, link=ref(case))


def external_group(cases):
    verdict = next(((c.detail.get('verdict') or {}).get('title') for c in cases if c.detail.get('verdict')), None)
    fields = [('Servicios', ', '.join(sorted({c.subject for c in cases})))]
    fields.append(('Veredicto', verdict) if verdict else
                  ('Lectura', 'varios servicios a la vez apuntan a la red propia o al origen de las sondas'))
    return Message('CRÍTICO', f'Servicios externos · {len(cases)} degradados a la vez', fields, link=ref(cases[0]))


def diagnosis(case, analysis, buttons=True):
    """`buttons`: the channel rates it in place (Telegram); otherwise the rating is done in ODIN."""
    result = analysis.result or {}
    fields = [('Resumen', result.get('resumen') or '—')]
    if result.get('hipotesis'):
        first = result['hipotesis'][0]
        fields.append((f'Hipótesis principal (confianza {first.get("confianza") or "—"})', first.get('hipotesis') or '—'))
    fields.append(('Valoración', 'con los botones; para consultar el caso, responder a este mensaje' if buttons else 'en ODIN'))
    checks = [f'- {i.get("accion")} — {i.get("comprobacion")}' for i in (result.get('recomendaciones') or [])[:3]]
    return Message('DIAGNÓSTICO ODIN', title(case), fields, body='\n'.join(checks), link='' if buttons else ref(case))


def recovered(case):
    back = case.resolved_at or case.last_state_at
    return Message('RESTABLECIDO', title(case), [('Hora', local(back)), ('Duración', span(back - case.opened_at))])


def dropped_again(case):
    return Message('NUEVA CAÍDA', title(case), [('Hora', local(case.last_state_at)), ('Caídas en el caso', str(case.drops))])


def report(row, case, author):
    return Message(REPORTS[row.kind], title(case), [('Informa', f'{author} · {local(row.created_at)}')], body=row.text, link=ref(case))


def shift(row):
    tags = {'cierre': 'ENTREGA DE TURNO', 'apertura': 'APERTURA DE TURNO'}
    return Message(tags[row.kind], f'{local(row.starts_at)} – {local(row.ends_at)}', body=row.text, link=settings().origin)


def esc(text, limit):
    return html.escape(str(text or '')[:limit], quote=False)


def telegram(message):
    lines = [f'<b>[{esc(message.tag, 40)}]</b> {esc(message.title, 300)}']
    lines += [f'<b>{esc(label, 80)}:</b> {esc(value, 1200)}' for label, value in message.fields]
    if message.body:lines.append(esc(message.body, 3000))
    if message.link:lines.append(f'<a href="{esc(message.link, 300)}">Ref. en ODIN</a>')
    return '\n'.join(lines)


def whatsapp(message):
    # WhatsApp marks bold with *...*: asterisks inside the values would break it, so they are dropped.
    plain = lambda text, limit: str(text or '')[:limit].replace('*', '')
    lines = [f'*[{plain(message.tag, 40)}]* {plain(message.title, 300)}']
    lines += [f'*{plain(label, 80)}:* {plain(value, 1200)}' for label, value in message.fields]
    if message.body:lines.append(plain(message.body, 3000))
    if message.link:lines.append(f'Ref.: {message.link}')
    return '\n'.join(lines)
