"""Versioned probes, measurements, deterministic incidents and maintenance."""
from alembic import op
from datetime import datetime, timezone, timedelta

revision = '0004_probes'
down_revision = '0003_link_ports'
branch_labels = None
depends_on = None


def upgrade():
    statements = [
        """CREATE TABLE probe_profiles (id varchar(36) PRIMARY KEY, name varchar(120) NOT NULL UNIQUE, version integer NOT NULL, archived boolean NOT NULL)""",
        """CREATE TABLE probe_profile_versions (id varchar(36) PRIMARY KEY, profile_id varchar(36) NOT NULL REFERENCES probe_profiles(id), number integer NOT NULL, config json NOT NULL, created_at bigint NOT NULL, UNIQUE(profile_id,number))""",
        """CREATE TABLE probe_targets (id varchar(36) PRIMARY KEY, name varchar(120) NOT NULL, address varchar(253) NOT NULL, family varchar(10) NOT NULL, pop_id varchar(36) REFERENCES pops(id), description varchar(500) NOT NULL, profile_version_id varchar(36) NOT NULL REFERENCES probe_profile_versions(id), overrides json NOT NULL, effective json NOT NULL, approved_ips json NOT NULL, enabled boolean NOT NULL, archived boolean NOT NULL, version integer NOT NULL, epoch integer NOT NULL, created_at bigint NOT NULL, changed_at bigint NOT NULL, next_due bigint NOT NULL, lease_token varchar(36), lease_until bigint, resolved_ips json NOT NULL, resolved_at bigint, last_error varchar(80))""",
        "CREATE UNIQUE INDEX uq_probe_target_name ON probe_targets(lower(name)) WHERE NOT archived",
        """CREATE TABLE probe_batches (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id), epoch integer NOT NULL, slot bigint NOT NULL, mode varchar(10) NOT NULL, status varchar(20) NOT NULL, token varchar(36), lease_until bigint, finished_at bigint, error_code varchar(80), result json, config json NOT NULL, UNIQUE(target_id,epoch,slot,mode))""",
        "CREATE INDEX ix_probe_batches_target_id ON probe_batches(target_id)",
        """CREATE TABLE probe_series (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id), address varchar(45) NOT NULL, epoch integer NOT NULL, started_at bigint NOT NULL, ended_at bigint, config json NOT NULL)""",
        "CREATE INDEX ix_probe_series_target_id ON probe_series(target_id)",
        "CREATE UNIQUE INDEX uq_probe_series_active ON probe_series(target_id,address) WHERE ended_at IS NULL",
        """CREATE TABLE probe_rounds (series_id varchar(36) NOT NULL REFERENCES probe_series(id), slot bigint NOT NULL, batch_id varchar(36) NOT NULL, status varchar(20) NOT NULL, sent integer NOT NULL, received integer NOT NULL, samples json NOT NULL, error_code varchar(80), finished_at bigint NOT NULL, PRIMARY KEY(series_id,slot), CHECK(received>=0 AND received<=sent)) PARTITION BY RANGE(slot)""",
        """CREATE TABLE probe_rollups (series_id varchar(36) NOT NULL REFERENCES probe_series(id), bucket bigint NOT NULL, resolution integer NOT NULL, expected integer NOT NULL, valid integer NOT NULL, sent integer NOT NULL, received integer NOT NULL, rtt_sum double precision NOT NULL, rtt_min double precision, rtt_max double precision, PRIMARY KEY(series_id,bucket,resolution))""",
        """CREATE TABLE probe_states (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id), address varchar(45) NOT NULL, series_id varchar(36) REFERENCES probe_series(id), last_slot bigint, conditions json NOT NULL, counters json NOT NULL, metrics json NOT NULL, UNIQUE(target_id,address))""",
        "CREATE INDEX ix_probe_states_target_id ON probe_states(target_id)",
        """CREATE TABLE probe_incidents (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id), address varchar(45) NOT NULL, pop_id varchar(36) REFERENCES pops(id), opened_at bigint NOT NULL, closed_at bigint, severity varchar(12) NOT NULL, conditions json NOT NULL, profile json NOT NULL, acknowledged_at bigint, acknowledged_by varchar(36))""",
        "CREATE INDEX ix_probe_incidents_target_id ON probe_incidents(target_id)",
        "CREATE UNIQUE INDEX uq_probe_incident_active ON probe_incidents(target_id,address) WHERE closed_at IS NULL",
        """CREATE TABLE probe_incident_events (id varchar(36) PRIMARY KEY, incident_id varchar(36) NOT NULL REFERENCES probe_incidents(id), kind varchar(40) NOT NULL, created_at bigint NOT NULL, actor_id varchar(36), data json NOT NULL)""",
        "CREATE INDEX ix_probe_incident_events_incident_id ON probe_incident_events(incident_id)",
        """CREATE TABLE probe_maintenance (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id), starts_at bigint NOT NULL, ends_at bigint NOT NULL, reason varchar(300) NOT NULL, actor_id varchar(36) NOT NULL, cancelled boolean NOT NULL, cancelled_at bigint, CHECK(ends_at>starts_at))""",
        "CREATE INDEX ix_probe_maintenance_target_id ON probe_maintenance(target_id)",
        """CREATE TABLE probe_origins (id varchar(20) PRIMARY KEY, heartbeat bigint NOT NULL, error_code varchar(80), fping_version varchar(80), rollup_until bigint, hourly_until bigint, retention_at bigint)""",
    ]
    for statement in statements:
        op.execute(statement)
    today=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    for offset in range(-7,3):
        day=today+timedelta(days=offset)
        start=int(day.timestamp())
        op.execute(f"CREATE TABLE probe_rounds_d{day:%Y%m%d} PARTITION OF probe_rounds FOR VALUES FROM ({start}) TO ({start+86400})")


def downgrade():
    for table in ['probe_maintenance','probe_incident_events','probe_incidents','probe_states','probe_rollups','probe_rounds','probe_series','probe_batches','probe_targets','probe_profile_versions','probe_profiles','probe_origins']:
        op.execute(f'DROP TABLE {table}')
