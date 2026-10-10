"""add opinion_pieces table (Op/Ed bucket)

Revision ID: a7d3e9f1c2b4
Revises: f2a6c9e4b7d1
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d3e9f1c2b4'
down_revision = 'f2a6c9e4b7d1'
branch_labels = None
depends_on = None


def upgrade():
    # create_app() runs db.create_all(), so on a running install the table can
    # already exist by the time this migration runs.
    if sa.inspect(op.get_bind()).has_table('opinion_pieces'):
        return
    op.create_table(
        'opinion_pieces',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('url', sa.String(), nullable=False, unique=True),
        sa.Column('source', sa.String()),
        sa.Column('outlet_id', sa.Integer(), sa.ForeignKey('outlets.id', ondelete='SET NULL')),
        sa.Column('bias_score', sa.Float()),
        sa.Column('date', sa.DateTime()),
        sa.Column('fetched_at', sa.DateTime()),
        sa.Column('content', sa.Text()),
        sa.Column('image_url', sa.String()),
    )
    op.create_index('ix_opinion_pieces_date', 'opinion_pieces', ['date'])
    op.create_index('ix_opinion_pieces_outlet_id', 'opinion_pieces', ['outlet_id'])


def downgrade():
    op.drop_table('opinion_pieces')
