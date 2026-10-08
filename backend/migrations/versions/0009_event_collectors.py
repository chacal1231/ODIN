"""Read-only source collectors and explicit snapshot ordering."""
from alembic import op
revision='0009_event_collectors'
down_revision='0008_source_events'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("ALTER TABLE integrations ADD COLUMN events_enabled boolean NOT NULL DEFAULT false, ADD COLUMN events_timezone varchar(80)")
    op.execute("""ALTER TABLE source_events
        ADD COLUMN condition_key varchar(200), ADD COLUMN original_state varchar(40) NOT NULL DEFAULT '',
        ADD COLUMN time_basis varchar(20) NOT NULL DEFAULT 'source',
        ADD COLUMN observation_sequence bigint NOT NULL DEFAULT 0,
        ADD CONSTRAINT ck_event_time_basis CHECK(time_basis IN ('source','snapshot'))""")
    op.execute('CREATE INDEX ix_source_events_condition ON source_events(stream_id,condition_key,occurred_at)')
    op.execute("""CREATE TABLE event_collectors (
        id varchar(60) PRIMARY KEY, source varchar(30) NOT NULL REFERENCES integrations(kind),
        topic varchar(30) NOT NULL, stream_name varchar(80) NOT NULL,
        config_version integer NOT NULL, next_due bigint NOT NULL,
        job_id varchar(36) REFERENCES jobs(id), last_attempt_at bigint, last_success_at bigint,
        last_error varchar(80), failure_count integer NOT NULL DEFAULT 0,
        last_count integer NOT NULL DEFAULT 0, UNIQUE(source,topic))""")


def downgrade():
    op.drop_table('event_collectors')
    op.drop_index('ix_source_events_condition',table_name='source_events')
    op.execute('ALTER TABLE source_events DROP CONSTRAINT ck_event_time_basis')
    for col in ['condition_key','original_state','time_basis','observation_sequence']:op.drop_column('source_events',col)
    op.drop_column('integrations','events_enabled');op.drop_column('integrations','events_timezone')
