"""Guides for level 1, by situation rather than by technical kind of case: what to check, what to report and when to hand
the case to level 2.

The situation comes from what ODIN already reads: the access reading of an OLT (cases/access.py), the verdict of external
services (probes/verdict.py) and the kind of a case of the own network. Each guide has 2 or 3 checks, a template for the
report to the NOC group, and the criterion (and minutes) to escalate to level 2. These are the built-in guides; an
administrator replaces any of them for the ISP (`noc_guides`), and removing that version restores the built-in one.
Templates take {lugar}, {lectura}, {afectacion}, {zonas}, {inicio}, {duracion} and {servicio}, filled from the case.
"""
import datetime as dt
from zoneinfo import ZoneInfo
from sqlalchemy import select
from ..config import settings
from ..models import NocGuide

KINDS = ('inicial', 'actualizacion', 'restablecido', 'escalado')


def guide(title, verify, template, escalate, minutes=None):
    return {'title': title, 'verify': verify, 'template': template, 'escalate': escalate, 'escalate_minutes': minutes}


DEFAULTS = {
    'access_unreachable': guide('OLT sin gestión', [
        'Confirmar en SmartOLT si la OLT figura inalcanzable y desde qué hora.',
        'Revisar si otros equipos del mismo POP también tienen casos abiertos.'],
        'OLT sin gestión: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar de inmediato: puede ser energía o uplink del POP.', 0),
    'access_uplink': guide('Uplink de OLT caído', [
        'Confirmar en ODIN qué uplink figura caído y si la OLT tiene otro activo.',
        'Revisar si la interfaz del router hacia la OLT tiene un caso abierto.'],
        'Uplink de OLT caído: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar de inmediato: N2 revisa el enlace hacia el POP.', 0),
    'access_fiber_common': guide('Corte de fibra común a varios PON', [
        'Confirmar que los PON afectados comparten zona o tramo.',
        'Revisar si la tarjeta de la OLT muestra alarmas en SmartOLT.'],
        'Corte de fibra común: {lugar}. {lectura}. Afectación: {afectacion}. Zonas: {zonas}. Inicio: {inicio}.',
        'Escalar de inmediato para despacho a campo.', 0),
    'access_fiber': guide('Corte de fibra en un PON', [
        'Confirmar en SmartOLT el PON, la zona y los splitters afectados.',
        'Revisar si otros PON de la misma zona empiezan a caer.'],
        'Corte de fibra: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar para despacho a campo si sigue caído.', 15),
    'access_power': guide('Falla de energía en la zona', [
        'Confirmar que las ONUs reportan pérdida de energía (dying gasp), no pérdida de señal.',
        'Verificar si la empresa de energía reporta cortes en la zona.'],
        'Falla eléctrica en la zona: {lugar}. {lectura}. No es una falla de la red. Inicio: {inicio}.',
        'No requiere N2 mientras la causa sea energía; escalar si pasa a pérdida de señal (LOS).', None),
    'access_offline': guide('ONUs sin conexión', [
        'Revisar en SmartOLT el estado de las ONUs afectadas.',
        'Verificar si la cantidad de ONUs sin conexión sigue creciendo.'],
        'ONUs sin conexión: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar si sigue o crece.', 30),
    'external_own': guide('Caída propia (varios servicios por varias salidas)', [
        'Confirmar en Servicios externos que la degradación cruza varias salidas.',
        'Revisar la salud del sondeador en la barra de estado de ODIN.'],
        'Degradación general de servicios externos: {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar de inmediato a N2 de red.', 0),
    'external_exit': guide('Falla en una salida a internet', [
        'Confirmar en el veredicto qué salida concentra los servicios degradados.',
        'Revisar si hay casos de BGP o interfaz hacia ese proveedor.'],
        'Falla en salida a internet: {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar a N2 para gestión con el proveedor.', 0),
    'external_service': guide('Servicio externo con falla propia', [
        'Confirmar que otros servicios por la misma salida están normales.',
        'Revisar si el proveedor del servicio publica una incidencia.'],
        'Falla propia de {servicio}: {lectura}. La red opera normal. Inicio: {inicio}.',
        'No requiere N2: la falla es del servicio.', None),
    'external_undetermined': guide('Servicio externo degradado sin veredicto', [
        'Revisar si se suman otros servicios degradados.',
        'Esperar la siguiente traza para conocer la salida.'],
        '{servicio} degradado: {lectura}. Inicio: {inicio}. Sin veredicto todavía.',
        'Escalar si se suman servicios o si sigue sin veredicto.', 30),
    'network_interface': guide('Interfaz o enlace propio caído', [
        'Confirmar en el caso qué es la interfaz (tránsito, transporte, enlace interno).',
        'Revisar los casos relacionados del mismo equipo o POP.'],
        'Interfaz caída: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar si es tránsito, transporte o enlace interno, o si sigue caída.', 10),
    'network_bgp': guide('Sesión BGP caída', [
        'Confirmar en el caso quién es el vecino y si hay otras sesiones caídas.',
        'Revisar si la interfaz hacia el vecino tiene un caso abierto.'],
        'Sesión BGP caída: {lugar}. {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar si es tránsito o enlace interno, o si sigue caída.', 10),
    'network_latency': guide('Latencia o pérdida en un equipo propio', [
        'Revisar en el historial del destino si la degradación es puntual o sostenida.',
        'Revisar la traza del incidente: dónde aumenta la latencia.'],
        'Degradación hacia {lugar}: {lectura}. Afectación: {afectacion}. Inicio: {inicio}.',
        'Escalar si es crítica o si sigue.', 20),
}


def situation(case):
    if case.kind == 'access':return 'access_' + ((case.detail.get('reading') or {}).get('code') or 'offline')
    if case.kind == 'latency' and case.detail.get('scope') == 'external':
        return 'external_' + ((case.detail.get('verdict') or {}).get('state') or 'undetermined')
    return {'interface': 'network_interface', 'bgp_session': 'network_bgp'}.get(case.kind, 'network_latency')


def current(db, key):
    row = db.get(NocGuide, key)
    base = DEFAULTS.get(key) or DEFAULTS['network_latency']
    if not row:return {'situation': key, **base, 'custom': False}
    return {'situation': key, 'title': row.title, 'verify': row.verify, 'template': row.template, 'escalate': row.escalate,
            'escalate_minutes': row.escalate_minutes, 'custom': True, 'updated_at': row.updated_at}


def every(db):
    return [current(db, key) for key in DEFAULTS]


def local(stamp):
    return dt.datetime.fromtimestamp(stamp, ZoneInfo(settings().timezone)).strftime('%d/%m %H:%M') if stamp else '—'


def span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:return f'{max(1, seconds // 60)} min'
    return f'{seconds // 3600} h {seconds % 3600 // 60:02d} min'


def facts(case, stamp):
    """Values for the templates, from what the case already holds."""
    detail = case.detail or {}
    reading = (detail.get('reading') or {}).get('title') or (detail.get('verdict') or {}).get('title')
    role = detail.get('neighbor') or detail.get('port') or {}
    if not reading:
        reading = role.get('summary') or (f'{role.get("label")} «{role.get("description")}»' if role.get('description') else
                                          f'{case.drops} cambios de estado')
    place = (f'OLT {case.subject} ({case.entity_label})' if case.kind == 'access' else
             case.subject if detail.get('scope') == 'external' else f'{case.subject} en {case.entity_label}')
    end = case.resolved_at or case.closed_at or stamp
    return {'lugar': place, 'lectura': reading.rstrip('.'), 'afectacion': (detail.get('impact') or {}).get('text') or 'sin estimar',
            'zonas': ', '.join(detail.get('zones') or []) or 'sin zona', 'inicio': local(case.opened_at),
            'duracion': span(end - case.opened_at), 'servicio': case.subject}


class Blank(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def draft(db, case, kind, stamp):
    """The text proposed to level 1 for one report; they review it before sending."""
    found = facts(case, stamp)
    if kind == 'restablecido':
        return f'Restablecido: {found["lugar"]}. Duración: {found["duracion"]} (desde {found["inicio"]}).'
    if kind == 'actualizacion':
        state = 'sigue caído' if case.last_state == 'down' else 'en observación'
        return f'Actualización: {found["lugar"]} {state}. {found["lectura"]}. Afectación: {found["afectacion"]}. Lleva {found["duracion"]}.'
    if kind == 'escalado':
        return f'Escalado a N2: {found["lugar"]}. {found["lectura"]}. Afectación: {found["afectacion"]}. Motivo: '
    return current(db, situation(case))['template'].format_map(Blank(found))
