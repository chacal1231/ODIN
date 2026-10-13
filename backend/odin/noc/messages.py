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

CARRIER_ROLES = ('transit', 'transport', 'ixp', 'peering')
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


# What an access case is, by its reading (cases/access.py): the title says the situation, not «Caída de acceso».
ACCESS = {'fiber': 'Corte de fibra', 'fiber_common': 'Corte de fibra común', 'power': 'Falla de energía', 'offline': 'ONUs sin conexión',
          'unreachable': 'OLT inalcanzable', 'uplink': 'Uplink caído'}


def title(case):
    if case.kind == 'access':
        r = case.detail.get('reading') or {}
        place = f' · PON {r["pon"]}' if r.get('pon') else ''
        pop = f' · {case.entity_label}' if case.entity_label and case.entity_label not in ('OLT', case.subject) else ''
        return f'{ACCESS.get(r.get("code"), kind(case))} · {case.subject}{place}{pop}'
    return f'{kind(case)} · {case.subject}' + (f' · {case.entity_label}' if case.entity_label and case.entity_label != case.subject else '')


def access_impact(case):
    """«16 de 20 ONUs del PON (14 % de la OLT) · Zona - El Centro · falla común, no son clientes sueltos»."""
    from ..cases.access import zone_text
    d = case.detail
    r = d.get('reading') or {}
    pons = d.get('pons') or []
    cause = {'fiber': 'los', 'fiber_common': 'los', 'power': 'power'}.get(r.get('code'))
    group = [p for p in pons if not cause or p['cause'] == cause]
    if not group:return (d.get('impact') or {}).get('text')
    affected = sum(p['affected'] for p in group)
    share = f' ({affected / d["registered"]:.0%} de la OLT)'.replace('%', ' %') if d.get('registered') else ''
    if len(group) == 1:
        p = group[0]
        text = f'{p["affected"]} de {p["subscribers"]} ONUs del PON{share} · {zone_text(p["zone"])}'
    else:
        names = sorted({zone_text(p['zone']) for p in group if p['zone']})
        text = f'{affected} ONUs en {len(group)} PON{share} · {", ".join(names) or "zona sin nombre"}'
    if any(p.get('common') for p in group) and r.get('code') in ('fiber', 'power', 'offline'):
        text += ' · falla común, no son clientes sueltos'
    return text


def action_text(action):
    """«Informar X. N2 despacha.» as one field: «informar X; N2 despacha» (acronyms such as N2 or OLT keep their case)."""
    lower = lambda t: t[:1].lower() + t[1:] if t[1:2].islower() else t
    return '; '.join(lower(part.strip()) for part in action.rstrip('.').split('. ') if part.strip())


def access_alert(case):
    r = case.detail.get('reading') or {}
    fields = [('Desde', local(case.opened_at))]
    if access_impact(case):fields.append(('Afectación', access_impact(case)))
    if r.get('action'):fields.append(('Acción', action_text(r['action'])))
    return Message('CRÍTICO' if case.severity == 'critical' else 'ADVERTENCIA', title(case), fields, link=ref(case))


def ref(case):
    return f'{settings().origin}/#caso={case.id}'


def reading(case):
    d = case.detail or {}
    role = d.get('neighbor') or d.get('port') or {}
    return ((d.get('reading') or {}).get('title') or (d.get('verdict') or {}).get('title') or role.get('summary')
            or (f'{role.get("label") or "sin clasificar"} «{role.get("description")}»' if role.get('description') else None))


def case_alert(case, isolation=None):
    if case.kind == 'access':return access_alert(case)
    fields = [('Estado', f'activo desde {local(case.opened_at)}')]
    if reading(case):fields.append(('Lectura', reading(case)))
    if (case.detail.get('impact') or {}).get('text'):fields.append(('Afectación', case.detail['impact']['text']))
    if isolation:fields.append(('Aislamiento', isolation))
    return Message('CRÍTICO' if case.severity == 'critical' else 'ADVERTENCIA', title(case), fields, link=ref(case))


def isolation(db, case):
    """Whether a BGP drop is alone or comes with others (cases/api.py); None for other kinds."""
    if case.kind != 'bgp_session':return None
    from ..cases.api import isolation as alone, related_cases
    from ..cases.entities import Resolver
    return alone(db, case, related_cases(db, case, Resolver(db)))


def carrier(case):
    """The port or neighbor summary of a case about a carrier link (transit, transport, IXP, peering), or None."""
    info = case.detail.get('neighbor') or case.detail.get('port') or {}
    return info if info.get('role') in CARRIER_ROLES else None


def provider_name(info):
    # Transports are named by their whole description: the provider is not always its second word (roles.py).
    name = info.get('description') if info.get('role') == 'transport' else (info.get('provider') or info.get('description'))
    return f'{name or "sin identificar"} ({info.get("label") or info.get("role")})'


def provider(case, info, follow_up):
    """A carrier incident for the providers group; `follow_up` says where the carrier's ticket and news go."""
    fields = [('Proveedor', provider_name(info)),
              ('Estado', f'activo desde {local(case.opened_at)} · {case.drops} caída(s)')]
    fields.append(('Enlace', info['summary']) if info.get('summary') else ('Puerto', f'«{info.get("description")}»'))
    fields.append(('Seguimiento', follow_up))
    return Message('CRÍTICO' if case.severity == 'critical' else 'ADVERTENCIA', title(case), fields, link=ref(case))


def provider_note(case, info, follow_up):
    """The carrier's part in the thread of a case already announced: no link or reading, the case message has them."""
    return Message('PROVEEDOR', title(case), [('Proveedor', provider_name(info)), ('Caídas', str(case.drops)), ('Seguimiento', follow_up)])


def provider_closed(case):
    end = case.closed_at or case.resolved_at or case.last_state_at
    return Message('CERRADO', title(case), [('Duración', span(end - case.opened_at)), ('Caídas', str(case.drops))])


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
