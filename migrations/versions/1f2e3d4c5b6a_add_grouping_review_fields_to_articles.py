"""add grouping review fields to articles

Revision ID: 1f2e3d4c5b6a
Revises: c9d8e7f6a5b4
Create Date: 2026-05-16 03:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '1f2e3d4c5b6a'
down_revision = 'c9d8e7f6a5b4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('grouping_match_method', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('grouping_confidence', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('grouping_candidate_story_ids', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('grouping_needs_review', sa.Boolean(), server_default='false', nullable=False))
        batch_op.add_column(sa.Column('grouping_reviewed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('grouping_reviewed_at')
        batch_op.drop_column('grouping_needs_review')
        batch_op.drop_column('grouping_candidate_story_ids')
        batch_op.drop_column('grouping_confidence')
        batch_op.drop_column('grouping_match_method')
