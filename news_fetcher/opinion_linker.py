"""Link Op/Ed pieces to the news story they argue about, and write a short
opinion roundup for stories with two or more linked columns (2026-10-10).

Runs once per full pipeline run, after the edition's summaries and before
the static export (`link_opinion_and_write_roundups()`):

1. Link: each Op/Ed piece fetched in the last LINK_LOOKBACK_HOURS that has not
   been checked yet is embedded from its title and teaser, the nearest news
   articles from the last ARTICLE_WINDOW_DAYS are collapsed to the top
   CANDIDATE_STORIES stories, and the fast model is asked YES/NO for each,
   most similar first. The first YES at MIN_SIMILARITY or above wins (best
   link only). Every piece is checked once, linked or not.
2. Scrape: linked pieces are scraped into `full_text`. NYT and WaPo block
   this, so their columns stay headline-and-teaser only.
3. Roundup: for each story with ROUNDUP_MIN_COLUMNS or more linked columns
   and a new link since its last roundup, the quality model marks each column
   on/off topic, writes one phrase on what it argues, and writes the roundup
   paragraph. Columns it marks off topic are unlinked; if any were, the
   paragraph is rewritten from the remaining ones.

Measured before building (2026-10-10, 324 op-eds over 48 h): 91 linked, about
80 right, 7 wrong, 7 loose; the roundup's on-topic check catches the wrong
ones. Letters to the editor are never linked.

Nothing here touches articles or story membership. A failure on one piece or
story is logged and skipped; the next full run retries what is left.
"""
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import text

from aggregator.models import OpinionPiece, Story, db
from aggregator.opinion import leaning
from news_fetcher import llm_client
from news_fetcher.prompt_registry import render_prompt

logger = logging.getLogger(__name__)

LINK_LOOKBACK_HOURS = 48
ARTICLE_WINDOW_DAYS = 4
CANDIDATE_STORIES = 3
MIN_SIMILARITY = 0.68
# Same floor as story_view.has_good_original: blocked articles have empty
# content and their embeddings cluster together whatever the subject.
ARTICLE_CONTENT_FLOOR = 500

ROUNDUP_MIN_COLUMNS = 2
ROUNDUP_MAX_COLUMNS = 5
FULL_TEXT_MIN = 800
COLUMN_TEXT_MAX = 1500
TEASER_MAX = 300
STORY_SUMMARY_MAX = 600
ROUNDUP_MIN_CHARS = 150

_LETTERS_TITLE_RE = re.compile(r"\bletters? to the editor\b", re.IGNORECASE)
_LETTERS_URL_RE = re.compile(r"/letters(?:-to-the-editor)?/|-letters/?$", re.IGNORECASE)
# NYT and WaPo letter pages have ordinary titles; their teaser gives them away.
_LETTERS_TEASER_RE = re.compile(r"^\s*(?:<p>\s*)?(?:ny post\s+)?readers\s+(?:respond|react|are|on|discuss|weigh)\b",
                                re.IGNORECASE)

_COLUMN_LINE_RE = re.compile(r"^\s*\[?(\d+)\]?\s*[:.)-]\s*(ON|OFF)\b\s*[|:-]?\s*(.*)$", re.IGNORECASE)


def _plain(html):
    from news_fetcher.summarizer import strip_html
    return strip_html(html or "").strip()


def is_letters_page(title, url, teaser):
    """Letters to the editor and reader-letter round-ups are not columns."""
    return bool(
        _LETTERS_TITLE_RE.search(title or "")
        or _LETTERS_URL_RE.search(url or "")
        or _LETTERS_TEASER_RE.match(teaser or "")
    )


def parse_link_answer(answer):
    return (answer or "").strip().upper().startswith("YES")


def choose_columns(pieces, limit=ROUNDUP_MAX_COLUMNS):
    """Up to `limit` columns, taking turns across left/center/right/unrated
    so one side can't fill the prompt; within a side, full text first, then
    newest."""
    sides = {"left": [], "center": [], "right": [], "unrated": []}
    for piece in pieces:
        sides[leaning(piece.bias_score)].append(piece)
    for side in sides.values():
        side.sort(key=lambda p: (not p.full_text, -(p.date or p.fetched_at or datetime.min).timestamp()))
    chosen = []
    while len(chosen) < limit and any(sides.values()):
        for name in ("left", "center", "right", "unrated"):
            if sides[name] and len(chosen) < limit:
                chosen.append(sides[name].pop(0))
    return chosen


def format_columns(pieces):
    blocks = []
    for i, piece in enumerate(pieces, 1):
        if piece.full_text:
            blocks.append(f'[{i}] {piece.source}: "{piece.title}"\n{piece.full_text[:COLUMN_TEXT_MAX]}')
        else:
            teaser = _plain(piece.content)[:TEASER_MAX] or "(none)"
            blocks.append(f'[{i}] {piece.source}: "{piece.title}" (HEADLINE ONLY)\nTeaser: {teaser}')
    return "\n\n".join(blocks)


def parse_roundup(output, n_columns):
    """Split the model's answer into ({number: (on_topic, stance)}, paragraph).

    Expected shape: one "<n>: ON | phrase" / "<n>: OFF | phrase" line per
    column, a line "ROUNDUP:", then the paragraph. Lines for unknown column
    numbers are ignored; a column with no line counts as on topic.
    """
    verdicts = {}
    paragraph_lines = []
    in_paragraph = False
    for line in (output or "").splitlines():
        stripped = line.strip().strip("*").strip()
        if not in_paragraph and stripped.upper().startswith("ROUNDUP"):
            in_paragraph = True
            rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if rest:
                paragraph_lines.append(rest)
            continue
        if in_paragraph:
            paragraph_lines.append(stripped)
            continue
        match = _COLUMN_LINE_RE.match(stripped)
        if match and 1 <= int(match.group(1)) <= n_columns:
            verdicts[int(match.group(1))] = (match.group(2).upper() == "ON", match.group(3).strip() or None)
    paragraph = " ".join(line for line in paragraph_lines if line).strip()
    return verdicts, paragraph


def roundup_rejection(paragraph):
    """Reason a roundup paragraph must not be stored, else None."""
    from news_fetcher.headline_generator import _looks_like_llm_failure
    from news_fetcher.quality_checks import looks_truncated

    if not paragraph:
        return "empty"
    if len(paragraph) < ROUNDUP_MIN_CHARS:
        return f"too short ({len(paragraph)} chars)"
    if _looks_like_llm_failure(paragraph):
        return "not a roundup"
    return looks_truncated(paragraph)


def _vector_literal(values):
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def _candidate_stories(vector):
    rows = db.session.execute(text("""
        SELECT a.story_id, 1 - (a.embedding <=> CAST(:v AS vector)) AS sim
        FROM articles a
        WHERE a.embedding IS NOT NULL AND a.story_id IS NOT NULL
          AND a.fetched_at > now() - make_interval(days => :d)
          AND length(a.content) >= :floor
        ORDER BY a.embedding <=> CAST(:v AS vector)
        LIMIT 40
    """), {"v": vector, "d": ARTICLE_WINDOW_DAYS, "floor": ARTICLE_CONTENT_FLOOR}).fetchall()
    stories, seen = [], set()
    for story_id, sim in rows:
        if story_id in seen:
            continue
        seen.add(story_id)
        stories.append((story_id, float(sim)))
        if len(stories) == CANDIDATE_STORIES:
            break
    return stories


def link_new_opinion_pieces(now=None):
    """Step 1: link each unchecked recent piece to at most one story."""
    from news_fetcher.story_grouper import strip_video_prefix

    now = now or datetime.utcnow()
    metrics = {"checked": 0, "linked": 0, "letters": 0, "no_embedding": 0, "llm_errors": 0}
    pieces = (OpinionPiece.query
              .filter(OpinionPiece.link_checked_at.is_(None),
                      OpinionPiece.fetched_at >= now - timedelta(hours=LINK_LOOKBACK_HOURS))
              .order_by(OpinionPiece.id).all())
    for piece in pieces:
        try:
            teaser = _plain(piece.content)[:200]
            if is_letters_page(piece.title, piece.url, teaser):
                piece.link_checked_at = now
                metrics["letters"] += 1
                db.session.commit()
                continue
            if piece.embedding is None:
                emb = llm_client.get_embedding(strip_video_prefix(piece.title) + ". " + teaser)
                if not emb:
                    # Leave it unchecked: an embedding outage shouldn't mark
                    # pieces as done. They retry next run.
                    metrics["no_embedding"] += 1
                    continue
                piece.embedding = emb
            vector = _vector_literal(piece.embedding)
            llm_failed = False
            for story_id, sim in _candidate_stories(vector):
                if sim < MIN_SIMILARITY:
                    break
                story = db.session.get(Story, story_id)
                if story is None:
                    continue
                prompt = render_prompt(
                    "opinion_link_check",
                    title=piece.title,
                    teaser=teaser or "(none)",
                    headline=story.headline or story.title,
                    summary=_plain(story.summary)[:STORY_SUMMARY_MAX] or "(none)",
                )
                if prompt is None:
                    raise RuntimeError("opinion_link_check prompt missing")
                answer = llm_client.generate_text(prompt, timeout=60, tier=llm_client.TIER_FAST)
                if answer is None:
                    llm_failed = True
                    break
                if parse_link_answer(answer):
                    piece.story_id = story_id
                    piece.link_similarity = round(sim, 4)
                    metrics["linked"] += 1
                    break
            if llm_failed:
                metrics["llm_errors"] += 1
                db.session.commit()  # keep the embedding; check again next run
                continue
            piece.link_checked_at = now
            metrics["checked"] += 1
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error("  [OpEd] Linking failed for opinion piece %s: %s", piece.id, exc)
            metrics["llm_errors"] += 1
    logger.info("  [OpEd] Linked %(linked)s of %(checked)s Op/Ed pieces (%(letters)s letters skipped)", metrics)
    return metrics


def scrape_linked_pieces():
    """Step 2: read the full text of linked pieces that haven't been tried."""
    from news_fetcher import scraper

    metrics = {"tried": 0, "full_text": 0}
    pieces = OpinionPiece.query.filter(OpinionPiece.story_id.isnot(None),
                                       OpinionPiece.scrape_status.is_(None)).all()
    for piece in pieces:
        metrics["tried"] += 1
        try:
            result = scraper.scrape_article(piece.url)
            body = _plain(result.content) if result.content else ""
            if len(body) >= FULL_TEXT_MIN:
                piece.full_text = body
                metrics["full_text"] += 1
            piece.scrape_status = result.status
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.info("  [OpEd] Scrape failed for opinion piece %s: %s", piece.id, exc)
            piece = db.session.get(OpinionPiece, piece.id)
            if piece is not None:
                piece.scrape_status = "failed"
                db.session.commit()
    logger.info("  [OpEd] Scraped %(tried)s linked Op/Ed pieces, %(full_text)s with full text", metrics)
    return metrics


def _stale_roundup_story_ids():
    rows = db.session.execute(text("""
        SELECT s.id
        FROM stories s
        JOIN opinion_pieces o ON o.story_id = s.id
        GROUP BY s.id, s.opinion_roundup_generated_at
        HAVING count(*) >= :n
           AND (s.opinion_roundup_generated_at IS NULL
                OR max(o.link_checked_at) > s.opinion_roundup_generated_at)
    """), {"n": ROUNDUP_MIN_COLUMNS}).fetchall()
    return [row[0] for row in rows]


def _ask_roundup(story, columns):
    prompt = render_prompt(
        "opinion_roundup",
        headline=story.headline or story.title,
        summary=_plain(story.summary)[:STORY_SUMMARY_MAX] or "(none)",
        columns=format_columns(columns),
    )
    if prompt is None:
        raise RuntimeError("opinion_roundup prompt missing")
    output = llm_client.generate_text(prompt, timeout=180, tier=llm_client.TIER_QUALITY)
    return parse_roundup(output, len(columns))


def write_roundup(story, now=None):
    """Step 3 for one story. Returns "written", "too_few" or a rejection reason."""
    now = now or datetime.utcnow()
    pieces = OpinionPiece.query.filter_by(story_id=story.id).all()
    columns = choose_columns(pieces)
    verdicts, paragraph = _ask_roundup(story, columns)

    off_topic = [col for i, col in enumerate(columns, 1) if not verdicts.get(i, (True, None))[0]]
    for i, col in enumerate(columns, 1):
        if i in verdicts and verdicts[i][1]:
            col.stance = verdicts[i][1][:300]
    for col in off_topic:
        logger.info("  [OpEd] Unlinking off-topic column %s '%s' from story %s", col.id, col.title[:60], story.id)
        col.story_id = None
        col.stance = None
    if off_topic:
        columns = [col for col in columns if col not in off_topic]
        remaining = [p for p in pieces if p not in off_topic]
        if len(remaining) < ROUNDUP_MIN_COLUMNS:
            story.opinion_roundup = None
            story.opinion_roundup_generated_at = now
            db.session.commit()
            return "too_few"
        columns = choose_columns(remaining)
        verdicts, paragraph = _ask_roundup(story, columns)
        for i, col in enumerate(columns, 1):
            if i in verdicts and verdicts[i][1]:
                col.stance = verdicts[i][1][:300]

    reason = roundup_rejection(paragraph)
    if reason:
        # Keep the previous roundup (if any) and leave the timestamp alone so
        # the next full run tries again.
        db.session.commit()
        logger.info("  [OpEd] Rejected roundup for story %s: %s", story.id, reason)
        return reason
    story.opinion_roundup = paragraph
    story.opinion_roundup_generated_at = now
    db.session.commit()
    return "written"


def write_stale_roundups():
    metrics = {"stories": 0, "written": 0, "too_few": 0, "rejected": 0, "errors": 0}
    for story_id in _stale_roundup_story_ids():
        metrics["stories"] += 1
        try:
            story = db.session.get(Story, story_id)
            outcome = write_roundup(story)
            if outcome in ("written", "too_few"):
                metrics[outcome] += 1
            else:
                metrics["rejected"] += 1
        except Exception as exc:
            db.session.rollback()
            logger.error("  [OpEd] Roundup failed for story %s: %s", story_id, exc)
            metrics["errors"] += 1
    logger.info("  [OpEd] Roundups: %(written)s written, %(rejected)s rejected, "
                "%(too_few)s left with too few columns, of %(stories)s stories", metrics)
    return metrics


def link_opinion_and_write_roundups():
    """The full-run step: link, scrape, then write roundups."""
    if not llm_client.check_llm_status(tier=llm_client.TIER_FAST):
        return {"status": "skipped", "reason": "llm_unavailable"}
    return {
        "status": "ok",
        "link": link_new_opinion_pieces(),
        "scrape": scrape_linked_pieces(),
        "roundups": write_stale_roundups(),
    }
