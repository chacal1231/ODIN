"""Optional MFA: new accounts start without it; accounts already enrolled keep it required."""
from alembic import op
revision='0032_optional_mfa'
down_revision='0031_noc_communication'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE users ADD COLUMN mfa_required boolean NOT NULL DEFAULT false')
    op.execute('UPDATE users SET mfa_required = true WHERE mfa_confirmed')


def downgrade():
    op.execute('ALTER TABLE users DROP COLUMN mfa_required')
