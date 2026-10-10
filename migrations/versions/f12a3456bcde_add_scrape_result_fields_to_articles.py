"""add scrape result fields to article

Revision ID: f12a3456bcde
Revises: 036ef249a62b
Create Date: 2026-05-04 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f12a3456bcde'
down_revision = '036ef249a62b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scrape_status', sa.String(length=32), server_default='pending', nullable=False))
        batch_op.add_column(sa.Column('scrape_method', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('scrape_failure_reason', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('scrape_http_status', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.drop_column('scrape_http_status')
        batch_op.drop_column('scrape_failure_reason')
        batch_op.drop_column('scrape_method')
        batch_op.drop_column('scrape_status')
