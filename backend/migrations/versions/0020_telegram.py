"""Telegram NOC group: linked users, bot state, case threads and pending corrections."""
from alembic import op
revision='0020_telegram'
down_revision='0019_case_roles'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE telegram_links (user_id varchar(36) PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        telegram_user_id bigint UNIQUE, code varchar(12) UNIQUE, code_expires_at bigint, linked_at bigint)''')
    op.execute('''CREATE TABLE telegram_state (id varchar(20) PRIMARY KEY, chat_id bigint, update_offset bigint NOT NULL DEFAULT 0,
        paused boolean NOT NULL DEFAULT false, heartbeat_at bigint, last_error varchar(80))''')
    op.execute('''CREATE TABLE telegram_messages (case_id varchar(36) PRIMARY KEY REFERENCES cases(id), chat_id bigint NOT NULL,
        message_id bigint NOT NULL, analysis_id varchar(36), recovered boolean NOT NULL DEFAULT false, notified_at bigint NOT NULL)''')
    op.execute('''CREATE TABLE telegram_prompts (message_id bigint PRIMARY KEY, chat_id bigint NOT NULL, analysis_id varchar(36) NOT NULL,
        user_id varchar(36) NOT NULL, verdict varchar(10) NOT NULL, created_at bigint NOT NULL)''')


def downgrade():
    for table in ('telegram_prompts', 'telegram_messages', 'telegram_state', 'telegram_links'):op.execute(f'DROP TABLE {table}')
