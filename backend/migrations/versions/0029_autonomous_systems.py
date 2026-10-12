"""Names and organizations of the autonomous systems seen in traces."""
from alembic import op
revision='0029_autonomous_systems'
down_revision='0028_adaptive_thresholds'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE autonomous_systems (asn bigint PRIMARY KEY, name varchar(300), org varchar(80), display varchar(120),
        country varchar(4), resolved_at bigint NOT NULL)''')


def downgrade():
    op.drop_table('autonomous_systems')
