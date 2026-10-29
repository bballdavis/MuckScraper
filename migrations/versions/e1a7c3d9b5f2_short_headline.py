"""Stories get a short headline for tight spots (the iOS small widget), and
its prompt is seeded (2026-10-29).

Revision ID: e1a7c3d9b5f2
Revises: d9f2b6a4c1e8
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1a7c3d9b5f2'
down_revision = 'd9f2b6a4c1e8'
branch_labels = None
depends_on = None


HEADLINE_SHORT = """You are a wire service editor shortening a headline so it fits a small phone widget.

Rewrite the headline below in at most {max_chars} characters, keeping its meaning.

Rules:
- Keep names, places and numbers exactly as written. Do not add any fact, number or name that is not in the headline.
- Drop filler words and secondary detail first; keep who did what.
- One line, no quotation marks, no trailing period, no label or commentary.
- Output only the headline.

HEADLINE: {headline}
"""

prompt_templates = sa.table(
    "prompt_templates",
    sa.column("key", sa.String),
    sa.column("description", sa.String),
    sa.column("default_text", sa.Text),
    sa.column("current_text", sa.Text),
    sa.column("updated_at", sa.DateTime),
)

DESCRIPTION = "A shorter rewrite of a long headline for tight spots such as the iOS widget. Names and numbers kept exactly."


def upgrade():
    op.add_column("stories", sa.Column("short_headline", sa.String(length=160), nullable=True))
    op.add_column("stories", sa.Column("short_headline_source", sa.Text(), nullable=True))
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM prompt_templates WHERE key = 'headline_short'")).first():
        return
    op.bulk_insert(prompt_templates, [{
        "key": "headline_short",
        "description": DESCRIPTION,
        "default_text": HEADLINE_SHORT,
        "current_text": HEADLINE_SHORT,
        "updated_at": None,
    }])


def downgrade():
    op.execute("DELETE FROM prompt_templates WHERE key = 'headline_short'")
    op.drop_column("stories", "short_headline_source")
    op.drop_column("stories", "short_headline")
