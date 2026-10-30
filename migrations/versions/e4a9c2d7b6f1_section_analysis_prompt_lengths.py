"""Section analysis prompts ask for long sections first and one or two
sentences in the short ones, so the story page and the iOS app can stack the
long ones and put the short ones side by side; business moves its analysts
section after the broader picture (Philip, 2026-10-29).

Only prompts that were never edited are replaced: an edited prompt keeps its
text and can still reset to the new default.

Revision ID: e4a9c2d7b6f1
Revises: e1a7c3d9b5f2
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'e4a9c2d7b6f1'
down_revision = 'e1a7c3d9b5f2'
branch_labels = None
depends_on = None

DEEP_REPORT_POLITICS = 'You are an experienced media analyst writing a detailed report on how different news outlets are covering the same political story.\n\nBelow are articles from the current source set, grouped by available outlet bias.\n\nSource availability:\n{source_availability}\n\n{combined}\n\nWrite a detailed analytical report using this EXACT format:\n\nThe story: [2-3 sentences explaining what happened factually]\n\nHow the left is covering it: [Only describe left-leaning coverage if left-leaning sources are listed above. If no left-leaning sources are listed, write exactly: "No left-leaning sources were found in the current coverage."]\n\nHow the center is covering it: [Only describe center coverage if center sources are listed above. If no center sources are listed, write exactly: "No center sources were found in the current coverage."]\n\nHow the right is covering it: [Only describe right-leaning coverage if right-leaning sources are listed above. If no right-leaning sources are listed, write exactly: "No right-leaning sources were found in the current coverage."]\n\nWhat\'s contested: [One or two sentences on where the different sides disagree most sharply, what facts or framings are in dispute]\n\nWhat\'s missing: [One or two sentences on what angles or perspectives seem absent from the coverage, what questions aren\'t being asked]\n\nWhat\'s next: [One sentence on what to watch for]\n\nRules:\n- Use EXACTLY the labels shown above including the colon\n- Be specific about framing differences, not just topic differences\n- Keep the three coverage sections to 2-4 sentences each, and the other sections after them to one or two sentences\n- Do not infer, invent, or speculate about how a missing source bucket would cover the story\n- If a source bucket has no listed articles, use the exact "No ... sources were found" sentence for that section\n- Stay neutral and analytical in your own voice\n- No markdown, no extra formatting\n- Do not add any text before or after the structure above'

DEEP_REPORT_SCIENCE = 'You are a science journalist writing a detailed report on a scientific or technology development.\n\nBelow are articles covering the same story:\n\n{combined}\n\nWrite a detailed analytical report using this EXACT format:\n\nThe discovery or development: [2-3 sentences explaining what happened or was discovered factually]\n\nWhy it matters: [The scientific or technological significance — what does this change or enable?]\n\nWhat the research shows: [Key findings, data points, or technical details from the coverage]\n\nReal world impact: [How this affects people, industries, or society in practical terms]\n\nWhat experts are saying: [One or two sentences of notable quotes or expert opinions from the coverage. If none available, say "Expert commentary not available in current coverage."]\n\nWhat\'s still unknown: [One or two sentences on open questions, limitations of the research, or what needs further study]\n\nWhat\'s next: [One sentence on upcoming developments or what to watch for]\n\nRules:\n- Use EXACTLY the labels shown above including the colon\n- Focus on accuracy and significance over drama\n- Keep the sections from "Why it matters" to "Real world impact" to 2-4 sentences each, and the sections after them to one or two sentences\n- Stay neutral and factual\n- No markdown, no extra formatting\n- Do not add any text before or after the structure above'

DEEP_REPORT_SPORTS = 'You are a sports journalist writing a factual report and analysis of a sports story.\n\nBelow are articles covering the same story:\n\n{combined}\n\nFirst decide what the story is about:\n- GAME: the coverage reports on a specific game, match, race, bout, or tournament round that has already been played.\n- OTHER: anything else -- injuries, trades, signings, contracts, coaching, previews of games not yet played, rankings, off-field incidents, legal or business news, disputes, or commentary.\n\nIf it is a GAME, write a report using EXACTLY this format:\n\nWhat happened: [2-3 sentences with the result and the decisive moments]\n\nKey performances: [Standout players, teams, or moments from the coverage]\n\nThe bigger picture: [What this means for standings, playoffs, championships, or the season]\n\nBy the numbers: [Key stats or records mentioned in the coverage. If none, say "Detailed statistics not available in current coverage."]\n\nWhat\'s next: [One sentence on the next game or development to watch]\n\nIf it is OTHER, write a report using EXACTLY this format:\n\nWhat happened: [2-3 sentences with the key facts of the news]\n\nWho\'s involved: [The people and organisations involved and what each has said or done]\n\nHow it\'s being covered: [Where the coverage agrees, differs, or adds its own angle]\n\nThe bigger picture: [What this means for the team, league, players, or the sport more broadly]\n\nWhat\'s next: [One sentence on the decision, hearing, return, or development to watch]\n\nRules:\n- Use EXACTLY the labels of the format you chose, including the colon\n- Do not say which format you chose\n- For OTHER, do not describe a game result, score, or performance unless the coverage reports one\n- Keep each section to 2-4 sentences, except "By the numbers", which is one or two sentences\n- Never invent scores, statistics, or results; use only figures that appear in the coverage\n- Focus on facts and context over opinion\n- No markdown, no extra formatting\n- Only state a person\'s title or office (e.g. "coach," "commissioner," "owner," "president") if it is explicitly written in the source text above -- never infer, assume, or invent one from your own knowledge, even if it seems likely\n- Do not add any text before or after the structure above'

DEEP_REPORT_BUSINESS = 'You are a financial journalist writing a detailed report on a business or markets story.\n\nBelow are articles covering the same story:\n\n{combined}\n\nWrite a detailed analytical report using this EXACT format:\n\nThe story: [2-3 sentences explaining what happened factually]\n\nMarket impact: [How markets, stocks, or prices have reacted based on the coverage]\n\nWhat companies or sectors are affected: [Key players, industries, or markets involved and how they are impacted]\n\nThe broader economic picture: [How this fits into wider economic trends, policy, or conditions]\n\nWhat analysts are saying: [One or two sentences of expert or analyst opinions from the coverage. If none available, say "Analyst commentary not available in current coverage."]\n\nRisks and opportunities: [One or two sentences on the key risks or opportunities this creates for investors, businesses, or consumers]\n\nWhat\'s next: [One sentence on key dates, decisions, or developments to watch]\n\nRules:\n- Use EXACTLY the labels shown above including the colon\n- Focus on market and economic significance\n- Keep the sections from "Market impact" to "The broader economic picture" to 2-4 sentences each, and the sections after them to one or two sentences\n- Stay neutral and factual\n- No markdown, no extra formatting\n- Do not add any text before or after the structure above'

DEEP_REPORT_DEFAULT = 'You are an experienced journalist writing a detailed report on a news story.\n\nBelow are articles covering the same story:\n\n{combined}\n\nWrite a detailed analytical report using this EXACT format:\n\nThe story: [2-3 sentences explaining what happened factually]\n\nWhy it matters: [The significance of this story — who it affects and how]\n\nKey details: [The most important facts, figures, or developments from the coverage]\n\nDifferent perspectives: [How different outlets or sources are framing this story. If coverage is uniform, say what angle is being emphasized.]\n\nWhat\'s missing: [One or two sentences on what angles or questions seem absent from the coverage]\n\nWhat\'s next: [One sentence on what to watch for]\n\nRules:\n- Use EXACTLY the labels shown above including the colon\n- Keep the sections from "Why it matters" to "Different perspectives" to 2-4 sentences each, and "What\'s missing" to one or two sentences\n- Stay neutral and analytical\n- Compare only the outlets and perspectives actually present in the article list\n- Do not use left/right political framing unless the story is explicitly about politics, government, law, elections, or policy\n- No markdown, no extra formatting\n- Do not add any text before or after the structure above'

_NEW = {
    'deep_report.politics': DEEP_REPORT_POLITICS,
    'deep_report.science': DEEP_REPORT_SCIENCE,
    'deep_report.sports': DEEP_REPORT_SPORTS,
    'deep_report.business': DEEP_REPORT_BUSINESS,
    'deep_report.default': DEEP_REPORT_DEFAULT,
}


def upgrade():
    conn = op.get_bind()
    for key, text in _NEW.items():
        conn.execute(
            sa.text(
                "UPDATE prompt_templates SET current_text = :t "
                "WHERE key = :k AND current_text = default_text"
            ),
            {"t": text, "k": key},
        )
        conn.execute(
            sa.text("UPDATE prompt_templates SET default_text = :t WHERE key = :k"),
            {"t": text, "k": key},
        )


def downgrade():
    # The previous defaults are not kept; the new text works as well.
    pass
