"""Add optional OIDC identity bindings without changing existing accounts.

Upstream grregis/MuckScraper#28 ships this as a4c6e8b0d2f5 on f7a1d3c5e8b2;
re-added here on the fork's chain.
"""
from alembic import op
import sqlalchemy as sa

revision = "a958c3be014a"
down_revision = "b6e2f9c4a8d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oidc_identities",
        sa.Column("identity_key", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    )


def downgrade():
    op.drop_table("oidc_identities")
