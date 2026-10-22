"""Add the Mobile API tables: paired devices, one-time pairing codes and
rate-limit counters.

Revision ID: e4c9a2f7b3d1
Revises: d3b8f6a1e2c9
Create Date: 2026-10-22

Device tokens and pairing codes are stored only as SHA-256 hashes
(aggregator/api/auth.py).
"""
from alembic import op
import sqlalchemy as sa

revision = 'e4c9a2f7b3d1'
down_revision = 'd3b8f6a1e2c9'
branch_labels = None
depends_on = None

_NOW = sa.text("(now() at time zone 'utc')")


def upgrade():
    # create_app() and the scheduler run db.create_all(), so on a running
    # install the tables can already exist by the time this migration runs.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "api_devices" not in tables:
        op.create_table(
            "api_devices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("scopes", sa.String(length=200), nullable=False, server_default="read"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("last_ip", sa.String(length=64), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_api_devices_user_id", "api_devices", ["user_id"])
    if "api_pairing_codes" not in tables:
        op.create_table(
            "api_pairing_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("scopes", sa.String(length=200), nullable=False, server_default="read"),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_NOW),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("device_id", sa.Integer(), sa.ForeignKey("api_devices.id", ondelete="SET NULL"), nullable=True),
        )
    if "api_rate_counters" not in tables:
        op.create_table(
            "api_rate_counters",
            sa.Column("key", sa.String(length=120), primary_key=True),
            sa.Column("window_start", sa.DateTime(), primary_key=True),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    op.drop_table("api_rate_counters")
    op.drop_table("api_pairing_codes")
    op.drop_table("api_devices")
