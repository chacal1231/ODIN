"""POPs, imported identities, reconciliation and synchronization history."""
from alembic import op
import sqlalchemy as sa

revision = '0002_inventory'
down_revision = '0001_base'
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column('version',sa.Integer(),nullable=False,server_default='1'),
                   sa.Column('allowed_networks',sa.JSON(),nullable=False,server_default='[]'),
                   sa.Column('auto_sync',sa.Boolean(),nullable=False,server_default=sa.false()),
                   sa.Column('checked_at',sa.BigInteger()), sa.Column('synced_at',sa.BigInteger()),
                   sa.Column('last_error',sa.String(80))]:
        op.add_column('integrations',column)
    op.create_table('pops',sa.Column('id',sa.String(36),primary_key=True),sa.Column('name',sa.String(120),nullable=False),sa.Column('description',sa.Text(),nullable=False),sa.Column('latitude',sa.Float()),sa.Column('longitude',sa.Float()),sa.Column('archived',sa.Boolean(),nullable=False),sa.Column('version',sa.Integer(),nullable=False),sa.Column('created_at',sa.BigInteger(),nullable=False),sa.CheckConstraint('(latitude IS NULL AND longitude IS NULL) OR (latitude IS NOT NULL AND longitude IS NOT NULL AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)',name='ck_pop_coordinates'))
    op.create_index('uq_pop_active_name','pops',[sa.text('lower(name)')],unique=True,postgresql_where=sa.text('NOT archived'))
    op.create_table('devices',sa.Column('id',sa.String(36),primary_key=True),sa.Column('name',sa.String(200),nullable=False),sa.Column('pop_id',sa.String(36),sa.ForeignKey('pops.id')),sa.Column('archived',sa.Boolean(),nullable=False),sa.Column('merged_into',sa.String(36),sa.ForeignKey('devices.id')),sa.Column('version',sa.Integer(),nullable=False),sa.Column('created_at',sa.BigInteger(),nullable=False))
    op.create_index('ix_devices_pop_id','devices',['pop_id'])
    op.create_table('source_identities',sa.Column('id',sa.String(36),primary_key=True),sa.Column('source',sa.String(30),sa.ForeignKey('integrations.kind'),nullable=False),sa.Column('external_id',sa.String(200),nullable=False),sa.Column('device_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('name',sa.String(200),nullable=False),sa.Column('addresses',sa.JSON(),nullable=False),sa.Column('details',sa.JSON(),nullable=False),sa.Column('last_seen',sa.BigInteger(),nullable=False),sa.Column('present',sa.Boolean(),nullable=False),sa.UniqueConstraint('source','external_id'))
    op.create_index('ix_source_identities_device_id','source_identities',['device_id'])
    op.create_table('inventory_interfaces',sa.Column('id',sa.String(36),primary_key=True),sa.Column('identity_id',sa.String(36),sa.ForeignKey('source_identities.id'),nullable=False),sa.Column('external_id',sa.String(200),nullable=False),sa.Column('name',sa.String(200),nullable=False),sa.Column('kind',sa.String(30),nullable=False),sa.Column('addresses',sa.JSON(),nullable=False),sa.Column('last_seen',sa.BigInteger(),nullable=False),sa.Column('present',sa.Boolean(),nullable=False),sa.UniqueConstraint('identity_id','external_id'))
    op.create_index('ix_inventory_interfaces_identity_id','inventory_interfaces',['identity_id'])
    op.create_table('inventory_assignments',sa.Column('id',sa.String(36),primary_key=True),sa.Column('device_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('previous_pop_id',sa.String(36),sa.ForeignKey('pops.id')),sa.Column('pop_id',sa.String(36),sa.ForeignKey('pops.id')),sa.Column('actor_id',sa.String(36),nullable=False),sa.Column('created_at',sa.BigInteger(),nullable=False),sa.Column('reason',sa.String(100),nullable=False))
    op.create_index('ix_inventory_assignments_device_id','inventory_assignments',['device_id'])
    op.create_table('reconciliation_candidates',sa.Column('id',sa.String(36),primary_key=True),sa.Column('left_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('right_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('evidence',sa.JSON(),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('resolved_by',sa.String(36)),sa.Column('resolved_at',sa.BigInteger()),sa.UniqueConstraint('left_id','right_id'))
    op.create_table('inventory_links',sa.Column('id',sa.String(36),primary_key=True),sa.Column('left_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('right_id',sa.String(36),sa.ForeignKey('devices.id'),nullable=False),sa.Column('description',sa.String(300),nullable=False),sa.Column('actor_id',sa.String(36),nullable=False),sa.Column('archived',sa.Boolean(),nullable=False),sa.Column('created_at',sa.BigInteger(),nullable=False))
    op.create_table('inventory_runs',sa.Column('id',sa.String(36),primary_key=True),sa.Column('source',sa.String(30),sa.ForeignKey('integrations.kind'),nullable=False),sa.Column('mode',sa.String(10),nullable=False),sa.Column('config_version',sa.Integer(),nullable=False),sa.Column('status',sa.String(20),nullable=False),sa.Column('created_at',sa.BigInteger(),nullable=False),sa.Column('finished_at',sa.BigInteger()),sa.Column('count',sa.Integer()),sa.Column('error_code',sa.String(80)),sa.Column('job_id',sa.String(36),sa.ForeignKey('jobs.id')))
    op.create_index('ix_inventory_runs_source','inventory_runs',['source'])


def downgrade():
    for name in ['inventory_runs','inventory_links','reconciliation_candidates','inventory_assignments','inventory_interfaces','source_identities','devices','pops']:
        op.drop_table(name)
    for name in ['last_error','synced_at','checked_at','auto_sync','allowed_networks','version']:
        op.drop_column('integrations',name)
