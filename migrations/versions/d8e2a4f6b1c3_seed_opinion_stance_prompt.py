"""Seed the opinion_stance prompt: a one-phrase position for every Op/Ed
column linked to a story, for the edition Opinion pages (2026-10-17).

Revision ID: d8e2a4f6b1c3
Revises: c5d1e8a3f7b9
Create Date: 2026-10-17
"""
from alembic import op
import sqlalchemy as sa


revision = 'd8e2a4f6b1c3'
down_revision = 'c5d1e8a3f7b9'
branch_labels = None
depends_on = None


OPINION_STANCE = """You are describing one opinion column for a news site. Below are a news story and an opinion column that was linked to it. The column comes with its text, or with only a headline and a teaser.

Answer with exactly one line, in one of these two forms:
ON | <one short phrase describing the column's own position on this story>
OFF | <one short phrase saying what the column is about instead>

Use OFF when the column is not mainly about this news story.

Rules for the phrase:
- Describe what the column actually argues, not the outlet's reputation. A column in a left-leaning outlet can argue a conservative point, and the other way round.
- If the column is marked HEADLINE ONLY, say only what its headline and teaser state. Do not guess at its argument.
- Do not take a side and do not add facts that are not in the column.
- At most 20 words. No quotation marks around it, no markdown.

NEWS STORY: {headline}
{summary}

COLUMN:
{column}
"""

prompt_templates = sa.table(
    "prompt_templates",
    sa.column("key", sa.String),
    sa.column("description", sa.String),
    sa.column("default_text", sa.Text),
    sa.column("current_text", sa.Text),
    sa.column("updated_at", sa.DateTime),
)


def upgrade():
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM prompt_templates WHERE key = 'opinion_stance'")).first():
        return
    op.bulk_insert(prompt_templates, [{
        "key": "opinion_stance",
        "description": "One-phrase position for an Op/Ed column linked to a story (ON/OFF topic), for columns the roundup didn't describe. Shown on the edition's Opinion page.",
        "default_text": OPINION_STANCE,
        "current_text": OPINION_STANCE,
        "updated_at": None,
    }])


def downgrade():
    op.execute("DELETE FROM prompt_templates WHERE key = 'opinion_stance'")
