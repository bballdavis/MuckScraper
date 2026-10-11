"""Mark articles that carry the same text as another article in their story
(a republished piece, or the same wire copy run by two outlets).

Revision ID: c5d1e8a3f7b9
Revises: b3f7c1d9e5a2
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d1e8a3f7b9'
down_revision = 'b3f7c1d9e5a2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('articles', sa.Column('copy_of_id', sa.Integer(), nullable=True))
    op.add_column('articles', sa.Column('copy_overlap', sa.Float(), nullable=True))
    op.add_column('articles', sa.Column('copy_checked_at', sa.DateTime(), nullable=True))
    op.create_foreign_key(
        'articles_copy_of_id_fkey', 'articles', 'articles',
        ['copy_of_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_articles_copy_of_id', 'articles', ['copy_of_id'])


def downgrade():
    op.drop_index('ix_articles_copy_of_id', table_name='articles')
    op.drop_constraint('articles_copy_of_id_fkey', 'articles', type_='foreignkey')
    for column in ('copy_checked_at', 'copy_overlap', 'copy_of_id'):
        op.drop_column('articles', column)
