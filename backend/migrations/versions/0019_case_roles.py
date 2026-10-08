"""Correlator rules v4: cases keep what their neighbor or interface is; recompute every session and interface case."""
from alembic import op
revision='0019_case_roles'
down_revision='0018_interface_descriptions'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("UPDATE cases SET enriched_at = NULL WHERE kind IN ('bgp_session', 'interface')")


def downgrade():
    pass
