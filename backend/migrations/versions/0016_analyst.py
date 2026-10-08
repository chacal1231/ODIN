"""Analyst: model runs with their evidence pack, NOC reviews, private conversations and validated knowledge."""
from alembic import op
revision='0016_analyst'
down_revision='0015_cases'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE conversations (id varchar(36) PRIMARY KEY, owner_id varchar(36) NOT NULL, title varchar(200) NOT NULL,
        context json NOT NULL, created_at bigint NOT NULL, updated_at bigint NOT NULL)''')
    op.execute('CREATE INDEX ix_conversations_owner_id ON conversations(owner_id, updated_at)')
    op.execute('''CREATE TABLE analyses (id varchar(36) PRIMARY KEY, kind varchar(10) NOT NULL,
        case_id varchar(36) REFERENCES cases(id), conversation_id varchar(36) REFERENCES conversations(id) ON DELETE CASCADE,
        requested_by varchar(36) NOT NULL, created_at bigint NOT NULL, status varchar(10) NOT NULL DEFAULT 'pending',
        stage varchar(20) NOT NULL DEFAULT 'queued', progress json NOT NULL, cancel_requested boolean NOT NULL DEFAULT false,
        job_id varchar(36) REFERENCES jobs(id), started_at bigint, finished_at bigint, input_version varchar(120) NOT NULL DEFAULT '',
        pack json NOT NULL, result json, error varchar(80), model varchar(300), prompt_version integer NOT NULL DEFAULT 0,
        prompt_tokens integer, completion_tokens integer, duration_ms integer,
        CHECK (kind IN ('case','chat')), CHECK (status IN ('pending','running','done','failed','cancelled')))''')
    op.execute('CREATE INDEX ix_analyses_case_id ON analyses(case_id, created_at)')
    op.execute('CREATE INDEX ix_analyses_conversation_id ON analyses(conversation_id)')
    op.execute("CREATE INDEX ix_analyses_active ON analyses(status, created_at) WHERE status IN ('pending','running')")
    op.execute('''CREATE TABLE analysis_reviews (id varchar(36) PRIMARY KEY, analysis_id varchar(36) NOT NULL REFERENCES analyses(id),
        author_id varchar(36) NOT NULL, verdict varchar(10) NOT NULL, correction text NOT NULL DEFAULT '', created_at bigint NOT NULL,
        CHECK (verdict IN ('correct','partial','incorrect')))''')
    op.execute('CREATE INDEX ix_analysis_reviews_analysis_id ON analysis_reviews(analysis_id)')
    op.execute('''CREATE TABLE chat_messages (id varchar(36) PRIMARY KEY,
        conversation_id varchar(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role varchar(10) NOT NULL,
        content text NOT NULL, client_id varchar(64), analysis_id varchar(36), created_at bigint NOT NULL,
        UNIQUE (conversation_id, client_id), CHECK (role IN ('user','assistant')))''')
    op.execute('CREATE INDEX ix_chat_messages_conversation_id ON chat_messages(conversation_id, created_at)')
    op.execute('''CREATE TABLE knowledge (id varchar(36) PRIMARY KEY, title varchar(200) NOT NULL, body text NOT NULL,
        case_kind varchar(20), keywords json NOT NULL, source_case_id varchar(36), created_by varchar(36) NOT NULL,
        created_at bigint NOT NULL, updated_at bigint NOT NULL, archived boolean NOT NULL DEFAULT false)''')
    # Correlator rules v2 keep the first and the last context logs: recompute the evidence of every case.
    op.execute('UPDATE cases SET enriched_at = NULL')
    op.execute('''CREATE TABLE analyst_state (id varchar(20) PRIMARY KEY, last_success_at bigint, last_failure_at bigint,
        last_error varchar(80), model varchar(300), context integer, heartbeat_at bigint)''')


def downgrade():
    for table in ('analyst_state', 'knowledge', 'chat_messages', 'analysis_reviews', 'analyses', 'conversations'):
        op.execute(f'DROP TABLE {table}')
