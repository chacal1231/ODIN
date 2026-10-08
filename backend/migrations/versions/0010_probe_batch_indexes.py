"""Index active probe batches; the claim loop must not scan finished history."""
from alembic import op
revision='0010_probe_batch_indexes'
down_revision='0009_event_collectors'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("CREATE INDEX ix_probe_batches_active ON probe_batches(status,lease_until) WHERE status IN ('pending','running')")
    op.execute('CREATE INDEX ix_probe_batches_finished ON probe_batches(finished_at)')


def downgrade():
    op.drop_index('ix_probe_batches_finished',table_name='probe_batches')
    op.drop_index('ix_probe_batches_active',table_name='probe_batches')
