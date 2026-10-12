"""Adaptive thresholds: weekly baselines per check and the thresholds they propose, applied without restarting series."""
from alembic import op
revision='0028_adaptive_thresholds'
down_revision='0027_smartolt'
branch_labels=None
depends_on=None


def upgrade():
    # Existing checks start in 'proposed': ODIN computes, an administrator approves.
    op.execute("ALTER TABLE probe_targets ADD COLUMN threshold_mode varchar(10) NOT NULL DEFAULT 'proposed', ADD COLUMN adaptive json NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE probe_targets ADD CONSTRAINT ck_probe_targets_threshold_mode CHECK (threshold_mode IN ('manual','proposed','auto'))")
    op.execute('''CREATE TABLE probe_baselines (id varchar(36) PRIMARY KEY, target_id varchar(36) NOT NULL REFERENCES probe_targets(id),
        computed_at bigint NOT NULL, protocol varchar(10) NOT NULL, status varchar(20) NOT NULL, days double precision NOT NULL,
        coverage double precision NOT NULL, windows integer NOT NULL, metrics json NOT NULL, proposal json NOT NULL, flags json NOT NULL,
        applied_at bigint, applied_by varchar(36), CHECK (status IN ('insufficient','provisional','ready')))''')
    op.execute('CREATE INDEX ix_probe_baselines_target ON probe_baselines(target_id, computed_at)')


def downgrade():
    op.drop_table('probe_baselines')
    op.execute('ALTER TABLE probe_targets DROP CONSTRAINT ck_probe_targets_threshold_mode')
    op.drop_column('probe_targets','adaptive')
    op.drop_column('probe_targets','threshold_mode')
