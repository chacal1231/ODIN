"""Analyst model by API (DeepSeek) besides the local llama-server: integrations name the model to ask for."""
from alembic import op
revision='0025_model_providers'
down_revision='0024_probe_traces'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE integrations ADD COLUMN model varchar(120)')


def downgrade():
    op.execute("DELETE FROM integrations WHERE kind = 'deepseek'")
    op.execute('ALTER TABLE integrations DROP COLUMN model')
