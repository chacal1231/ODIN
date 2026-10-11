from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='ODIN_', extra='ignore')
    database_url: str
    master_key_file: str
    origin: str = 'http://localhost:8080'
    secure_cookies: bool = True
    map_tile_url: str = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
    # The operator this installation serves, shown beside ODIN (one installation per ISP).
    isp_name: str = ''
    probe_concurrency: int = 24
    probe_max_addresses: int = 256
    trace_concurrency: int = 4
    idle_seconds: int = 1800
    session_seconds: int = 43200

    @model_validator(mode='after')
    def validate_settings(self):
        if not self.database_url.startswith('postgresql+psycopg://'):
            raise ValueError('PostgreSQL es obligatorio')
        if self.origin.endswith('/') or not self.origin.startswith(('https://', 'http://')):
            raise ValueError('ODIN_ORIGIN debe ser un origen sin barra final')
        if not self.secure_cookies and (urlsplit(self.origin).scheme != 'http' or urlsplit(self.origin).hostname not in ('localhost', '127.0.0.1')):
            raise ValueError('Cookies sin Secure solo permitidas en desarrollo local')
        if not 1 <= self.probe_concurrency <= 32 or not 1 <= self.probe_max_addresses <= 512:
            raise ValueError('Capacidad de sondeos inválida')
        if not 1 <= self.trace_concurrency <= 16:
            raise ValueError('Capacidad de trazas inválida')
        if len(self.isp_name) > 60 or any(ord(c) < 32 for c in self.isp_name):
            raise ValueError('ODIN_ISP_NAME: hasta 60 caracteres, sin controles')
        if self.idle_seconds <= 0 or self.session_seconds <= 0:
            raise ValueError('Duración de sesión inválida')
        Fernet(Path(self.master_key_file).read_bytes().strip())
        return self

    def cipher(self):
        return Fernet(Path(self.master_key_file).read_bytes().strip())


@lru_cache
def settings():
    return Settings()
