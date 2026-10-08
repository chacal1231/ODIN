"""Port descriptions (ifAlias): with the port addresses they tell what each BGP neighbor is."""
from alembic import op
revision='0018_interface_descriptions'
down_revision='0017_latency_cases'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("ALTER TABLE inventory_interfaces ADD COLUMN description varchar(500) NOT NULL DEFAULT ''")


def downgrade():
    op.execute('ALTER TABLE inventory_interfaces DROP COLUMN description')
