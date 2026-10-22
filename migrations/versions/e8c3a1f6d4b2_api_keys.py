"""API keys: tokens an admin creates directly, beside paired devices.

Revision ID: e8c3a1f6d4b2
Revises: d2b7e5a9c4f1
Create Date: 2026-10-22

api_devices already holds every bearer token. `kind` tells a paired phone
("device") from a key made in Settings › Mobile API for a script, a server
or another app ("key"). Both authenticate, rate-limit and sign out the same.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e8c3a1f6d4b2'
down_revision = 'd2b7e5a9c4f1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_devices", sa.Column("kind", sa.String(16), nullable=False, server_default="device"))


def downgrade():
    op.drop_column("api_devices", "kind")
