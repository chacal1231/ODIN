"""Classification of probe destinations without changing measurement identity."""
from alembic import op
revision = '0005_target_categories'
down_revision = '0004_probes'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE probe_targets ADD COLUMN categories json NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE probe_targets ADD COLUMN service varchar(120) NOT NULL DEFAULT ''")
    op.execute("UPDATE probe_targets SET categories='[\"red_propia\"]'::json, service='PE' WHERE name ~* '^PE([-_[:space:]]|$)'")


def downgrade():
    op.drop_column('probe_targets','service')
    op.drop_column('probe_targets','categories')
