"""Link Op/Ed pieces to stories and add story opinion roundups; seed the two
prompts that do it.

Revision ID: b3f7c1d9e5a2
Revises: a7d3e9f1c2b4
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision = 'b3f7c1d9e5a2'
down_revision = 'a7d3e9f1c2b4'
branch_labels = None
depends_on = None


OPINION_LINK_CHECK = """You are matching an opinion column to a news story.

Opinion column:
Title: {title}
Teaser: {teaser}

News story:
Headline: {headline}
Summary: {summary}

Is the opinion column mainly about the specific event or development in this news story (not just the same broad subject)? Answer with only YES or NO."""

OPINION_ROUNDUP = """You are writing a short "Opinion roundup" for a news site. Below is a news story and the opinion columns that were linked to it. Some columns come with their text; some come with only a headline and a teaser.

First, write one line per column in exactly this form:
<number>: ON | <one short phrase describing that column's own position>
or, if the column is not mainly about this news story:
<number>: OFF | <one short phrase saying what it is about instead>

Then write a line "ROUNDUP:" followed by ONE paragraph of 3 to 5 sentences that tells the reader what the ON columns argue and where they disagree.

Rules for the paragraph:
- Leave out every column you marked OFF.
- Describe each column by what it actually argues, not by the outlet's reputation. A column in a left-leaning outlet can argue a conservative point, and the other way round.
- Name the outlet when you describe an argument (for example "A Fox News column argues...").
- For a column marked HEADLINE ONLY, say only what its headline and teaser state. Do not guess at its argument.
- Do not take a side, do not add facts that are not in the columns, and do not repeat the news story itself.
- No bullet points, no headline, no markdown.

NEWS STORY: {headline}
{summary}

COLUMNS:
{columns}
"""

NEW_ROWS = [
    ("opinion_link_check",
     "Is this Op/Ed column about this news story? YES/NO, fast model. Used to link Op/Ed pieces to stories.",
     OPINION_LINK_CHECK),
    ("opinion_roundup",
     "The story page's opinion roundup: marks each linked column ON/OFF topic with a one-phrase position, then one paragraph.",
     OPINION_ROUNDUP),
]

prompt_templates = sa.table(
    "prompt_templates",
    sa.column("key", sa.String),
    sa.column("description", sa.String),
    sa.column("default_text", sa.Text),
    sa.column("current_text", sa.Text),
    sa.column("updated_at", sa.DateTime),
)


def upgrade():
    op.add_column('opinion_pieces', sa.Column('embedding', Vector(768), nullable=True))
    op.add_column('opinion_pieces', sa.Column('story_id', sa.Integer(), nullable=True))
    op.add_column('opinion_pieces', sa.Column('link_similarity', sa.Float(), nullable=True))
    op.add_column('opinion_pieces', sa.Column('link_checked_at', sa.DateTime(), nullable=True))
    op.add_column('opinion_pieces', sa.Column('full_text', sa.Text(), nullable=True))
    op.add_column('opinion_pieces', sa.Column('scrape_status', sa.String(32), nullable=True))
    op.add_column('opinion_pieces', sa.Column('stance', sa.Text(), nullable=True))
    op.create_foreign_key('opinion_pieces_story_id_fkey', 'opinion_pieces', 'stories',
                          ['story_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_opinion_pieces_story_id', 'opinion_pieces', ['story_id'])

    op.add_column('stories', sa.Column('opinion_roundup', sa.Text(), nullable=True))
    op.add_column('stories', sa.Column('opinion_roundup_generated_at', sa.DateTime(), nullable=True))

    conn = op.get_bind()
    existing = {row[0] for row in conn.execute(sa.text("SELECT key FROM prompt_templates"))}
    rows = [
        {"key": k, "description": d, "default_text": t, "current_text": t, "updated_at": None}
        for k, d, t in NEW_ROWS if k not in existing
    ]
    if rows:
        op.bulk_insert(prompt_templates, rows)


def downgrade():
    op.execute("DELETE FROM prompt_templates WHERE key IN ('opinion_link_check', 'opinion_roundup')")
    op.drop_column('stories', 'opinion_roundup_generated_at')
    op.drop_column('stories', 'opinion_roundup')
    op.drop_index('ix_opinion_pieces_story_id', table_name='opinion_pieces')
    op.drop_constraint('opinion_pieces_story_id_fkey', 'opinion_pieces', type_='foreignkey')
    for column in ('stance', 'scrape_status', 'full_text', 'link_checked_at',
                   'link_similarity', 'story_id', 'embedding'):
        op.drop_column('opinion_pieces', column)
