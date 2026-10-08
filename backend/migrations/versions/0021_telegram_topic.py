"""Telegram: the forum topic of the NOC group where cases are posted."""
from alembic import op
revision='0021_telegram_topic'
down_revision='0020_telegram'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE telegram_state ADD COLUMN thread_id bigint')


def downgrade():
    op.execute('ALTER TABLE telegram_state DROP COLUMN thread_id')
