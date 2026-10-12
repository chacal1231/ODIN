"""NOC communication: WhatsApp group, level-1 reports per case, editable guides, report deadline and shifts."""
from alembic import op
revision='0031_noc_communication'
down_revision='0030_access_cases'
branch_labels=None
depends_on=None


def upgrade():
    for statement in (
        '''CREATE TABLE noc_settings (id varchar(10) PRIMARY KEY, report_minutes integer NOT NULL, shifts json NOT NULL,
            updated_at bigint, updated_by varchar(36), CHECK (report_minutes BETWEEN 1 AND 240))''',
        '''CREATE TABLE noc_guides (situation varchar(40) PRIMARY KEY, title varchar(120) NOT NULL, verify json NOT NULL,
            template text NOT NULL, escalate varchar(500) NOT NULL, escalate_minutes integer, updated_at bigint NOT NULL,
            updated_by varchar(36))''',
        '''CREATE TABLE case_reports (id varchar(36) PRIMARY KEY, case_id varchar(36) NOT NULL REFERENCES cases(id), kind varchar(20) NOT NULL,
            text text NOT NULL, author_id varchar(36) NOT NULL, created_at bigint NOT NULL, deliveries json NOT NULL,
            CHECK (kind IN ('inicial','actualizacion','restablecido','escalado')))''',
        'CREATE INDEX ix_case_reports_case ON case_reports(case_id, created_at)',
        '''CREATE TABLE shift_reports (id varchar(36) PRIMARY KEY, kind varchar(20) NOT NULL, starts_at bigint NOT NULL, ends_at bigint NOT NULL,
            text text NOT NULL, data json NOT NULL, created_at bigint NOT NULL, deliveries json NOT NULL,
            UNIQUE (kind, ends_at), CHECK (kind IN ('cierre','apertura')))''',
        '''CREATE TABLE whatsapp_state (id varchar(10) PRIMARY KEY, group_id varchar(120), group_name varchar(200),
            paused boolean NOT NULL DEFAULT false, heartbeat_at bigint, last_error varchar(200))''',
        '''CREATE TABLE whatsapp_messages (case_id varchar(36) PRIMARY KEY REFERENCES cases(id), group_id varchar(120) NOT NULL,
            message_id varchar(200) NOT NULL, analysis_id varchar(36), recovered boolean NOT NULL DEFAULT false, notified_at bigint NOT NULL)''',
    ):
        op.execute(statement)


def downgrade():
    for table in ('whatsapp_messages', 'whatsapp_state', 'shift_reports', 'case_reports', 'noc_guides', 'noc_settings'):
        op.drop_table(table)
