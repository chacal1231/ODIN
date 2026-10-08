"""Akvorado traffic: five-minute interface series, interface metadata, exporters and reader state."""
from alembic import op
revision='0013_traffic'
down_revision='0012_log_senders'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE traffic_samples (exporter varchar(200) NOT NULL, interface varchar(200) NOT NULL,
        direction varchar(3) NOT NULL, bucket bigint NOT NULL, bytes bigint NOT NULL, packets bigint NOT NULL,
        PRIMARY KEY (exporter, interface, direction, bucket),
        CHECK (direction IN ('in','out')), CHECK (bytes >= 0 AND packets >= 0), CHECK (bucket % 300 = 0))''')
    op.execute('CREATE INDEX ix_traffic_samples_bucket ON traffic_samples(bucket)')
    op.execute('''CREATE TABLE traffic_interfaces (exporter varchar(200) NOT NULL, interface varchar(200) NOT NULL,
        description varchar(200) NOT NULL DEFAULT '', speed bigint NOT NULL DEFAULT 0, connectivity varchar(200) NOT NULL DEFAULT '',
        provider varchar(200) NOT NULL DEFAULT '', boundary varchar(20) NOT NULL DEFAULT 'undefined', last_seen bigint NOT NULL,
        PRIMARY KEY (exporter, interface))''')
    op.execute('''CREATE TABLE flow_exporters (name varchar(200) PRIMARY KEY, address varchar(45) NOT NULL,
        site varchar(200) NOT NULL DEFAULT '', role varchar(200) NOT NULL DEFAULT '', first_seen bigint NOT NULL, last_seen bigint NOT NULL)''')
    op.execute('''CREATE TABLE traffic_state (id varchar(30) PRIMARY KEY, through bigint, source_latest bigint,
        next_due bigint NOT NULL DEFAULT 0, job_id varchar(36) REFERENCES jobs(id), last_attempt_at bigint, last_success_at bigint,
        last_error varchar(80), failure_count integer NOT NULL DEFAULT 0, exporters_at bigint, retention_at bigint)''')


def downgrade():
    for table in ('traffic_state','flow_exporters','traffic_interfaces','traffic_samples'):op.drop_table(table)
