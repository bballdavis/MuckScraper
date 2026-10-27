"""LLM spending caps: a ledger of paid LLM calls and what they cost.

llm_usage holds one row per call to Anthropic, Gemini, Groq or OpenRouter,
so news_fetcher/llm_spend.py can hold each provider to its daily and
monthly caps (Settings › LLM providers).
"""
from alembic import op
import sqlalchemy as sa

revision = "f3c8a1d6e2b9"
down_revision = "e5d2b8a4c6f1"
branch_labels = None
depends_on = None


def upgrade():
    # create_app() and the scheduler run db.create_all(), so the table can
    # already exist on a running install; create it only when it doesn't.
    if "llm_usage" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("called_at", sa.DateTime(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("priced", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_llm_usage_provider_called_at", "llm_usage", ["provider", "called_at"])


def downgrade():
    op.drop_index("ix_llm_usage_provider_called_at", table_name="llm_usage")
    op.drop_table("llm_usage")
