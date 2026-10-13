"""Access: history of ONUs down per PON, and the significance that tells a common failure from scattered customers."""
from alembic import op
revision='0035_access_history'
down_revision='0034_whatsapp_single_group'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE pon_samples (olt_id varchar(40) NOT NULL, board varchar(10) NOT NULL, port varchar(10) NOT NULL, at bigint NOT NULL,
        onus integer NOT NULL, los integer NOT NULL DEFAULT 0, power integer NOT NULL DEFAULT 0, offline integer NOT NULL DEFAULT 0,
        PRIMARY KEY (olt_id, board, port, at))''')
    op.execute('CREATE INDEX ix_pon_samples_at ON pon_samples(at)')
    op.execute('ALTER TABLE correlation_state ADD COLUMN access_significance double precision NOT NULL DEFAULT 0.0001')


def downgrade():
    op.execute('ALTER TABLE correlation_state DROP COLUMN access_significance')
    op.execute('DROP TABLE pon_samples')
