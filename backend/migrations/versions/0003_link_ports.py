"""Physical link endpoints and trunk VLANs."""
from alembic import op
import sqlalchemy as sa

revision = '0003_link_ports'
down_revision = '0002_inventory'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('inventory_links', sa.Column('left_port_id', sa.String(36), nullable=True))
    op.add_column('inventory_links', sa.Column('right_port_id', sa.String(36), nullable=True))
    op.create_foreign_key('fk_link_left_port', 'inventory_links', 'inventory_interfaces', ['left_port_id'], ['id'])
    op.create_foreign_key('fk_link_right_port', 'inventory_links', 'inventory_interfaces', ['right_port_id'], ['id'])
    op.create_check_constraint('ck_link_port_pair', 'inventory_links', '(left_port_id IS NULL) = (right_port_id IS NULL)')
    op.add_column('inventory_links', sa.Column('vlans', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('inventory_links', 'vlans')
    op.drop_constraint('ck_link_port_pair', 'inventory_links')
    op.drop_constraint('fk_link_left_port', 'inventory_links')
    op.drop_constraint('fk_link_right_port', 'inventory_links')
    op.drop_column('inventory_links', 'left_port_id')
    op.drop_column('inventory_links', 'right_port_id')
