# news_fetcher/fetch_and_store_articles/edition_content.py
"""
Filling a published edition with summaries, deep reports and child
article summaries (process_current_edition), plus the deep-report
backfill helper.
"""

import logging
from datetime import datetime, timedelta
import json
from aggregator import db
from aggregator.models import Article, Story
from news_fetcher import llm_client as _llm
from news_fetcher.summarizer import check_ollama_status

logger = logging.getLogger(__name__)


def generate_missing_deep_reports(batch_size=5):
    """Find multi-article stories picked for headlines that don't have deep reports."""
    if not check_ollama_status(_llm.TIER_QUALITY):
        logger.info("Quality-tier LLM offline, skipping deep report generation.")
        return

    from sqlalchemy import func
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=2)

    # Only target stories with a headline_score > 0 (meaning they were picked by the ranker)
    undissected = Story.query.join(Article).group_by(Story.id).having(
        func.count(Article.id) >= 2
    ).filter(
        Story.headline_score > 0,
        Story.created_at >= cutoff,
        (Story.deep_report == None) | (Story.deep_report == "")
    ).order_by(Story.headline_score.desc()).limit(batch_size).all()

    if not undissected:
        logger.info("No headline stories need deep reports.")
        return

    logger.info(f"Generating deep reports for {len(undissected)} headline stories...")
    from news_fetcher.summarizer import generate_deep_report
    for story in undissected:
        report = generate_deep_report(story)
        if report:
            story.deep_report = report
            logger.info(f"  Generated deep report for: {story.title[:60]}")
    
    db.session.commit()
    logger.info("Finished deep report batch.")


STALE_ARTICLE_THRESHOLD = 3  # new articles needed to trigger reanalysis

# How far back to look for stories left incomplete in a superseded edition
# (e.g. Ollama was unreachable while that edition was still "latest").
BACKFILL_LOOKBACK_DAYS = 2

# AppSetting key for the opt-in "also generate Article.deep_analysis during
# story fill" toggle. Stored via the same JSON-in-AppSetting mechanism the admin
# blueprint uses for other persisted settings.
AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY = "auto_article_deep_analysis_enabled"


def _auto_article_deep_analysis_enabled():
    """Whether ingestion should also generate per-article deep analysis.

    Off by default: deep-analysis prompts run at a higher timeout than article
    summaries and cost real extra GPU time for every qualifying article, so this
    should not silently turn on for everyone. Reads the AppSetting toggle
    directly (same pattern scraper.py uses for its retry cache) so the fetcher
    has no import dependency on the admin blueprint.
    """
    from aggregator.models import AppSetting

    setting = AppSetting.query.filter_by(
        key=AUTO_ARTICLE_DEEP_ANALYSIS_SETTING_KEY
    ).first()
    if not setting:
        return False
    try:
        return bool(json.loads(setting.value))
    except (ValueError, TypeError):
        return False


def _fill_story_content(story, metrics):
    """
    Generate any missing summary/deep-report/child-article-summary content
    for a single story. Returns True if anything was generated or reset.
    """
    from news_fetcher.summarizer import (
        summarize_story,
        generate_deep_report,
        summarize_article,
        generate_article_deep_analysis,
        article_has_analysable_content,
    )

    article_count = len(story.articles)
    if article_count == 0:
        return False

    changed = False

    try:
        story_is_stale = False

        # Check if analysis is stale — enough new articles arrived
        # since the last time this story was summarized
        if story.summary_generated_at and article_count >= 2:
            new_article_count = sum(
                1 for a in story.articles
                if a.fetched_at and a.fetched_at > story.summary_generated_at
            )
            if new_article_count >= STALE_ARTICLE_THRESHOLD:
                logger.info(
                    f"  [Processor] Story stale ({new_article_count} new articles "
                    f"since last analysis): {story.title[:60]}"
                )
                story.summary = None
                story.deep_report = None
                story_is_stale = True
                metrics["stale_stories_reset"] += 1
                changed = True

        if article_count >= 2:
            story_outputs_ready = bool(story.summary) and bool(story.deep_report)
        else:
            story_outputs_ready = bool(story.summary)

        missing_child_summaries = any(
            article.content and not article.summary
            for article in story.articles
        )

        if not story_is_stale and story_outputs_ready and not missing_child_summaries:
            metrics["stable_stories_skipped"] += 1
            return changed

        # 1. Process Story-level Summaries
        if article_count >= 2:
            if not story.summary:
                summary = summarize_story(story)
                if summary:
                    story.summary = summary
                    story.summary_generated_at = datetime.utcnow()
                    metrics["story_summaries_generated"] += 1
                    changed = True
                    logger.info(f"  [Processor] Story summary: {story.title[:60]}")

            if not story.deep_report:
                report = generate_deep_report(story)
                if report:
                    story.deep_report = report
                    metrics["deep_reports_generated"] += 1
                    changed = True
                    logger.info(f"  [Processor] Deep report: {story.title[:60]}")
        else:
            # Single-article story: Ensure story summary exists
            story.headline = None
            if not story.summary:
                art = story.articles[0]
                summary = art.summary or summarize_article(art)
                if summary:
                    art.summary = summary
                    story.summary = summary
                    story.summary_generated_at = datetime.utcnow()
                    metrics["story_summaries_generated"] += 1
                    changed = True
                    logger.info(f"  [Processor] Single-source summary: {story.title[:60]}")

            # Ensure old stories that once had multiple articles (and thus a deep_report)
            # are cleaned up when they later appear as single-article stories.
            story.deep_report = None

        db.session.commit()
        # This is critical for the static site links
        for article in story.articles:
            if not article.summary and article.content:
                summary = summarize_article(article)
                if summary:
                    article.summary = summary
                    metrics["child_article_summaries_generated"] += 1
                    changed = True
                    logger.info(f"    [Processor] Child article summary: {article.title[:60]}")
        db.session.commit()

        # Per-article deep analysis for every article in an edition story (this
        # function only runs for edition stories). Any topic qualifies; the
        # only filter is enough real article text to analyse. Off by default
        # for new installs -- see _auto_article_deep_analysis_enabled(). Measured
        # 2026-10-07 on gemma4-12b: ~3.7 s per article.
        if _auto_article_deep_analysis_enabled():
            for article in story.articles:
                if (
                    not article.deep_analysis
                    and article_has_analysable_content(article)
                ):
                    analysis = generate_article_deep_analysis(article)
                    if analysis:
                        article.deep_analysis = analysis
                        metrics["child_article_analyses_generated"] += 1
                        changed = True
                        logger.info(f"    [Processor] Child article deep analysis: {article.title[:60]}")
            db.session.commit()

    except Exception as e:
        logger.error(f"  [Processor] Error processing story {story.id}: {e}")
        db.session.rollback()

    return changed


STORY_COUNTERS = (
    "story_summaries_generated", "deep_reports_generated", "child_article_summaries_generated",
    "child_article_analyses_generated", "stale_stories_reset", "stable_stories_skipped",
)


def _empty_edition_metrics(status, **extra):
    return {"status": status, "stories_seen": 0, **{key: 0 for key in STORY_COUNTERS},
            "backfilled_edition_ids": [], **extra}


def edition_content_targets(backfill_recent=False):
    """What process_current_edition() would fill, without filling it:
    (metrics to start from, [(story_id, edition_id)]). edition_id is set for
    stories of a superseded edition (the backfill) and None for the latest
    edition's. The scheduler runs one job per story (news_fetcher/
    pipeline_run.py) and adds them up with edition_content_metrics()."""
    from aggregator.models import Edition, EditionStory

    latest_edition = Edition.query.order_by(Edition.created_at.desc()).first()
    if not latest_edition:
        logger.info("[Processor] No edition found to process.")
        return _empty_edition_metrics("skipped_no_edition"), []

    # Summaries are the work this gates, so it asks about the quality tier.
    ollama_available = check_ollama_status(_llm.TIER_QUALITY)
    if not ollama_available:
        logger.warning(
            "[Processor] Ollama unreachable at start of edition processing; "
            "will still scan stories and skip generation calls until Ollama recovers."
        )

    story_ids = [es.story_id for es in latest_edition.edition_stories.order_by(EditionStory.rank).all()]
    metrics = _empty_edition_metrics(
        "processed", edition_id=latest_edition.id, ollama_available_at_start=ollama_available,
    )
    metrics["stories_seen"] = len(story_ids)
    logger.info(f"[Processor] Processing {len(story_ids)} stories from {latest_edition.edition_type} edition...")

    seen = set(story_ids)
    targets = [(story_id, None) for story_id in story_ids]
    if backfill_recent:
        cutoff = datetime.utcnow().date() - timedelta(days=BACKFILL_LOOKBACK_DAYS)
        stale_editions = Edition.query.filter(
            Edition.published == True,
            Edition.id != latest_edition.id,
            Edition.date >= cutoff,
        ).order_by(Edition.date.desc(), Edition.created_at.desc()).all()
        for edition in stale_editions:
            for es in edition.edition_stories.order_by(EditionStory.rank).all():
                if es.story_id is None or es.story_id in seen:
                    continue
                seen.add(es.story_id)
                targets.append((es.story_id, edition.id))
    return metrics, targets


def fill_story(story_id, edition_id=None):
    """Fill one story's missing summary, deep report and child summaries.
    Returns its counters, whether anything changed, and `edition_id` (the
    superseded edition it was backfilled for, if any)."""
    counters = {key: 0 for key in STORY_COUNTERS}
    story = db.session.get(Story, story_id)
    changed = bool(story) and _fill_story_content(story, counters)
    return {**counters, "changed": bool(changed), "edition_id": edition_id}


def edition_content_metrics(metrics, story_results):
    """process_current_edition()'s metrics from edition_content_targets()'s
    and each fill_story() result."""
    metrics = dict(metrics, backfilled_edition_ids=list(metrics.get("backfilled_edition_ids") or []))
    for result in story_results:
        for key in STORY_COUNTERS:
            metrics[key] = metrics.get(key, 0) + (result.get(key) or 0)
        edition_id = result.get("edition_id")
        if result.get("changed") and edition_id and edition_id not in metrics["backfilled_edition_ids"]:
            logger.info(f"  [Processor] Backfilled stories in superseded edition {edition_id}")
            metrics["backfilled_edition_ids"].append(edition_id)
    logger.info("[Processor] Current edition processing complete.")
    return metrics


def process_current_edition(backfill_recent=False):
    """
    Exhaustively summarize and analyze the stories selected for the latest
    edition.
    1. Finds the most recent Edition and fills any missing summary/deep-report/
       child-article-summary content for its stories.
    2. If backfill_recent is True, also does the same, within
       BACKFILL_LOOKBACK_DAYS, for any other published edition's stories that
       are still incomplete — this catches stories that were left without a
       summary because Ollama was unreachable while their edition was still
       "latest" (it only recovered after a newer edition superseded it, so
       they'd otherwise never be revisited). Off by default since it touches
       editions beyond the current one — callers that want it (the scheduler)
       opt in explicitly.
    This ensures the static headlines site is fully populated.
    """
    metrics, targets = edition_content_targets(backfill_recent=backfill_recent)
    if metrics["status"] != "processed":
        return metrics
    return edition_content_metrics(metrics, [fill_story(story_id, edition_id) for story_id, edition_id in targets])
