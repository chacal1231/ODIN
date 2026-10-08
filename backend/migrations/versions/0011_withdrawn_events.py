"""Events whose source definition was deleted: neither open nor recovered."""
from alembic import op
revision='0011_withdrawn_events'
down_revision='0010_probe_batch_indexes'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE source_events DROP CONSTRAINT source_events_status_check')
    op.execute("ALTER TABLE source_events ADD CONSTRAINT source_events_status_check CHECK (status IN ('open','recovered','info','withdrawn'))")


def downgrade():
    op.execute("UPDATE source_events SET status='open' WHERE status='withdrawn'")
    op.execute('ALTER TABLE source_events DROP CONSTRAINT source_events_status_check')
    op.execute("ALTER TABLE source_events ADD CONSTRAINT source_events_status_check CHECK (status IN ('open','recovered','info'))")
