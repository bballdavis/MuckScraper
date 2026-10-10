"""add has_updates to edition_stories

Revision ID: c4f8b7a1d9e2
Revises: f12a3456bcde
Create Date: 2026-05-04 14:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4f8b7a1d9e2'
down_revision = 'f12a3456bcde'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('has_updates', sa.Boolean(), server_default='false', nullable=False))


def downgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.drop_column('has_updates')
