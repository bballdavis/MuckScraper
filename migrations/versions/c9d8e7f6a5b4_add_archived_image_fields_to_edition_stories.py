"""add archived image fields to edition stories

Revision ID: c9d8e7f6a5b4
Revises: 7b3c5d9e1a2f
Create Date: 2026-05-11 22:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d8e7f6a5b4'
down_revision = '7b3c5d9e1a2f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_image_path', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('source_image_url', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('image_credit_text', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('image_download_status', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('image_downloaded_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('image_width', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('image_height', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('image_bytes', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.drop_column('image_bytes')
        batch_op.drop_column('image_height')
        batch_op.drop_column('image_width')
        batch_op.drop_column('image_downloaded_at')
        batch_op.drop_column('image_download_status')
        batch_op.drop_column('image_credit_text')
        batch_op.drop_column('source_image_url')
        batch_op.drop_column('archived_image_path')
