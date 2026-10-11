"""Traceroutes (mtr) towards monitored destinations: baseline every 15 minutes, on incidents and on demand."""
from alembic import op
revision='0024_probe_traces'
down_revision='0023_isp_scope'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE probe_traces (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id),
        address varchar(45) NOT NULL, protocol varchar(10) NOT NULL, port integer, reason varchar(20) NOT NULL,
        incident_id varchar(36) REFERENCES probe_incidents(id), actor_id varchar(36), slot bigint NOT NULL,
        status varchar(20) NOT NULL, token varchar(36), lease_until bigint, finished_at bigint, error_code varchar(80),
        reached boolean, hops json NOT NULL, path json NOT NULL, fingerprint varchar(64), path_changed boolean,
        CHECK (reason IN ('baseline','incident_opened','incident_recovered','manual')), CHECK (protocol IN ('icmp','tcp')))''')
    op.execute('CREATE INDEX ix_probe_traces_target ON probe_traces(target_id, slot)')
    op.execute('CREATE INDEX ix_probe_traces_incident_id ON probe_traces(incident_id)')
    op.execute("CREATE INDEX ix_probe_traces_active ON probe_traces(status, slot) WHERE status IN ('pending','running')")
    # One baseline per check and 15-minute slot, whoever schedules it.
    op.execute("CREATE UNIQUE INDEX uq_probe_traces_baseline ON probe_traces(target_id, slot) WHERE reason = 'baseline'")


def downgrade():
    op.drop_table('probe_traces')
