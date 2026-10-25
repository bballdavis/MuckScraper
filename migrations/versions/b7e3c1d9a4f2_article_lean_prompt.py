"""Rewrite the article lean prompt so Rate this article's lean stops calling
everything Center.

The old prompt asked for a bare number from the first 600 characters of an
article (its lede) and the fast model answered 3 for nearly every news story.
The new one says Center has to be earned, names what to look at, and asks for
a one-line reason after the score (news_fetcher/outlet_bias_llm.py reads both).

Revision ID: b7e3c1d9a4f2
Revises: e4a7c2f9b1d6
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e3c1d9a4f2'
down_revision = 'e4a7c2f9b1d6'
branch_labels = None
depends_on = None


DESCRIPTION = "An article's own lean (1-5) with a one-line reason, for Rate this article's lean. Fast model."

OUTLET_BIAS_BY_ARTICLE = """You are a media bias analyst. Rate the political lean of the news article below on this scale:
1 = Left
2 = Lean Left
3 = Center
4 = Lean Right
5 = Right

Judge the article itself, not the outlet that published it. Look at:
- Framing: whose problem, cause or solution the story is built around
- Word choice: loaded or charged terms for people, groups or policies
- Sourcing: whose voices are quoted, how prominently, and who gets the last word
- Selection: which facts or context are included, played up or left out

Center means the piece gives the competing sides comparable, neutral treatment. It is not the safe answer for an article that is merely written in a calm news style: if the framing, sourcing or emphasis favours one side, use 2 or 4, and 1 or 5 when it argues one side outright. Use 3 only when you can point to that balance.

Answer in exactly two lines, with no other text:
LEAN: <a single number from 1 to 5, or the word unknown if the text is too short or not political>
WHY: <one sentence naming the specific framing, wording or sourcing behind the rating>

Article:
{article_text}"""


def upgrade():
    conn = op.get_bind()
    # Only replace the live text where it was never customised; an edited
    # prompt is the owner's and keeps its text (and can still reset to this).
    conn.execute(
        sa.text(
            "UPDATE prompt_templates SET current_text = :t "
            "WHERE key = 'outlet_bias.by_article' AND current_text = default_text"
        ),
        {"t": OUTLET_BIAS_BY_ARTICLE},
    )
    conn.execute(
        sa.text("UPDATE prompt_templates SET default_text = :t, description = :d "
                "WHERE key = 'outlet_bias.by_article'"),
        {"t": OUTLET_BIAS_BY_ARTICLE, "d": DESCRIPTION},
    )


def downgrade():
    # The old prompt still parses (a bare number), so there is nothing to undo.
    pass
