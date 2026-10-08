import time
import uuid
from sqlalchemy import Column, String, Boolean, BigInteger, Integer, ForeignKey, JSON, Text, Index, Float, UniqueConstraint
from sqlalchemy import text
from .db import Base


def uid():
    return str(uuid.uuid4())


def now():
    return int(time.time())


class User(Base):
    __tablename__ = 'users'
    id = Column(String(36), primary_key=True, default=uid)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False, default='operator')
    active = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=True)
    mfa_secret = Column(Text)
    mfa_confirmed = Column(Boolean, nullable=False, default=False)
    last_totp_step = Column(BigInteger, nullable=False, default=-1)
    recovery_hashes = Column(JSON, nullable=False, default=list)
    created_at = Column(BigInteger, nullable=False, default=now)


class AuthSession(Base):
    __tablename__ = 'sessions'
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(ForeignKey('users.id'), nullable=False, index=True)
    csrf_hash = Column(String(64), nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    last_seen = Column(BigInteger, nullable=False, default=now)


class Challenge(Base):
    __tablename__ = 'auth_challenges'
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(ForeignKey('users.id'), nullable=False, index=True)
    csrf_hash = Column(String(64), nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)


class RateBucket(Base):
    __tablename__ = 'rate_buckets'
    key = Column(String(64), primary_key=True)
    count = Column(Integer, nullable=False)
    expires_at = Column(BigInteger, nullable=False, index=True)


class Audit(Base):
    __tablename__ = 'audit_log'
    id = Column(String(36), primary_key=True, default=uid)
    actor_id = Column(String(36))
    action = Column(String(80), nullable=False)
    target = Column(String(200))
    created_at = Column(BigInteger, nullable=False, default=now, index=True)


class Integration(Base):
    __tablename__ = 'integrations'
    kind = Column(String(30), primary_key=True)
    endpoint = Column(Text, nullable=False)
    encrypted_secret = Column(Text)
    updated_at = Column(BigInteger, nullable=False, default=now)
    version = Column(Integer, nullable=False, default=1)
    allowed_networks = Column(JSON, nullable=False, default=list)
    auto_sync = Column(Boolean, nullable=False, default=False)
    events_enabled = Column(Boolean, nullable=False, default=False)
    events_timezone = Column(String(80))
    checked_at = Column(BigInteger)
    synced_at = Column(BigInteger)
    last_error = Column(String(80))


class Job(Base):
    __tablename__ = 'jobs'
    id = Column(String(36), primary_key=True, default=uid)
    kind = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    idempotency_key = Column(String(200), unique=True, nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    run_after = Column(BigInteger, nullable=False, default=now)
    attempts = Column(Integer, nullable=False, default=0)
    lease_until = Column(BigInteger)
    lease_token = Column(String(36))
    result = Column(JSON)
    __table_args__ = (Index('ix_jobs_claim', 'status', 'run_after'),)


class Pop(Base):
    __tablename__ = 'pops'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=False, default='')
    latitude = Column(Float)
    longitude = Column(Float)
    archived = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False, default=now)


class Device(Base):
    __tablename__ = 'devices'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(200), nullable=False)
    pop_id = Column(ForeignKey('pops.id'), index=True)
    archived = Column(Boolean, nullable=False, default=False)
    merged_into = Column(ForeignKey('devices.id'))
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False, default=now)


class SourceIdentity(Base):
    __tablename__ = 'source_identities'
    id = Column(String(36), primary_key=True, default=uid)
    source = Column(ForeignKey('integrations.kind'), nullable=False)
    external_id = Column(String(200), nullable=False)
    device_id = Column(ForeignKey('devices.id'), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    addresses = Column(JSON, nullable=False, default=list)
    details = Column(JSON, nullable=False, default=dict)
    last_seen = Column(BigInteger, nullable=False)
    present = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint('source', 'external_id'),)


class InventoryInterface(Base):
    __tablename__ = 'inventory_interfaces'
    id = Column(String(36), primary_key=True, default=uid)
    identity_id = Column(ForeignKey('source_identities.id'), nullable=False, index=True)
    external_id = Column(String(200), nullable=False)
    name = Column(String(200), nullable=False)
    kind = Column(String(30), nullable=False)
    addresses = Column(JSON, nullable=False, default=list)  # ports: address with prefix ('10.0.9.1/30')
    description = Column(String(500), nullable=False, default='')  # ifAlias: the NOC's ROLE-PROVIDER-DETAIL convention
    last_seen = Column(BigInteger, nullable=False)
    present = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint('identity_id', 'external_id'),)


class Assignment(Base):
    __tablename__ = 'inventory_assignments'
    id = Column(String(36), primary_key=True, default=uid)
    device_id = Column(ForeignKey('devices.id'), nullable=False, index=True)
    previous_pop_id = Column(ForeignKey('pops.id'))
    pop_id = Column(ForeignKey('pops.id'))
    actor_id = Column(String(36), nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    reason = Column(String(100), nullable=False, default='manual')


class Candidate(Base):
    __tablename__ = 'reconciliation_candidates'
    id = Column(String(36), primary_key=True, default=uid)
    left_id = Column(ForeignKey('devices.id'), nullable=False)
    right_id = Column(ForeignKey('devices.id'), nullable=False)
    evidence = Column(JSON, nullable=False, default=list)
    status = Column(String(20), nullable=False, default='pending')
    resolved_by = Column(String(36))
    resolved_at = Column(BigInteger)
    __table_args__ = (UniqueConstraint('left_id', 'right_id'),)


class InventoryLink(Base):
    __tablename__ = 'inventory_links'
    id = Column(String(36), primary_key=True, default=uid)
    left_id = Column(ForeignKey('devices.id'), nullable=False)
    right_id = Column(ForeignKey('devices.id'), nullable=False)
    left_port_id = Column(ForeignKey('inventory_interfaces.id'))
    right_port_id = Column(ForeignKey('inventory_interfaces.id'))
    vlans = Column(JSON, nullable=False, default=list)
    description = Column(String(300), nullable=False, default='')
    actor_id = Column(String(36), nullable=False)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(BigInteger, nullable=False, default=now)


class SyncRun(Base):
    __tablename__ = 'inventory_runs'
    id = Column(String(36), primary_key=True, default=uid)
    source = Column(ForeignKey('integrations.kind'), nullable=False, index=True)
    mode = Column(String(10), nullable=False)
    config_version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    created_at = Column(BigInteger, nullable=False, default=now)
    finished_at = Column(BigInteger)
    count = Column(Integer)
    error_code = Column(String(80))
    job_id = Column(ForeignKey('jobs.id'))


class ProbeProfile(Base):
    __tablename__ = 'probe_profiles'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(120), nullable=False, unique=True)
    version = Column(Integer, nullable=False, default=1)
    archived = Column(Boolean, nullable=False, default=False)


class ProbeProfileVersion(Base):
    __tablename__ = 'probe_profile_versions'
    id = Column(String(36), primary_key=True, default=uid)
    profile_id = Column(ForeignKey('probe_profiles.id'), nullable=False)
    number = Column(Integer, nullable=False)
    config = Column(JSON, nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    __table_args__ = (UniqueConstraint('profile_id','number'),)


class MonitoringDestination(Base):
    __tablename__ = 'monitoring_destinations'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(120), nullable=False)
    scope = Column(String(20), nullable=False)
    device_id = Column(ForeignKey('devices.id'))
    link_id = Column(ForeignKey('inventory_links.id'))
    version = Column(Integer, nullable=False, default=1)


class ProbeTarget(Base):
    __tablename__ = 'probe_targets'
    id = Column(String(36), primary_key=True, default=uid)
    name = Column(String(120), nullable=False)
    address = Column(String(253), nullable=False)
    family = Column(String(10), nullable=False, default='ipv4')
    protocol = Column(String(10), nullable=False, default='icmp')
    check_config = Column(JSON, nullable=False, default=dict)
    dns_policy = Column(String(20), nullable=False, default='approved_ips')
    destination_id = Column(ForeignKey('monitoring_destinations.id'), index=True)
    essential = Column(Boolean, nullable=False, default=True)
    catalog_key = Column(String(200), unique=True)
    catalog_metadata = Column(JSON, nullable=False, default=dict)
    validation = Column(JSON, nullable=False, default=dict)
    pop_id = Column(ForeignKey('pops.id'))
    description = Column(String(500), nullable=False, default='')
    categories = Column(JSON, nullable=False, default=list)
    service = Column(String(120), nullable=False, default='')
    profile_version_id = Column(ForeignKey('probe_profile_versions.id'), nullable=False)
    overrides = Column(JSON, nullable=False, default=dict)
    effective = Column(JSON, nullable=False)
    approved_ips = Column(JSON, nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    archived = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    epoch = Column(Integer, nullable=False, default=1)
    created_at = Column(BigInteger, nullable=False, default=now)
    changed_at = Column(BigInteger, nullable=False, default=now)
    next_due = Column(BigInteger, nullable=False, default=0)
    lease_token = Column(String(36))
    lease_until = Column(BigInteger)
    resolved_ips = Column(JSON, nullable=False, default=list)
    resolved_at = Column(BigInteger)
    last_error = Column(String(80))


class ProbeBatch(Base):
    __tablename__ = 'probe_batches'
    id = Column(String(36), primary_key=True, default=uid)
    target_id = Column(ForeignKey('probe_targets.id'), nullable=False, index=True)
    epoch = Column(Integer, nullable=False)
    slot = Column(BigInteger, nullable=False)
    mode = Column(String(10), nullable=False, default='scheduled')
    status = Column(String(20), nullable=False, default='pending')
    token = Column(String(36))
    lease_until = Column(BigInteger)
    finished_at = Column(BigInteger)
    error_code = Column(String(80))
    result = Column(JSON)
    config = Column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint('target_id','epoch','slot','mode'),
        Index('ix_probe_batches_active', 'status', 'lease_until', postgresql_where=text("status IN ('pending','running')")),
        Index('ix_probe_batches_finished', 'finished_at'))


class ProbeSeries(Base):
    __tablename__ = 'probe_series'
    id = Column(String(36), primary_key=True, default=uid)
    target_id = Column(ForeignKey('probe_targets.id'), nullable=False, index=True)
    address = Column(String(45), nullable=False)
    epoch = Column(Integer, nullable=False)
    started_at = Column(BigInteger, nullable=False)
    ended_at = Column(BigInteger)
    config = Column(JSON, nullable=False)


class ProbeRound(Base):
    __tablename__ = 'probe_rounds'
    series_id = Column(ForeignKey('probe_series.id'), primary_key=True)
    slot = Column(BigInteger, primary_key=True)
    batch_id = Column(String(36), nullable=False)
    status = Column(String(20), nullable=False)
    details = Column(JSON, nullable=False, default=dict)
    sent = Column(Integer, nullable=False, default=0)
    received = Column(Integer, nullable=False, default=0)
    samples = Column(JSON, nullable=False, default=list)
    error_code = Column(String(80))
    finished_at = Column(BigInteger, nullable=False)


class ProbeRollup(Base):
    __tablename__ = 'probe_rollups'
    series_id = Column(ForeignKey('probe_series.id'), primary_key=True)
    bucket = Column(BigInteger, primary_key=True)
    resolution = Column(Integer, primary_key=True)
    expected = Column(Integer, nullable=False)
    valid = Column(Integer, nullable=False)
    sent = Column(Integer, nullable=False)
    received = Column(Integer, nullable=False)
    rtt_sum = Column(Float, nullable=False)
    rtt_min = Column(Float)
    rtt_max = Column(Float)


class ProbeState(Base):
    __tablename__ = 'probe_states'
    id = Column(String(36), primary_key=True, default=uid)
    target_id = Column(ForeignKey('probe_targets.id'), nullable=False, index=True)
    address = Column(String(45), nullable=False)
    series_id = Column(ForeignKey('probe_series.id'))
    last_slot = Column(BigInteger)
    conditions = Column(JSON, nullable=False, default=dict)
    counters = Column(JSON, nullable=False, default=dict)
    metrics = Column(JSON, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint('target_id','address'),)


class ProbeIncident(Base):
    __tablename__ = 'probe_incidents'
    id = Column(String(36), primary_key=True, default=uid)
    target_id = Column(ForeignKey('probe_targets.id'), nullable=False, index=True)
    address = Column(String(45), nullable=False)
    pop_id = Column(ForeignKey('pops.id'))
    opened_at = Column(BigInteger, nullable=False, default=now)
    closed_at = Column(BigInteger)
    severity = Column(String(12), nullable=False)
    conditions = Column(JSON, nullable=False)
    profile = Column(JSON, nullable=False)
    acknowledged_at = Column(BigInteger)
    acknowledged_by = Column(String(36))


class ProbeIncidentEvent(Base):
    __tablename__ = 'probe_incident_events'
    id = Column(String(36), primary_key=True, default=uid)
    incident_id = Column(ForeignKey('probe_incidents.id'), nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    actor_id = Column(String(36))
    data = Column(JSON, nullable=False, default=dict)


class ProbeMaintenance(Base):
    __tablename__ = 'probe_maintenance'
    id = Column(String(36), primary_key=True, default=uid)
    target_id = Column(ForeignKey('probe_targets.id'), nullable=False, index=True)
    starts_at = Column(BigInteger, nullable=False)
    ends_at = Column(BigInteger, nullable=False)
    reason = Column(String(300), nullable=False)
    actor_id = Column(String(36), nullable=False)
    cancelled = Column(Boolean, nullable=False, default=False)
    cancelled_at = Column(BigInteger)


class ProbeOrigin(Base):
    __tablename__ = 'probe_origins'
    id = Column(String(20), primary_key=True, default='odin')
    heartbeat = Column(BigInteger, nullable=False)
    error_code = Column(String(80))
    fping_version = Column(String(80))
    rollup_until = Column(BigInteger)
    hourly_until = Column(BigInteger)
    retention_at = Column(BigInteger)


class ProbeCatalog(Base):
    __tablename__ = 'probe_catalogs'
    id = Column(String(80), primary_key=True)
    version = Column(Integer, nullable=False)
    document = Column(JSON, nullable=False)
    digest = Column(String(64), nullable=False)
    imported_at = Column(BigInteger, nullable=False, default=now)


class EventStream(Base):
    __tablename__ = 'event_streams'
    id = Column(String(36), primary_key=True, default=uid)
    source = Column(String(30), nullable=False)
    name = Column(String(80), nullable=False)
    version = Column(Integer, nullable=False, default=0)
    cursor = Column(String(2048))
    coverage = Column(String(20), nullable=False, default='unavailable')
    reason = Column(String(80))
    window_start = Column(BigInteger)
    window_end = Column(BigInteger)
    last_attempt_at = Column(BigInteger)
    last_success_at = Column(BigInteger)
    last_event_at = Column(BigInteger)
    complete_through = Column(BigInteger)
    __table_args__ = (UniqueConstraint('source', 'name'),)


class SourceEvent(Base):
    __tablename__ = 'source_events'
    id = Column(String(36), primary_key=True, default=uid)
    stream_id = Column(ForeignKey('event_streams.id'), nullable=False)
    external_id = Column(String(200), nullable=False)
    identity_kind = Column(String(20), nullable=False)
    entity_id = Column(String(200))
    device_id = Column(ForeignKey('devices.id'), index=True)
    occurred_at = Column(BigInteger, nullable=False)
    source_updated_at = Column(BigInteger, nullable=False)
    received_at = Column(BigInteger, nullable=False, default=now)
    last_received_at = Column(BigInteger, nullable=False, default=now)
    kind = Column(String(80), nullable=False)
    status = Column(String(20), nullable=False)
    severity = Column(String(20), nullable=False)
    original_severity = Column(String(80), nullable=False)
    title = Column(String(500), nullable=False)
    recovery_of = Column(String(200))
    reference = Column(String(300), nullable=False)
    condition_key = Column(String(200))
    original_state = Column(String(40), nullable=False, default='')
    time_basis = Column(String(20), nullable=False, default='source')
    observation_sequence = Column(BigInteger, nullable=False, default=0)
    content_hash = Column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint('stream_id', 'external_id'),
        Index('ix_source_events_time', 'occurred_at', 'id'),
        Index('ix_source_events_condition', 'stream_id', 'condition_key', 'occurred_at'),
        Index('ix_source_events_entity_time', 'entity_id', 'occurred_at'),
        Index('ix_source_events_received', 'received_at', 'id'))


class EventPage(Base):
    __tablename__ = 'event_pages'
    id = Column(String(36), primary_key=True, default=uid)
    stream_id = Column(ForeignKey('event_streams.id'), nullable=False)
    request_key = Column(String(100), nullable=False)
    content_hash = Column(String(64), nullable=False)
    coverage = Column(String(20), nullable=False)
    reason = Column(String(80))
    window_start = Column(BigInteger, nullable=False)
    window_end = Column(BigInteger, nullable=False)
    event_count = Column(Integer, nullable=False)
    changed_count = Column(Integer, nullable=False)
    received_at = Column(BigInteger, nullable=False, default=now)
    checkpoint_version = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint('stream_id', 'request_key'),
        Index('ix_event_pages_version', 'stream_id', 'checkpoint_version'),)


class EventCollector(Base):
    __tablename__ = 'event_collectors'
    id = Column(String(60), primary_key=True)
    source = Column(ForeignKey('integrations.kind'), nullable=False)
    topic = Column(String(30), nullable=False)
    stream_name = Column(String(80), nullable=False)
    config_version = Column(Integer, nullable=False)
    next_due = Column(BigInteger, nullable=False)
    job_id = Column(ForeignKey('jobs.id'))
    last_attempt_at = Column(BigInteger)
    last_success_at = Column(BigInteger)
    last_error = Column(String(80))
    failure_count = Column(Integer, nullable=False, default=0)
    last_count = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint('source','topic'),)


class LogSender(Base):
    """Latest sending address per log source name; association to inventory is confirmed by an administrator."""
    __tablename__ = 'log_senders'
    name = Column(String(200), primary_key=True)
    address = Column(String(45), nullable=False)
    first_seen = Column(BigInteger, nullable=False)
    last_seen = Column(BigInteger, nullable=False)


class Case(Base):
    """Correlated case: one entity and subject (BGP session or interface) with its linked evidence."""
    __tablename__ = 'cases'
    id = Column(String(36), primary_key=True, default=uid)
    kind = Column(String(20), nullable=False)
    key = Column(String(400), nullable=False, index=True)
    entity_key = Column(String(250), nullable=False, index=True)
    entity_label = Column(String(200), nullable=False)
    subject = Column(String(200), nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    opened_at = Column(BigInteger, nullable=False)
    last_activity_at = Column(BigInteger, nullable=False)
    last_state = Column(String(10), nullable=False)
    last_state_at = Column(BigInteger, nullable=False)
    resolved_at = Column(BigInteger)
    closed_at = Column(BigInteger)
    state = Column(String(10), nullable=False, default='active')
    severity = Column(String(10), nullable=False, default='warning')
    drops = Column(Integer, nullable=False, default=0)
    recoveries = Column(Integer, nullable=False, default=0)
    rule_version = Column(Integer, nullable=False)
    enriched_at = Column(BigInteger)
    acknowledged_at = Column(BigInteger)
    acknowledged_by = Column(String(36))
    __table_args__ = (Index('ix_cases_state_activity', 'state', 'last_activity_at'),)


class CaseEvidence(Base):
    """Link to data already held by ODIN, with its role and the explained reason for the link."""
    __tablename__ = 'case_evidence'
    id = Column(String(36), primary_key=True, default=uid)
    case_id = Column(ForeignKey('cases.id'), nullable=False, index=True)
    source = Column(String(20), nullable=False)
    ref = Column(String(300), nullable=False)
    role = Column(String(10), nullable=False)
    strength = Column(String(10), nullable=False)
    reason = Column(String(300), nullable=False)
    occurred_at = Column(BigInteger, nullable=False)
    title = Column(String(500), nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    rule_version = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint('case_id', 'source', 'ref'),)


class CaseNote(Base):
    __tablename__ = 'case_notes'
    id = Column(String(36), primary_key=True, default=uid)
    case_id = Column(ForeignKey('cases.id'), nullable=False, index=True)
    author_id = Column(String(36), nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    text = Column(Text, nullable=False)


class CorrelationState(Base):
    __tablename__ = 'correlation_state'
    id = Column(String(20), primary_key=True)
    cursor_received_at = Column(BigInteger, nullable=False, default=0)
    cursor_id = Column(String(36), nullable=False, default='')
    next_due = Column(BigInteger, nullable=False, default=0)
    job_id = Column(ForeignKey('jobs.id'))
    last_run_at = Column(BigInteger)
    last_success_at = Column(BigInteger)
    last_error = Column(String(80))
    alerts_enabled = Column(Boolean, nullable=False, default=False)



class Analysis(Base):
    """One run of the local model: a case analysis or a chat answer, with the evidence pack it read."""
    __tablename__ = 'analyses'
    id = Column(String(36), primary_key=True, default=uid)
    kind = Column(String(10), nullable=False)
    case_id = Column(ForeignKey('cases.id'), index=True)
    conversation_id = Column(ForeignKey('conversations.id', ondelete='CASCADE'), index=True)
    requested_by = Column(String(36), nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    status = Column(String(10), nullable=False, default='pending')
    stage = Column(String(20), nullable=False, default='queued')
    progress = Column(JSON, nullable=False, default=dict)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    job_id = Column(ForeignKey('jobs.id'))
    started_at = Column(BigInteger)
    finished_at = Column(BigInteger)
    input_version = Column(String(120), nullable=False, default='')
    pack = Column(JSON, nullable=False, default=dict)
    result = Column(JSON)
    error = Column(String(80))
    model = Column(String(300))
    prompt_version = Column(Integer, nullable=False, default=0)
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    duration_ms = Column(Integer)


class AnalysisReview(Base):
    """NOC verdict on an analysis; corrections become memory for later analyses of similar cases."""
    __tablename__ = 'analysis_reviews'
    id = Column(String(36), primary_key=True, default=uid)
    analysis_id = Column(ForeignKey('analyses.id'), nullable=False, index=True)
    author_id = Column(String(36), nullable=False)
    verdict = Column(String(10), nullable=False)
    correction = Column(Text, nullable=False, default='')
    created_at = Column(BigInteger, nullable=False, default=now)


class Conversation(Base):
    """Private to its owner; administrators do not read other users' conversations."""
    __tablename__ = 'conversations'
    id = Column(String(36), primary_key=True, default=uid)
    owner_id = Column(String(36), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    context = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False, default=now)
    updated_at = Column(BigInteger, nullable=False, default=now)


class ChatMessage(Base):
    __tablename__ = 'chat_messages'
    id = Column(String(36), primary_key=True, default=uid)
    conversation_id = Column(ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    client_id = Column(String(64))
    analysis_id = Column(String(36))
    created_at = Column(BigInteger, nullable=False, default=now)
    __table_args__ = (UniqueConstraint('conversation_id', 'client_id'),)


class Knowledge(Base):
    """Procedure or finding validated by the NOC; the model's text never becomes knowledge by itself."""
    __tablename__ = 'knowledge'
    id = Column(String(36), primary_key=True, default=uid)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    case_kind = Column(String(20))
    keywords = Column(JSON, nullable=False, default=list)
    source_case_id = Column(String(36))
    created_by = Column(String(36), nullable=False)
    created_at = Column(BigInteger, nullable=False, default=now)
    updated_at = Column(BigInteger, nullable=False, default=now)
    archived = Column(Boolean, nullable=False, default=False)


class AnalystState(Base):
    __tablename__ = 'analyst_state'
    id = Column(String(20), primary_key=True)
    last_success_at = Column(BigInteger)
    last_failure_at = Column(BigInteger)
    last_error = Column(String(80))
    model = Column(String(300))
    context = Column(Integer)
    heartbeat_at = Column(BigInteger)


class TelegramLink(Base):
    """An ODIN user's Telegram account: proven by sending the one-time code from «Mi acceso» to the bot."""
    __tablename__ = 'telegram_links'
    user_id = Column(String(36), primary_key=True)
    telegram_user_id = Column(BigInteger, unique=True)
    code = Column(String(12), unique=True)
    code_expires_at = Column(BigInteger)
    linked_at = Column(BigInteger)


class TelegramState(Base):
    """The NOC group the bot posts to (bound by an administrator with /grupo) and the read position of updates."""
    __tablename__ = 'telegram_state'
    id = Column(String(20), primary_key=True)
    chat_id = Column(BigInteger)
    thread_id = Column(BigInteger)  # the forum topic /grupo was sent from; None posts to the group itself
    update_offset = Column(BigInteger, nullable=False, default=0)
    paused = Column(Boolean, nullable=False, default=False)
    heartbeat_at = Column(BigInteger)
    last_error = Column(String(80))


class TelegramMessage(Base):
    """The thread of one alerting case in the group: the case message, its diagnosis and its recovery."""
    __tablename__ = 'telegram_messages'
    case_id = Column(ForeignKey('cases.id'), primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    analysis_id = Column(String(36))
    diagnosis_message_id = Column(BigInteger)
    recovered = Column(Boolean, nullable=False, default=False)
    notified_at = Column(BigInteger, nullable=False)


class TelegramPrompt(Base):
    """«What really happened?» asked to one user after a partial or incorrect verdict; their reply is the correction."""
    __tablename__ = 'telegram_prompts'
    message_id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    analysis_id = Column(String(36), nullable=False)
    user_id = Column(String(36), nullable=False)
    verdict = Column(String(10), nullable=False)
    created_at = Column(BigInteger, nullable=False)


class TelegramQuestion(Base):
    """A question asked with /pregunta (or replying to a case thread): answered in the group when the chat run ends."""
    __tablename__ = 'telegram_questions'
    analysis_id = Column(String(36), primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    thread_id = Column(BigInteger)
    created_at = Column(BigInteger, nullable=False)
