"""Latency cases: a latency incident opens a case like a session or interface change does."""
from alembic import op
revision='0017_latency_cases'
down_revision='0016_analyst'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE cases DROP CONSTRAINT cases_kind_check')
    op.execute("ALTER TABLE cases ADD CONSTRAINT cases_kind_check CHECK (kind IN ('bgp_session','interface','latency'))")


def downgrade():
    op.execute("DELETE FROM case_evidence WHERE case_id IN (SELECT id FROM cases WHERE kind = 'latency')")
    op.execute("DELETE FROM cases WHERE kind = 'latency'")
    op.execute('ALTER TABLE cases DROP CONSTRAINT cases_kind_check')
    op.execute("ALTER TABLE cases ADD CONSTRAINT cases_kind_check CHECK (kind IN ('bgp_session','interface'))")
