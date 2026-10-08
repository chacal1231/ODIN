"""BMP route monitoring: five-minute churn per router/peer/family, peer identities and routers."""
from alembic import op
revision='0014_bmp_routes'
down_revision='0013_traffic'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE route_samples (router varchar(45) NOT NULL, peer varchar(45) NOT NULL, afi integer NOT NULL,
        bucket bigint NOT NULL, updates bigint NOT NULL, withdrawals bigint NOT NULL, other bigint NOT NULL DEFAULT 0,
        prefixes bigint NOT NULL, PRIMARY KEY (router, peer, afi, bucket),
        CHECK (afi IN (1, 2)), CHECK (updates >= 0 AND withdrawals >= 0 AND other >= 0 AND prefixes >= 0), CHECK (bucket % 300 = 0))''')
    op.execute('CREATE INDEX ix_route_samples_bucket ON route_samples(bucket)')
    op.execute('''CREATE TABLE bmp_peers (router varchar(45) NOT NULL, peer varchar(45) NOT NULL, afi integer NOT NULL,
        asn varchar(20) NOT NULL DEFAULT '', as_name varchar(200) NOT NULL DEFAULT '', last_seen bigint NOT NULL,
        PRIMARY KEY (router, peer, afi))''')
    op.execute('CREATE TABLE bmp_routers (address varchar(45) PRIMARY KEY, first_seen bigint NOT NULL, last_seen bigint NOT NULL)')


def downgrade():
    for table in ('bmp_routers', 'bmp_peers', 'route_samples'):op.drop_table(table)
