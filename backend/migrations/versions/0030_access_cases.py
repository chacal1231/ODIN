"""Access cases: an OLT's PON outages, uplinks and reachability from SmartOLT open a case."""
from alembic import op
revision='0030_access_cases'
down_revision='0029_autonomous_systems'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE cases DROP CONSTRAINT cases_kind_check')
    op.execute("ALTER TABLE cases ADD CONSTRAINT cases_kind_check CHECK (kind IN ('bgp_session','interface','latency','access'))")


def downgrade():
    for table in ('case_evidence', 'case_notes'):
        op.execute(f"DELETE FROM {table} WHERE case_id IN (SELECT id FROM cases WHERE kind = 'access')")
    op.execute("DELETE FROM cases WHERE kind = 'access'")
    op.execute('ALTER TABLE cases DROP CONSTRAINT cases_kind_check')
    op.execute("ALTER TABLE cases ADD CONSTRAINT cases_kind_check CHECK (kind IN ('bgp_session','interface','latency'))")
