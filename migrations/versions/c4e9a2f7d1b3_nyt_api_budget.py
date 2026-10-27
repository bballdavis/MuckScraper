"""NYT API budget: a ledger of requests and the Times Wire items they brought.

nyt_api_calls replaces the per-process daily counter, so the 500-a-day key
limit holds across restarts and both containers; nyt_wire_items holds what
the Times Wire returns, so a NYT article's text costs no request of its own.
"""
from alembic import op
import sqlalchemy as sa

revision = "c4e9a2f7d1b3"
down_revision = "a958c3be014a"
branch_labels = None
depends_on = None


def upgrade():
    # create_app() and the scheduler run db.create_all(), so on a running
    # install either table can already exist; create only what is missing.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "nyt_api_calls" not in existing:
        _create_api_calls()
    if "nyt_wire_items" not in existing:
        _create_wire_items()


def _create_api_calls():
    op.create_table(
        "nyt_api_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("called_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("items", sa.Integer(), nullable=True),
    )
    op.create_index("ix_nyt_api_calls_called_at", "nyt_api_calls", ["called_at"])


def _create_wire_items():
    op.create_table(
        "nyt_wire_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("url", sa.String(2000), nullable=False, unique=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("lead_paragraph", sa.Text(), nullable=True),
        sa.Column("section", sa.String(100), nullable=True),
        sa.Column("subsection", sa.String(100), nullable=True),
        sa.Column("byline", sa.String(300), nullable=True),
        sa.Column("item_type", sa.String(50), nullable=True),
        sa.Column("image_url", sa.String(2000), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("searched_at", sa.DateTime(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_nyt_wire_items_published_at", "nyt_wire_items", ["published_at"])


def downgrade():
    op.drop_index("ix_nyt_wire_items_published_at", table_name="nyt_wire_items")
    op.drop_table("nyt_wire_items")
    op.drop_index("ix_nyt_api_calls_called_at", table_name="nyt_api_calls")
    op.drop_table("nyt_api_calls")
