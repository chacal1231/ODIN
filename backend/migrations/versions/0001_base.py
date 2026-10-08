"""E1 identity, configuration and durable jobs."""
from alembic import op
import sqlalchemy as sa

revision = '0001_base'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('username', sa.String(80), nullable=False, unique=True),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('must_change_password', sa.Boolean(), nullable=False),
        sa.Column('mfa_secret', sa.Text()),
        sa.Column('mfa_confirmed', sa.Boolean(), nullable=False),
        sa.Column('last_totp_step', sa.BigInteger(), nullable=False),
        sa.Column('recovery_hashes', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'operator')", name='ck_user_role'))
    op.create_table('sessions',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('csrf_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('last_seen', sa.BigInteger(), nullable=False))
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])
    op.create_table('auth_challenges',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('csrf_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False))
    op.create_index('ix_auth_challenges_user_id', 'auth_challenges', ['user_id'])
    op.create_table('rate_buckets',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False))
    op.create_index('ix_rate_buckets_expires_at', 'rate_buckets', ['expires_at'])
    op.create_table('audit_log',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('actor_id', sa.String(36)),
        sa.Column('action', sa.String(80), nullable=False),
        sa.Column('target', sa.String(200)),
        sa.Column('created_at', sa.BigInteger(), nullable=False))
    op.create_index('ix_audit_log_created_at', 'audit_log', ['created_at'])
    op.create_table('integrations',
        sa.Column('kind', sa.String(30), primary_key=True),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('encrypted_secret', sa.Text()),
        sa.Column('updated_at', sa.BigInteger(), nullable=False))
    op.create_table('jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('kind', sa.String(50), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('idempotency_key', sa.String(200), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('run_after', sa.BigInteger(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_until', sa.BigInteger()),
        sa.Column('lease_token', sa.String(36)),
        sa.Column('result', sa.JSON()))
    op.create_index('ix_jobs_claim', 'jobs', ['status', 'run_after'])


def downgrade():
    for table in ['jobs', 'integrations', 'audit_log', 'rate_buckets', 'auth_challenges', 'sessions', 'users']:
        op.drop_table(table)
