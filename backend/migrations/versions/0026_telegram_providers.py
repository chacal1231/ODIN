"""Internal Telegram group for carrier incidents: one thread per case, replies kept as case notes."""
from alembic import op
revision='0026_telegram_providers'
down_revision='0025_model_providers'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE telegram_state ADD COLUMN providers_chat_id bigint, ADD COLUMN providers_thread_id bigint')
    op.execute('''CREATE TABLE telegram_provider_messages (case_id varchar(36) PRIMARY KEY REFERENCES cases(id),
        chat_id bigint NOT NULL, message_id bigint NOT NULL, recovered boolean NOT NULL, closed boolean NOT NULL, notified_at bigint NOT NULL)''')
    op.execute('CREATE INDEX ix_telegram_provider_messages_message_id ON telegram_provider_messages(message_id)')


def downgrade():
    op.drop_table('telegram_provider_messages')
    op.execute('ALTER TABLE telegram_state DROP COLUMN providers_chat_id, DROP COLUMN providers_thread_id')
