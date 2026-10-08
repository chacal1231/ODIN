"""Group checks into logical monitoring destinations without changing measurement epochs."""
from alembic import op
revision='0007_monitoring_destinations'
down_revision='0006_transport_probes'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("""CREATE TABLE monitoring_destinations (
        id varchar(36) PRIMARY KEY, name varchar(120) NOT NULL,
        scope varchar(20) NOT NULL, device_id varchar(36) REFERENCES devices(id),
        link_id varchar(36) REFERENCES inventory_links(id), version integer NOT NULL DEFAULT 1,
        CHECK(scope IN ('network','external')),
        CHECK(device_id IS NULL OR link_id IS NULL))""")
    op.execute("ALTER TABLE probe_targets ADD COLUMN destination_id varchar(36) REFERENCES monitoring_destinations(id), ADD COLUMN essential boolean NOT NULL DEFAULT true")
    op.execute("CREATE INDEX ix_probe_targets_destination_id ON probe_targets(destination_id)")
    op.execute("""INSERT INTO monitoring_destinations(id,name,scope,device_id)
        SELECT t.id,t.name,CASE WHEN t.categories::jsonb ? 'red_propia' THEN 'network' ELSE 'external' END,
        CASE WHEN t.categories::jsonb ? 'red_propia' THEN
          (SELECT min(d.id) FROM devices d WHERE NOT d.archived AND lower(d.name)=lower(t.name)
           HAVING count(*)=1) ELSE NULL END FROM probe_targets t""")
    op.execute('UPDATE probe_targets SET destination_id=id')


def downgrade():
    op.drop_column('probe_targets','essential')
    op.drop_column('probe_targets','destination_id')
    op.drop_table('monitoring_destinations')
