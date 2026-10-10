"""expand scrape telemetry fields

Revision ID: 7b3c5d9e1a2f
Revises: e91f4c2b7a6d
Create Date: 2026-05-09 02:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b3c5d9e1a2f'
down_revision = 'e91f4c2b7a6d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.alter_column(
            'scrape_method',
            existing_type=sa.String(length=64),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'scrape_failure_reason',
            existing_type=sa.String(length=255),
            type_=sa.String(length=1024),
            existing_nullable=True,
        )


def downgrade():
    with op.batch_alter_table('articles', schema=None) as batch_op:
        batch_op.alter_column(
            'scrape_failure_reason',
            existing_type=sa.String(length=1024),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'scrape_method',
            existing_type=sa.String(length=255),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
