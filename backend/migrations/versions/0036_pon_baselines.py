"""What is normal for each PON by hour of the day, learned from its history of ONUs down."""
from alembic import op
revision='0036_pon_baselines'
down_revision='0035_access_history'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE pon_baselines (olt_id varchar(40) NOT NULL, board varchar(10) NOT NULL, port varchar(10) NOT NULL,
        cause varchar(20) NOT NULL, hour integer NOT NULL, rate double precision NOT NULL, days integer NOT NULL, computed_at bigint NOT NULL,
        PRIMARY KEY (olt_id, board, port, cause, hour))''')


def downgrade():
    op.execute('DROP TABLE pon_baselines')
