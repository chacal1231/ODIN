"""Adapters supply sanitized, bounded records; transport credentials never belong here."""
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal['zabbix', 'librenms', 'graylog']
Coverage = Literal['complete', 'partial', 'unavailable', 'unsupported']
MAX_TIMESTAMP = 253402300799  # End of year 9999; bounded for storage and clients.


class EventRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    external_id: str = Field(min_length=1, max_length=200)
    identity_kind: Literal['source_id', 'fingerprint'] = 'source_id'
    entity_id: str | None = Field(default=None, min_length=1, max_length=200)
    occurred_at: int = Field(ge=0, le=MAX_TIMESTAMP)
    source_updated_at: int = Field(ge=0, le=MAX_TIMESTAMP)
    kind: str = Field(min_length=1, max_length=80)
    status: Literal['open', 'recovered', 'info', 'withdrawn']
    severity: Literal['info', 'warning', 'critical', 'unknown']
    original_severity: str = Field(max_length=80)
    title: str = Field(min_length=1, max_length=500)
    condition_key: str | None = Field(default=None, max_length=200)
    original_state: str = Field(default='', max_length=40)
    time_basis: Literal['source', 'snapshot'] = 'source'
    observation_sequence: int = Field(default=0, ge=0, le=9223372036854775807)
    recovery_of: str | None = Field(default=None, min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def chronological(self):
        if (self.time_basis == 'snapshot') != (self.observation_sequence > 0):
            raise ValueError('Un snapshot requiere orden explícito de observación')
        if self.source_updated_at < self.occurred_at:
            raise ValueError('La actualización no puede preceder al evento')
        if self.recovery_of and self.status != 'recovered':
            raise ValueError('La relación de recuperación exige estado recuperado')
        return self


class EventQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    window_start: int = Field(ge=0, le=MAX_TIMESTAMP)
    window_end: int = Field(ge=0, le=MAX_TIMESTAMP)
    cursor: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode='after')
    def ordered(self):
        if self.window_end < self.window_start:
            raise ValueError('Ventana invertida')
        return self


class EventPageInput(EventQuery):
    request_key: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0, le=2147483647)
    coverage: Coverage
    reason: str | None = Field(default=None, pattern=r'^[a-z0-9_]{1,80}$')
    next_cursor: str | None = Field(default=None, max_length=2048)
    events: list[EventRecord] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def consistent(self):
        if len(self.events) > self.limit:
            raise ValueError('Página excede el límite solicitado')
        if len({e.external_id for e in self.events}) != len(self.events):
            raise ValueError('IDs repetidos dentro de una página')
        if self.coverage != 'complete' and not self.reason:
            raise ValueError('La cobertura incompleta requiere motivo')
        if self.coverage in ('unavailable', 'unsupported') and (self.events or self.next_cursor != self.cursor):
            raise ValueError('Una lectura fallida no contiene eventos ni avanza cursor')
        return self


class EventAdapter(Protocol):
    def read_events(self, query: EventQuery) -> EventPageInput:
        """Return sanitized observations and explicit coverage, never infer success from []."""
        ...
