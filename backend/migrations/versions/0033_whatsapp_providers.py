"""WhatsApp providers group: carrier incidents in their own thread, as Telegram's /proveedores group."""
from alembic import op
revision='0033_whatsapp_providers'
down_revision='0032_optional_mfa'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE whatsapp_state ADD COLUMN providers_group_id varchar(120), ADD COLUMN providers_group_name varchar(200)')
    op.execute('''CREATE TABLE whatsapp_provider_messages (
        case_id varchar(36) PRIMARY KEY REFERENCES cases(id),
        group_id varchar(120) NOT NULL,
        message_id varchar(200) NOT NULL,
        recovered boolean NOT NULL DEFAULT false,
        closed boolean NOT NULL DEFAULT false,
        notified_at bigint NOT NULL)''')


def downgrade():
    op.execute('DROP TABLE whatsapp_provider_messages')
    op.execute('ALTER TABLE whatsapp_state DROP COLUMN providers_group_id, DROP COLUMN providers_group_name')
