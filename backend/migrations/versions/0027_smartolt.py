"""SmartOLT (read-only): OLTs, PON ports, uplinks, ONUs without customer data, PON outages and the reader state."""
from alembic import op
revision='0027_smartolt'
down_revision='0026_telegram_providers'
branch_labels=None
depends_on=None


def upgrade():
    for statement in (
        '''CREATE TABLE olts (id varchar(40) PRIMARY KEY, name varchar(200) NOT NULL, hardware varchar(120) NOT NULL, ip varchar(45),
            device_id varchar(36) REFERENCES devices(id), pop_id varchar(36) REFERENCES pops(id), pop_manual boolean NOT NULL,
            uptime_seconds bigint, temperature double precision, reachable boolean NOT NULL, present boolean NOT NULL,
            first_seen bigint NOT NULL, last_seen bigint NOT NULL)''',
        '''CREATE TABLE olt_pon_ports (olt_id varchar(40) NOT NULL REFERENCES olts(id), board varchar(10) NOT NULL, port varchar(10) NOT NULL,
            pon_type varchar(20) NOT NULL, admin_status varchar(30) NOT NULL, status varchar(30) NOT NULL, description varchar(300) NOT NULL,
            tx_power double precision, onus integer NOT NULL, online integer NOT NULL, average_signal double precision, last_seen bigint NOT NULL,
            PRIMARY KEY (olt_id, board, port))''',
        '''CREATE TABLE olt_uplinks (olt_id varchar(40) NOT NULL REFERENCES olts(id), name varchar(80) NOT NULL, description varchar(300) NOT NULL,
            kind varchar(30) NOT NULL, admin_status varchar(30) NOT NULL, status varchar(40) NOT NULL, negotiation varchar(60) NOT NULL,
            temperature double precision, last_seen bigint NOT NULL, PRIMARY KEY (olt_id, name))''',
        '''CREATE TABLE onus (id varchar(120) PRIMARY KEY, sn varchar(64) NOT NULL, olt_id varchar(40) NOT NULL, board varchar(10) NOT NULL,
            port varchar(10) NOT NULL, onu varchar(10) NOT NULL, onu_type varchar(120) NOT NULL, zone varchar(120) NOT NULL, odb varchar(120) NOT NULL,
            name varchar(200) NOT NULL, admin_status varchar(30) NOT NULL, status varchar(30) NOT NULL, status_changed_at bigint,
            signal varchar(30) NOT NULL, signal_1310 double precision, signal_1490 double precision, present boolean NOT NULL, last_seen bigint NOT NULL)''',
        'CREATE INDEX ix_onus_olt_id ON onus(olt_id)',
        'CREATE INDEX ix_onus_pon ON onus(olt_id, board, port)',
        '''CREATE TABLE olt_outages (id varchar(36) PRIMARY KEY, olt_id varchar(40) NOT NULL REFERENCES olts(id), board varchar(10) NOT NULL,
            port varchar(10) NOT NULL, cause varchar(20) NOT NULL, zone varchar(120) NOT NULL, description varchar(300) NOT NULL, started_at bigint,
            subscribers integer NOT NULL, affected integer NOT NULL, odbs json NOT NULL, active boolean NOT NULL, first_seen bigint NOT NULL,
            last_seen bigint NOT NULL, ended_at bigint, CHECK (cause IN ('los', 'power', 'offline', 'stale')))''',
        'CREATE INDEX ix_olt_outages_olt_id ON olt_outages(olt_id)',
        'CREATE UNIQUE INDEX uq_olt_outages_active ON olt_outages(olt_id, board, port, cause) WHERE active',
        '''CREATE TABLE smartolt_state (id varchar(20) PRIMARY KEY, config_version integer, job_id varchar(36) REFERENCES jobs(id),
            due json NOT NULL, tasks json NOT NULL, blocked_until bigint, rate_remaining integer, rate_limit integer, rate_observed_at bigint)''',
    ):
        op.execute(statement)


def downgrade():
    for table in ('smartolt_state', 'olt_outages', 'onus', 'olt_uplinks', 'olt_pon_ports', 'olts'):op.drop_table(table)
    op.execute("DELETE FROM inventory_runs WHERE source = 'smartolt'")
    op.execute("DELETE FROM integrations WHERE kind = 'smartolt'")
