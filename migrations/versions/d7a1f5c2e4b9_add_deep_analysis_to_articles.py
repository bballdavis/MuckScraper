"""add deep_analysis to articles

Revision ID: d7a1f5c2e4b9
Revises: c4f8b7a1d9e2
Create Date: 2026-05-04 15:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7a1f5c2e4b9'
down_revision = 'c4f8b7a1d9e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deep_analysis', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('deep_analysis')
