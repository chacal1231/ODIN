"""Source-neutral events, transactional page receipts and checkpoints."""
from alembic import op
revision = '0008_source_events'
down_revision = '0007_monitoring_destinations'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE event_streams (
        id varchar(36) PRIMARY KEY, source varchar(30) NOT NULL, name varchar(80) NOT NULL,
        version integer NOT NULL DEFAULT 0, cursor varchar(2048),
        coverage varchar(20) NOT NULL DEFAULT 'unavailable', reason varchar(80),
        window_start bigint, window_end bigint, last_attempt_at bigint, last_success_at bigint,
        last_event_at bigint, complete_through bigint, UNIQUE(source,name),
        CHECK (coverage IN ('complete','partial','unavailable','unsupported')),
        CHECK (version >= 0))''')
    op.execute('''CREATE TABLE source_events (
        id varchar(36) PRIMARY KEY, stream_id varchar(36) NOT NULL REFERENCES event_streams(id),
        external_id varchar(200) NOT NULL, identity_kind varchar(20) NOT NULL,
        entity_id varchar(200), device_id varchar(36) REFERENCES devices(id),
        occurred_at bigint NOT NULL, source_updated_at bigint NOT NULL,
        received_at bigint NOT NULL, last_received_at bigint NOT NULL,
        kind varchar(80) NOT NULL, status varchar(20) NOT NULL, severity varchar(20) NOT NULL,
        original_severity varchar(80) NOT NULL, title varchar(500) NOT NULL,
        recovery_of varchar(200), reference varchar(300) NOT NULL, content_hash varchar(64) NOT NULL,
        UNIQUE(stream_id,external_id),
        CHECK (status IN ('open','recovered','info')),
        CHECK (severity IN ('info','warning','critical','unknown')),
        CHECK (identity_kind IN ('source_id','fingerprint')),
        CHECK (source_updated_at >= occurred_at))''')
    op.execute('CREATE INDEX ix_source_events_device_id ON source_events(device_id)')
    op.execute('CREATE INDEX ix_source_events_time ON source_events(occurred_at,id)')
    op.execute('CREATE INDEX ix_source_events_entity_time ON source_events(entity_id,occurred_at)')
    op.execute('''CREATE TABLE event_pages (
        id varchar(36) PRIMARY KEY, stream_id varchar(36) NOT NULL REFERENCES event_streams(id),
        request_key varchar(100) NOT NULL, content_hash varchar(64) NOT NULL,
        coverage varchar(20) NOT NULL, reason varchar(80), window_start bigint NOT NULL,
        window_end bigint NOT NULL, event_count integer NOT NULL, changed_count integer NOT NULL,
        received_at bigint NOT NULL, checkpoint_version integer NOT NULL,
        UNIQUE(stream_id,request_key), CHECK(window_end >= window_start))''')

    op.execute('CREATE INDEX ix_event_pages_version ON event_pages(stream_id,checkpoint_version)')


def downgrade():
    op.drop_table('event_pages')
    op.drop_table('source_events')
    op.drop_table('event_streams')
