"""ISP edition: without Akvorado traffic or BMP routes; their tables, integrations and jobs go."""
from alembic import op
revision='0023_isp_scope'
down_revision='0022_telegram_questions'
branch_labels=None
depends_on=None

SOURCES = "('clickhouse_flows', 'clickhouse_bmp')"


def upgrade():
    for table in ('peer_interfaces', 'traffic_state', 'flow_exporters', 'traffic_interfaces', 'traffic_samples',
                  'bmp_routers', 'bmp_peers', 'route_samples'):
        op.drop_table(table)
    op.execute("DELETE FROM jobs WHERE kind IN ('traffic_poll', 'bmp_poll')")
    op.execute(f'DELETE FROM source_identities WHERE source IN {SOURCES}')
    op.execute(f'DELETE FROM integrations WHERE kind IN {SOURCES}')


def downgrade():
    raise NotImplementedError('La edición ISP no recupera las tablas de tráfico ni de BMP')
