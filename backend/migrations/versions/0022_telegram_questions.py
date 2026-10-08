"""Telegram: questions asked from the NOC group, and the diagnosis message of each case thread."""
from alembic import op
revision='0022_telegram_questions'
down_revision='0021_telegram_topic'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE telegram_messages ADD COLUMN diagnosis_message_id bigint')
    op.execute('''CREATE TABLE telegram_questions (analysis_id varchar(36) PRIMARY KEY, chat_id bigint NOT NULL, message_id bigint NOT NULL,
        thread_id bigint, created_at bigint NOT NULL)''')


def downgrade():
    op.execute('DROP TABLE telegram_questions')
    op.execute('ALTER TABLE telegram_messages DROP COLUMN diagnosis_message_id')
