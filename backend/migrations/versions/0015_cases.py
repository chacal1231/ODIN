"""Correlated cases: cases, linked evidence, notes, confirmed peer interfaces and correlator state."""
from alembic import op
revision='0015_cases'
down_revision='0014_bmp_routes'
branch_labels=None
depends_on=None


def upgrade():
    # The correlator walks new events by reception order.
    op.execute('CREATE INDEX ix_source_events_received ON source_events(received_at, id)')
    op.execute('''CREATE TABLE cases (id varchar(36) PRIMARY KEY, kind varchar(20) NOT NULL, key varchar(400) NOT NULL,
        entity_key varchar(250) NOT NULL, entity_label varchar(200) NOT NULL, subject varchar(200) NOT NULL, detail json NOT NULL,
        opened_at bigint NOT NULL, last_activity_at bigint NOT NULL, last_state varchar(10) NOT NULL, last_state_at bigint NOT NULL,
        resolved_at bigint, closed_at bigint, state varchar(10) NOT NULL DEFAULT 'active', severity varchar(10) NOT NULL DEFAULT 'warning',
        drops integer NOT NULL DEFAULT 0, recoveries integer NOT NULL DEFAULT 0, rule_version integer NOT NULL, enriched_at bigint,
        acknowledged_at bigint, acknowledged_by varchar(36),
        CHECK (kind IN ('bgp_session','interface')), CHECK (state IN ('active','stale','closed')),
        CHECK (last_state IN ('down','up')), CHECK (severity IN ('warning','critical')))''')
    op.execute('CREATE INDEX ix_cases_key ON cases(key)')
    op.execute('CREATE INDEX ix_cases_entity_key ON cases(entity_key)')
    op.execute('CREATE INDEX ix_cases_state_activity ON cases(state, last_activity_at)')
    # Only one non-closed case per key.
    op.execute("CREATE UNIQUE INDEX ux_cases_open_key ON cases(key) WHERE state <> 'closed'")
    op.execute('''CREATE TABLE case_evidence (id varchar(36) PRIMARY KEY, case_id varchar(36) NOT NULL REFERENCES cases(id),
        source varchar(20) NOT NULL, ref varchar(300) NOT NULL, role varchar(10) NOT NULL, strength varchar(10) NOT NULL,
        reason varchar(300) NOT NULL, occurred_at bigint NOT NULL, title varchar(500) NOT NULL, data json NOT NULL,
        rule_version integer NOT NULL, UNIQUE (case_id, source, ref),
        CHECK (role IN ('trigger','context','impact')), CHECK (strength IN ('entity','device','time')))''')
    op.execute('CREATE INDEX ix_case_evidence_case_id ON case_evidence(case_id)')
    op.execute('''CREATE TABLE case_notes (id varchar(36) PRIMARY KEY, case_id varchar(36) NOT NULL REFERENCES cases(id),
        author_id varchar(36) NOT NULL, created_at bigint NOT NULL, text text NOT NULL)''')
    op.execute('CREATE INDEX ix_case_notes_case_id ON case_notes(case_id)')
    op.execute('''CREATE TABLE peer_interfaces (entity_key varchar(250) NOT NULL, peer varchar(45) NOT NULL,
        exporter varchar(200) NOT NULL, interface varchar(200) NOT NULL, confirmed_by varchar(36) NOT NULL,
        confirmed_at bigint NOT NULL, PRIMARY KEY (entity_key, peer))''')
    op.execute('''CREATE TABLE correlation_state (id varchar(20) PRIMARY KEY, cursor_received_at bigint NOT NULL DEFAULT 0,
        cursor_id varchar(36) NOT NULL DEFAULT '', next_due bigint NOT NULL DEFAULT 0, job_id varchar(36) REFERENCES jobs(id),
        last_run_at bigint, last_success_at bigint, last_error varchar(80), alerts_enabled boolean NOT NULL DEFAULT false)''')


def downgrade():
    for table in ('correlation_state', 'peer_interfaces', 'case_notes', 'case_evidence', 'cases'):op.drop_table(table)
    op.drop_index('ix_source_events_received', table_name='source_events')
