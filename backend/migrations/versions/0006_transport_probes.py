"""TCP/HTTPS probes and versioned imported catalogs."""
from alembic import op
revision='0006_transport_probes'
down_revision='0005_target_categories'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("ALTER TABLE probe_targets ADD COLUMN protocol varchar(10) NOT NULL DEFAULT 'icmp', ADD COLUMN check_config json NOT NULL DEFAULT '{}'::json, ADD COLUMN dns_policy varchar(20) NOT NULL DEFAULT 'approved_ips', ADD COLUMN catalog_key varchar(200) UNIQUE, ADD COLUMN catalog_metadata json NOT NULL DEFAULT '{}'::json, ADD COLUMN validation json NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE probe_rounds ADD COLUMN details json NOT NULL DEFAULT '{}'::json")
    op.execute("CREATE TABLE probe_catalogs (id varchar(80) PRIMARY KEY, version integer NOT NULL, document json NOT NULL, digest varchar(64) NOT NULL, imported_at bigint NOT NULL)")


def downgrade():
    op.drop_table('probe_catalogs')
    op.drop_column('probe_rounds','details')
    for key in ('validation','catalog_metadata','catalog_key','dns_policy','check_config','protocol'):
        op.drop_column('probe_targets',key)
