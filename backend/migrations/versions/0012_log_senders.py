"""Graylog log senders: name and latest sending address, for confirmed inventory association."""
from alembic import op
revision='0012_log_senders'
down_revision='0011_withdrawn_events'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE log_senders (name varchar(200) PRIMARY KEY, address varchar(45) NOT NULL,
        first_seen bigint NOT NULL, last_seen bigint NOT NULL)''')


def downgrade():
    op.drop_table('log_senders')
