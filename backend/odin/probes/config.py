import ipaddress
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from ..models import ProbeProfile, ProbeProfileVersion

DEFAULT_PROFILE_ID = '00000000-0000-4000-8000-000000000001'
DEFAULT_VERSION_ID = '00000000-0000-4000-8000-000000000002'
TRANSPORT_PROFILE_ID = '00000000-0000-4000-8000-000000000003'
TRANSPORT_VERSION_ID = '00000000-0000-4000-8000-000000000004'


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    interval_seconds: int = Field(default=30, ge=30, le=300)
    packets: int = Field(default=10, ge=1, le=20)
    period_ms: int = Field(default=500, ge=100, le=2000)
    timeout_ms: int = Field(default=500, ge=50, le=2000)
    window_seconds: int = Field(default=300, ge=300, le=1800)
    loss_warning: float = Field(default=5, gt=0, lt=100)
    loss_critical: float = Field(default=20, gt=0, le=100)
    loss_recovery: float = Field(default=2, gt=0, lt=100)
    rtt_warning: float | None = Field(default=None, gt=0, le=10000)
    rtt_critical: float | None = Field(default=None, gt=0, le=20000)
    rtt_recovery_ratio: float = Field(default=0.9, ge=0.5, lt=1)
    minimum_coverage: float = Field(default=0.8, ge=0.5, le=1)
    persistence: int = Field(default=2, ge=1, le=10)
    recovery_rounds: int = Field(default=3, ge=1, le=10)
    unreachable_rounds: int = Field(default=3, ge=2, le=10)

    @model_validator(mode='after')
    def valid(self):
        if self.interval_seconds % 30 or self.window_seconds % self.interval_seconds:
            raise ValueError('Intervalo múltiplo de 30 y ventana múltiplo del intervalo')
        if self.timeout_ms > self.period_ms or self.packets*self.period_ms/1000+2 >= self.interval_seconds*0.8:
            raise ValueError('Los paquetes y timeout deben terminar antes de la siguiente ronda')
        if not self.loss_recovery < self.loss_warning < self.loss_critical:
            raise ValueError('Pérdida: recuperación < advertencia < crítico')
        if (self.rtt_warning is None)!=(self.rtt_critical is None):
            raise ValueError('Define ambos umbrales RTT o desactiva ambos')
        if self.rtt_warning is not None and self.rtt_warning >= self.rtt_critical:
            raise ValueError('RTT crítico debe ser mayor que advertencia')
        return self


def safe_ip(value, family=None):
    ip=ipaddress.ip_address(value)
    if '%' in str(value) or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise ValueError('Dirección no autorizable para sondeos')
    if family and ip.version != (4 if family=='ipv4' else 6):
        raise ValueError('La IP no coincide con la familia seleccionada')
    return str(ip)


def host(value):
    value=value.strip().rstrip('.')
    try:
        return safe_ip(value)
    except ValueError:
        # Numeric-looking invalid addresses must not become hostnames.
        if ':' in value or re.fullmatch(r'[0-9.]+',value):raise ValueError('IP no válida')
    try:value=value.encode('idna').decode('ascii').lower()
    except UnicodeError:raise ValueError('DNS no válido')
    if len(value)>253 or '.' not in value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',x) for x in value.split('.')):
        raise ValueError('Introduce una IP o un nombre DNS, sin URL ni puerto')
    return value


def seed_defaults(db):
    db.execute(text('SELECT pg_advisory_xact_lock(712003)'))
    if not db.get(ProbeProfile,DEFAULT_PROFILE_ID):
        db.add(ProbeProfile(id=DEFAULT_PROFILE_ID,name='Estándar',version=1,archived=False));db.flush()
        db.add(ProbeProfileVersion(id=DEFAULT_VERSION_ID,profile_id=DEFAULT_PROFILE_ID,number=1,config=ProbeConfig().model_dump()))
    if not db.get(ProbeProfile,TRANSPORT_PROFILE_ID):
        db.add(ProbeProfile(id=TRANSPORT_PROFILE_ID,name='Servicios TCP / HTTPS',version=1,archived=False));db.flush()
        db.add(ProbeProfileVersion(id=TRANSPORT_VERSION_ID,profile_id=TRANSPORT_PROFILE_ID,number=1,
            config=ProbeConfig(interval_seconds=60,packets=1,period_ms=2000,timeout_ms=2000).model_dump()))
    templates=[
        ('00000000-0000-4000-8000-000000000011','00000000-0000-4000-8000-000000000012','PE / infraestructura propia',ProbeConfig()),
        ('00000000-0000-4000-8000-000000000013','00000000-0000-4000-8000-000000000014','Enlaces y tránsito',ProbeConfig()),
        ('00000000-0000-4000-8000-000000000015','00000000-0000-4000-8000-000000000016','DNS público · conectividad ICMP',ProbeConfig()),
        ('00000000-0000-4000-8000-000000000017','00000000-0000-4000-8000-000000000018','Servicios regionales',ProbeConfig(interval_seconds=60,packets=1,period_ms=2000,timeout_ms=2000)),
        ('00000000-0000-4000-8000-000000000019','00000000-0000-4000-8000-000000000020','Servicios globales',ProbeConfig(interval_seconds=60,packets=1,period_ms=2000,timeout_ms=2000)),
        ('00000000-0000-4000-8000-000000000021','00000000-0000-4000-8000-000000000022','Referencia internacional',ProbeConfig(interval_seconds=60,packets=1,period_ms=2000,timeout_ms=2000)),
    ]
    for profile_id,version_id,name,config in templates:
        if not db.get(ProbeProfile,profile_id):
            db.add(ProbeProfile(id=profile_id,name=name,version=1,archived=False));db.flush()
            db.add(ProbeProfileVersion(id=version_id,profile_id=profile_id,number=1,config=config.model_dump()))
    db.commit()


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    port: int = Field(default=443,ge=1,le=65535)
    path: str = Field(default='/',max_length=1024)
    method: str = Field(default='HEAD',pattern='^(HEAD|GET)$')
    expected_status: list[int] = Field(default_factory=lambda:[200,204,301,302,303,307,308,400,401,403,404,405],min_length=1,max_length=32)

    @model_validator(mode='after')
    def valid(self):
        if not self.path.startswith('/') or self.path.startswith('//') or any(ord(c)<=32 or ord(c)>=127 for c in self.path):
            raise ValueError('Ruta HTTP relativa, ASCII, sin espacios ni controles')
        if any(code<200 or code>599 for code in self.expected_status):raise ValueError('Estado HTTP no válido')
        self.expected_status=sorted(set(self.expected_status))
        return self


def measurement_config(config,protocol,check_config=None):
    if protocol not in ('icmp','tcp','https'):raise ValueError('Protocolo no válido')
    result=dict(config)
    if protocol!='icmp':
        if config['packets']>3:raise ValueError('TCP/HTTPS admite hasta 3 intentos por ronda; usa el perfil Servicios TCP / HTTPS')
        result.update(protocol=protocol,check_config=TransportConfig(**(check_config or {})).model_dump())
    else:result['protocol']='icmp'
    return result
