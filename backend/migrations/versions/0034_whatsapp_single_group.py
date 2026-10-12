"""WhatsApp has one group: carrier incidents go to the case thread of the NOC group, not to a separate providers group."""
from alembic import op
revision='0034_whatsapp_single_group'
down_revision='0033_whatsapp_providers'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE whatsapp_state DROP COLUMN providers_group_id, DROP COLUMN providers_group_name')
    # Threads opened in a separate providers group are not continued in the NOC group.
    op.execute('DELETE FROM whatsapp_provider_messages')


def downgrade():
    op.execute('ALTER TABLE whatsapp_state ADD COLUMN providers_group_id varchar(120), ADD COLUMN providers_group_name varchar(200)')
