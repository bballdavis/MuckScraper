# muckscraperHeadlinesGoogleNEW/news_fetcher/scheduler.py
# news_fetcher/scheduler.py

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from aggregator import create_app, db, jobs, scheduler_tasks
from aggregator.workers import Workers
from aggregator.article_signals import bias_bucket_for_score, is_independent_source
from aggregator.models import AppSetting, PipelineSchedule
from news_fetcher.fetch_and_store_articles import cleanup_old_payloads, ingest_payloads, poll_news_apis_if_due, scheduled_fetches, review_ambiguous_grouping_matches, sync_allsides_ratings, publish_edition, retry_unrated_outlets, clear_stale_single_article_headlines
from news_fetcher.headline_generator import generate_headlines_for_stale_stories
from news_fetcher.copy_detector import mark_recent_copies
from news_fetcher.opinion_linker import link_opinion_and_write_roundups
from news_fetcher.outlet_logos import refresh_card_images
from news_fetcher.fetch_and_store_articles.edition_content import (
    edition_content_metrics,
    edition_content_targets,
    fill_story,
)
from news_fetcher.rss_fetcher import (
    active_feed_urls,
    oped_feed_metrics,
    oped_rss_metrics,
    rss_feed_metrics,
    rss_metrics,
    enrich_skewed_stories_with_right_feeds,
    get_skewed_story_ids_for_right_enrichment,
    enrich_skewed_stories_with_left_feeds,
    get_skewed_story_ids_for_left_enrichment,
)
from news_fetcher import llm_client, nyt_api, pipeline_run
from datetime import datetime, timedelta, timezone
import logging
import sys
import os
import requests
import json
from zoneinfo import ZoneInfo

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# When the scheduler fetches/publishes is DB-backed (PipelineSchedule,
# admin-editable at /admin/pipeline-schedule) rather than env-var hour lists.
TIMEZONE = "America/New_York"


def get_scheduled_fetches():
    """
    What NewsAPI and GNews are asked for -- DB-backed (ScheduledFetch,
    admin-editable at /admin/scheduled-fetches) rather than a hardcoded list.

    Read fresh on every poll, so an edit takes effect with no restart. The
    polls themselves run on each API's own schedule within its daily budget
    (news_fetcher/api_budget.py); pipeline runs store what they brought.
    """
    return scheduled_fetches()


# The worker threads (aggregator/workers.py) and the ingest pool
# (news_fetcher/workpool.py) each hold a connection while they work.
app = create_app(engine_options={"pool_size": 20, "max_overflow": 20, "pool_pre_ping": True})
SCRAPE_OUTCOME_HISTORY_KEY = "scrape_outcome_history_v1"
SCRAPE_OUTCOME_HISTORY_MAX_RUNS = 40
FETCH_RUN_STATUS_KEY = pipeline_run.FETCH_RUN_STATUS_KEY


def run_optional_headline_ranking():
    """
    Run the private ranking plugin when it exists locally, else the built-in
    ranker (aggregator/story_rank.py), so Story.headline_score is never left
    at 0. The open-source scheduler must not require ignored/private modules.
    """
    try:
        from news_fetcher.headline_ranker import run_headline_ranking
    except ImportError as e:
        from aggregator.story_rank import rank_recent_stories
        logging.info(f"--- Private headline ranking unavailable ({e}); using the built-in ranker ---")
        return rank_recent_stories()

    run_headline_ranking()
    return {"status": "ok"}


def run_optional_daily_horoscope():
    """
    Run the private daily-horoscope plugin when it exists locally. It writes
    at most once per day, so calling it on every full run is cheap.
    """
    try:
        from news_fetcher.horoscope import run_daily_horoscope
    except ImportError as e:
        logging.info(f"--- Daily horoscope skipped ({e}) ---")
        return {
            "status": "skipped",
            "reason": str(e),
        }

    return run_daily_horoscope()


def run_optional_static_export(extra_edition_ids=None, opinion_story_ids=None):
    """
    Export optional static output when an additional exporter is available.
    This keeps the main open-source stack working even when deployment-specific
    export code is not present.
    """
    try:
        from private_site.export_static import export_static_site
    except ImportError as e:
        logging.warning(
            "--- Optional static export skipped (%s). If static publishing is "
            "enabled in this deployment, make sure the extra exporter module "
            "and output mounts are available to the scheduler container. ---",
            e,
        )
        return {
            "status": "skipped",
            "reason": str(e),
        }

    export_static_site(extra_edition_ids=extra_edition_ids, opinion_story_ids=opinion_story_ids)
    return {"status": "ok"}


def _load_json_setting(key):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting or not setting.value:
        return None
    try:
        return json.loads(setting.value)
    except Exception:
        logging.warning("Could not parse JSON AppSetting for key=%s", key)
        return None


def _save_json_setting(key, value):
    payload = json.dumps(value, sort_keys=True)
    setting = AppSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = payload
    else:
        db.session.add(AppSetting(key=key, value=payload))
    db.session.commit()


# Per-run quality counters kept in the outcome history: history key -> (step
# name in run_metrics["steps"], counter names). last_run_metrics is overwritten
# every run, so without this the counters have no trend.
_QUALITY_STEP_COUNTERS = {
    "headline_generation": ("headline_generation", ("candidates", "generated", "failed")),
    "grouping_review": ("review_ambiguous_grouping_matches", ("reviewed", "reassigned")),
    "edition_content": (
        "process_current_edition",
        (
            "story_summaries_generated", "deep_reports_generated",
            "child_article_summaries_generated", "stale_stories_reset",
        ),
    ),
    "edition_publish": (
        "publish_edition",
        (
            "story_count", "dedupe_skip_count", "mixed_coverage_count",
            "caps_skipped_bias", "balance_bucket_counts",
        ),
    ),
}


def _quality_step_counters(run_metrics):
    """Counters for phases that ran; None for phases that were skipped or errored.

    None, not 0: a fetch-only run skips these phases, and "did not run" must not
    look like "ran and produced nothing" in a trend.
    """
    steps = run_metrics.get("steps") or {}
    out = {}
    for key, (step_name, counters) in _QUALITY_STEP_COUNTERS.items():
        step = steps.get(step_name)
        ran = isinstance(step, dict) and step.get("status") in ("ok", "processed", "published", "empty")
        out[key] = {name: step.get(name) for name in counters} if ran else None
    return out


def _build_scrape_outcome_history_entry(run_metrics, headline_site_metrics):
    started_at = run_metrics.get("started_at")
    finished_at = run_metrics.get("finished_at")
    duration_seconds = None
    if started_at and finished_at:
        try:
            duration_seconds = int(
                (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
            )
        except Exception:
            duration_seconds = None

    return {
        "recorded_at": finished_at or datetime.utcnow().isoformat(),
        "started_at": started_at,
        "run_full_pipeline": run_metrics.get("run_full_pipeline"),
        "status": run_metrics.get("status"),
        "duration_seconds": duration_seconds,
        "timings": [{"step": t["step"], "seconds": t.get("seconds")} for t in run_metrics.get("timings", [])],
        "edition": headline_site_metrics.get("edition"),
        "run_scrape_statuses": dict(run_metrics.get("totals", {}).get("scrape_statuses", {})),
        "run_skipped": dict(run_metrics.get("totals", {}).get("skipped", {})),
        "run_bias_buckets": dict(run_metrics.get("totals", {}).get("bias_buckets", {})),
        "run_bias_sources": dict(run_metrics.get("totals", {}).get("bias_sources", {})),
        "headline_scrape": dict(headline_site_metrics.get("scrape", {}).get("articles_by_status", {})),
        "headline_readable_articles": headline_site_metrics.get("scrape", {}).get("readable_articles"),
        "headline_fully_read_articles": headline_site_metrics.get("scrape", {}).get("fully_read_articles"),
        "headline_blocked_articles": headline_site_metrics.get("scrape", {}).get("blocked_articles"),
        "stored_articles": run_metrics.get("totals", {}).get("stored"),
        "input_articles": run_metrics.get("totals", {}).get("input_articles"),
        "quality": _quality_step_counters(run_metrics),
    }


def append_scrape_outcome_history(run_metrics, headline_site_metrics, max_runs=SCRAPE_OUTCOME_HISTORY_MAX_RUNS):
    history = _load_json_setting(SCRAPE_OUTCOME_HISTORY_KEY)
    if not isinstance(history, list):
        history = []

    history.append(_build_scrape_outcome_history_entry(run_metrics, headline_site_metrics))
    history = history[-max_runs:]
    _save_json_setting(SCRAPE_OUTCOME_HISTORY_KEY, history)
    return history


def _merge_counts(target, source):
    for key, value in (source or {}).items():
        target[key] = target.get(key, 0) + value


def _run_targeted_rss_enrichment_pass(config, run_metrics, ollama_state):
    """
    Run one direction (left or right) of targeted RSS enrichment: a first
    enrichment pass, then a second pass on stories still skewed after the
    first, with the same bias-retry follow-up step.

    Headline ranking no longer runs between passes here — a single ranking
    pass now runs once at the end of the full pipeline (see run_all_fetches)
    instead of up to 5 times. The second-pass target query
    (config["get_skewed_ids_func"]) still orders by Story.headline_score, so
    it uses whatever score the previous pipeline run set rather than a
    mid-run refresh — the same tradeoff already accepted on the Groq
    provider path, which skips these passes' ranking re-runs entirely.
    """
    steps = run_metrics["steps"]
    direction = config["direction"]

    logging.info(f"--- Running targeted {config['direction_adj']} RSS enrichment ---")
    try:
        enrichment_metrics = config["enrich_func"]()
        steps[config["enrichment_step"]] = enrichment_metrics
        run_metrics["totals"]["input_articles"] += enrichment_metrics.get("input_articles", 0)
        run_metrics["totals"]["stored"] += enrichment_metrics.get("stored", 0)
        run_metrics["totals"]["new_outlets"] += enrichment_metrics.get("new_outlets", 0)
        run_metrics["totals"]["stories_touched"] += enrichment_metrics.get("stories_touched", 0)
        _merge_counts(run_metrics["totals"]["skipped"], enrichment_metrics.get("skipped"))
        _merge_counts(run_metrics["totals"]["scrape_statuses"], enrichment_metrics.get("scrape_statuses"))
        _merge_counts(run_metrics["totals"]["bias_buckets"], enrichment_metrics.get("bias_buckets"))
        _merge_counts(run_metrics["totals"]["bias_sources"], enrichment_metrics.get("bias_sources"))
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error in targeted {direction} RSS enrichment: {e}")
        run_metrics["status"] = "partial_error"
        enrichment_metrics = {"status": "error", "reason": str(e), "stored": 0}
        steps[config["enrichment_step"]] = enrichment_metrics

    if enrichment_metrics.get("stored", 0) > 0:
        logging.info(f"--- Refreshing outlet bias after targeted {direction} enrichment ---")
        _check_ollama_status_for_report(ollama_state, config["bias_retry_ollama_label"])
        try:
            retry_unrated_outlets()
            steps[config["bias_retry_step"]] = {"status": "ok"}
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error retrying outlet bias after targeted {direction} enrichment: {e}")
            run_metrics["status"] = "partial_error"
            steps[config["bias_retry_step"]] = {"status": "error", "reason": str(e)}

        logging.info(f"--- Running second-pass targeted {direction} RSS enrichment on skewed stories ---")
        try:
            reranked_target_ids = config["get_skewed_ids_func"](max_stories=5, candidate_pool=30)
            if reranked_target_ids:
                second_pass_metrics = config["enrich_func"](
                    max_stories=5,
                    max_articles_per_story=2,
                    candidate_story_ids=reranked_target_ids,
                )
                second_pass_metrics["target_story_ids"] = reranked_target_ids
                steps[config["second_pass_enrichment_step"]] = second_pass_metrics
                run_metrics["totals"]["input_articles"] += second_pass_metrics.get("input_articles", 0)
                run_metrics["totals"]["stored"] += second_pass_metrics.get("stored", 0)
                run_metrics["totals"]["new_outlets"] += second_pass_metrics.get("new_outlets", 0)
                run_metrics["totals"]["stories_touched"] += second_pass_metrics.get("stories_touched", 0)
                _merge_counts(run_metrics["totals"]["skipped"], second_pass_metrics.get("skipped"))
                _merge_counts(run_metrics["totals"]["scrape_statuses"], second_pass_metrics.get("scrape_statuses"))
                _merge_counts(run_metrics["totals"]["bias_buckets"], second_pass_metrics.get("bias_buckets"))
                _merge_counts(run_metrics["totals"]["bias_sources"], second_pass_metrics.get("bias_sources"))

                if second_pass_metrics.get("stored", 0) > 0:
                    _check_ollama_status_for_report(ollama_state, config["second_pass_bias_retry_ollama_label"])
                    try:
                        retry_unrated_outlets()
                        steps[config["second_pass_bias_retry_step"]] = {"status": "ok"}
                    except Exception as e:
                        db.session.rollback()
                        logging.error(f"Error retrying outlet bias after second-pass targeted {direction} enrichment: {e}")
                        run_metrics["status"] = "partial_error"
                        steps[config["second_pass_bias_retry_step"]] = {"status": "error", "reason": str(e)}
                else:
                    steps[config["second_pass_bias_retry_step"]] = {
                        "status": "skipped",
                        "reason": "no_second_pass_enrichment_articles",
                    }
            else:
                steps[config["second_pass_enrichment_step"]] = {
                    "status": "skipped",
                    "reason": "no_reranked_skewed_stories",
                }
                steps[config["second_pass_bias_retry_step"]] = {
                    "status": "skipped",
                    "reason": "no_reranked_skewed_stories",
                }
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error in second-pass targeted {direction} RSS enrichment: {e}")
            run_metrics["status"] = "partial_error"
            steps[config["second_pass_enrichment_step"]] = {
                "status": "error",
                "reason": str(e),
            }
            steps[config["second_pass_bias_retry_step"]] = {
                "status": "skipped",
                "reason": "second_pass_error",
            }
    else:
        steps[config["bias_retry_step"]] = {
            "status": "skipped",
            "reason": "no_enrichment_articles",
        }
        steps[config["second_pass_enrichment_step"]] = {
            "status": "skipped",
            "reason": "first_pass_no_enrichment_articles",
        }
        steps[config["second_pass_bias_retry_step"]] = {
            "status": "skipped",
            "reason": "first_pass_no_enrichment_articles",
        }


RIGHT_RSS_ENRICHMENT_CONFIG = {
    "direction": "right",
    "direction_adj": "right-leaning",
    "enrich_func": enrich_skewed_stories_with_right_feeds,
    "get_skewed_ids_func": get_skewed_story_ids_for_right_enrichment,
    "enrichment_step": "targeted_right_rss_enrichment",
    "bias_retry_step": "targeted_right_rss_bias_retry",
    "second_pass_enrichment_step": "targeted_right_rss_enrichment_second_pass",
    "second_pass_bias_retry_step": "targeted_right_rss_second_pass_bias_retry",
    "bias_retry_ollama_label": "before_targeted_enrichment_bias_retry",
    "second_pass_bias_retry_ollama_label": "before_second_pass_targeted_enrichment_bias_retry",
}

LEFT_RSS_ENRICHMENT_CONFIG = {
    "direction": "left",
    "direction_adj": "left-leaning",
    "enrich_func": enrich_skewed_stories_with_left_feeds,
    "get_skewed_ids_func": get_skewed_story_ids_for_left_enrichment,
    "enrichment_step": "targeted_left_rss_enrichment",
    "bias_retry_step": "targeted_left_rss_bias_retry",
    "second_pass_enrichment_step": "targeted_left_rss_enrichment_second_pass",
    "second_pass_bias_retry_step": "targeted_left_rss_second_pass_bias_retry",
    "bias_retry_ollama_label": "before_targeted_left_enrichment_bias_retry",
    "second_pass_bias_retry_ollama_label": "before_second_pass_targeted_left_enrichment_bias_retry",
}


def build_headline_site_metrics():
    from aggregator.models import Edition, EditionStory

    latest_edition = Edition.query.filter_by(published=True).order_by(
        Edition.created_at.desc()
    ).first()
    if not latest_edition:
        return {
            "status": "no_published_edition",
            "recorded_at": datetime.utcnow().isoformat(),
        }

    edition_stories = latest_edition.edition_stories.order_by(EditionStory.rank).all()
    stories = [edition_story.story for edition_story in edition_stories]
    article_ids = set()
    outlet_ids = set()
    independent_outlet_ids = set()
    article_bias_counts = {}
    outlet_bias_counts = {}
    outlet_bias_source_counts = {"allsides": 0, "ai": 0, "unrated": 0}
    scrape_status_counts = {
        "success": 0,
        "fallback": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
    }
    stories_with_bias_mix = 0
    stories_with_unrated_articles = 0
    multi_source_story_count = 0

    for story in stories:
        story_bias_buckets = set()
        story_outlet_ids = set()
        # Corroboration claims (multi-source, bias mix) count only outlets that
        # contributed real scraped content -- see INDEPENDENT_CONTENT_FLOOR.
        # Raw volume and scrape-health counters below stay unfiltered so the
        # historical series in scrape_outcome_history_v1 remains comparable.
        story_independent_outlet_ids = set()
        story_independent_bias_buckets = set()

        for article in story.articles:
            article_ids.add(article.id)
            independent = is_independent_source(article)
            if article.outlet_id:
                story_outlet_ids.add(article.outlet_id)
                if independent:
                    story_independent_outlet_ids.add(article.outlet_id)
                    independent_outlet_ids.add(article.outlet_id)

            scrape_status = (article.scrape_status or "pending").lower()
            scrape_status_counts[scrape_status] = scrape_status_counts.get(scrape_status, 0) + 1

            bucket = bias_bucket_for_score(article.bias_score if article.bias_score is not None else (article.outlet.bias_score if article.outlet else None))
            article_bias_counts[bucket] = article_bias_counts.get(bucket, 0) + 1
            story_bias_buckets.add(bucket)
            if independent:
                story_independent_bias_buckets.add(bucket)

            if article.outlet and article.outlet.id not in outlet_ids:
                outlet_ids.add(article.outlet.id)
                outlet_bucket = bias_bucket_for_score(article.outlet.bias_score)
                outlet_bias_counts[outlet_bucket] = outlet_bias_counts.get(outlet_bucket, 0) + 1
                bias_source = article.outlet.bias_source or "unrated"
                outlet_bias_source_counts[bias_source] = outlet_bias_source_counts.get(bias_source, 0) + 1

        if len(story_independent_outlet_ids) > 1:
            multi_source_story_count += 1
        if len({bucket for bucket in story_independent_bias_buckets if bucket != "unrated"}) >= 2:
            stories_with_bias_mix += 1
        if "unrated" in story_bias_buckets:
            stories_with_unrated_articles += 1

    return {
        "status": "ok",
        "recorded_at": datetime.utcnow().isoformat(),
        "edition": {
            "id": latest_edition.id,
            "date": latest_edition.date.isoformat(),
            "edition_type": latest_edition.edition_type,
            "created_at": latest_edition.created_at.isoformat() if latest_edition.created_at else None,
        },
        "story_count": len(stories),
        "article_count": len(article_ids),
        "outlet_count": len(outlet_ids),
        "independent_outlet_count": len(independent_outlet_ids),
        "multi_source_story_count": multi_source_story_count,
        "stories_with_bias_mix": stories_with_bias_mix,
        "stories_with_unrated_articles": stories_with_unrated_articles,
        "bias": {
            "articles_by_bucket": article_bias_counts,
            "outlets_by_bucket": outlet_bias_counts,
            "outlets_by_source": outlet_bias_source_counts,
        },
        "scrape": {
            "articles_by_status": scrape_status_counts,
            "readable_articles": scrape_status_counts.get("success", 0) + scrape_status_counts.get("fallback", 0),
            "fully_read_articles": scrape_status_counts.get("success", 0),
            "blocked_articles": scrape_status_counts.get("blocked", 0),
        },
    }


def get_last_fetch_time():
    """Get the last fetch timestamp from the database."""
    setting = AppSetting.query.filter_by(key="last_fetch").first()
    if setting and setting.value:
        try:
            return datetime.fromisoformat(setting.value)
        except Exception:
            return None
    return None


def set_last_fetch_time():
    """Store the current time as the last fetch timestamp."""
    setting = AppSetting.query.filter_by(key="last_fetch").first()
    if setting:
        setting.value = datetime.utcnow().isoformat()
    else:
        setting = AppSetting(key="last_fetch", value=datetime.utcnow().isoformat())
        db.session.add(setting)
    db.session.commit()


def get_last_allsides_sync():
    """Get the last AllSides sync timestamp from the database."""
    setting = AppSetting.query.filter_by(key="last_allsides_sync").first()
    if setting and setting.value:
        try:
            return datetime.fromisoformat(setting.value)
        except Exception:
            return None
    return None


def set_last_allsides_sync():
    """Store the current time as the last AllSides sync timestamp."""
    setting = AppSetting.query.filter_by(key="last_allsides_sync").first()
    if setting:
        setting.value = datetime.utcnow().isoformat()
    else:
        setting = AppSetting(key="last_allsides_sync", value=datetime.utcnow().isoformat())
        db.session.add(setting)
    db.session.commit()


def set_scheduler_started_at():
    """
    Record when the scheduler process last (re)started -- used by the admin
    Container Restart page to tell whether a PipelineSchedule edit happened
    after the scheduler last picked up its jobs (i.e. whether a restart is
    still needed for the new schedule to take effect).
    """
    setting = AppSetting.query.filter_by(key="scheduler_started_at").first()
    if setting:
        setting.value = datetime.utcnow().isoformat()
    else:
        setting = AppSetting(key="scheduler_started_at", value=datetime.utcnow().isoformat())
        db.session.add(setting)
    db.session.commit()


def _scheduled_hours():
    return sorted({e.hour for e in PipelineSchedule.query.filter_by(is_active=True).all()})


def _full_pipeline_hours():
    return sorted({
        e.hour for e in PipelineSchedule.query.filter_by(is_active=True, run_full_pipeline=True).all()
    })


def _latest_scheduled_run_before(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    local_tz = ZoneInfo(TIMEZONE)
    local_now = now.astimezone(local_tz)

    candidates = []
    for day_offset in (0, -1):
        candidate_date = (local_now + timedelta(days=day_offset)).date()
        for hour in _scheduled_hours():
            candidates.append(
                datetime(
                    candidate_date.year,
                    candidate_date.month,
                    candidate_date.day,
                    hour,
                    0,
                    tzinfo=local_tz,
                )
            )

    eligible = [candidate for candidate in candidates if candidate <= local_now]
    if not eligible:
        return None
    return max(eligible).astimezone(timezone.utc)


def _latest_full_pipeline_run_before(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    local_tz = ZoneInfo(TIMEZONE)
    local_now = now.astimezone(local_tz)

    candidates = []
    for day_offset in (0, -1):
        candidate_date = (local_now + timedelta(days=day_offset)).date()
        for hour in _full_pipeline_hours():
            candidates.append(
                datetime(
                    candidate_date.year,
                    candidate_date.month,
                    candidate_date.day,
                    hour,
                    0,
                    tzinfo=local_tz,
                )
            )

    eligible = [candidate for candidate in candidates if candidate <= local_now]
    if not eligible:
        return None
    return max(eligible).astimezone(timezone.utc)


def should_fetch_now(now=None, last_fetch=None):
    """
    Returns True on startup only when the app missed a scheduled fetch slot.
    This avoids ad-hoc catch-up runs based on elapsed time alone.
    """
    if last_fetch is None:
        last_fetch = get_last_fetch_time()

    if not last_fetch:
        logging.info("No record of previous fetch. Initializing with a startup fetch.")
        return True

    if last_fetch.tzinfo is None:
        last_fetch = last_fetch.replace(tzinfo=timezone.utc)

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    latest_scheduled_run = _latest_scheduled_run_before(now=now)
    if latest_scheduled_run and last_fetch < latest_scheduled_run:
        logging.info(
            "Last fetch at %s missed scheduled run at %s, fetching on startup.",
            last_fetch.isoformat(),
            latest_scheduled_run.isoformat(),
        )
        return True

    logging.info(
        "Last fetch at %s is up to date with scheduled runs. Skipping startup fetch.",
        last_fetch.isoformat(),
    )
    return False


def should_run_full_pipeline(now=None, last_fetch=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if last_fetch is not None:
        if last_fetch.tzinfo is None:
            last_fetch = last_fetch.replace(tzinfo=timezone.utc)
        latest_full_slot = _latest_full_pipeline_run_before(now=now)
        return bool(latest_full_slot and last_fetch < latest_full_slot)

    local_now = now.astimezone(ZoneInfo(TIMEZONE))
    return local_now.hour in _full_pipeline_hours()


def _notify_n8n():
    webhook = os.getenv("N8N_WEBHOOK_URL")
    if not webhook:
        return
    try:
        # Deliberately an Ollama-host probe, not check_llm_status(): this
        # webhook suspends the physical Ollama box, so it must ask about that
        # machine rather than about whichever provider serves a tier. On a
        # cloud-only install there is no box and the probe correctly says no.
        if not llm_client.ollama_host_status()["online"]:
            logging.info("  [n8n] Ollama already unreachable, skipping suspend webhook")
            return

        response = requests.post(webhook, timeout=5)
        response.raise_for_status()
        logging.info(
            "  [n8n] Webhook fired — Ollama suspend sequence triggered (status %s)",
            response.status_code,
        )
    except Exception as e:
        logging.warning(f"  [n8n] Webhook failed ({e}) — continuing normally")


def _refresh_inference_health(ollama_state, label):
    """Probe real Ollama inference and set llm_client's wedged flag.

    The reachability checks only ask whether Ollama's HTTP server answers,
    which stays true when the GPU cannot run inference (2026-09-28). A wedged
    result makes every Ollama call fail fast until a later checkpoint's probe
    succeeds, so a dead GPU degrades the run like an ordinary outage.
    """
    if not llm_client.uses_ollama():
        return
    try:
        ok = llm_client.probe_ollama_inference()
    except Exception as e:
        logging.warning("  [LLM] Inference probe errored during %s (%s)", label, e)
        return
    was_wedged = llm_client.inference_wedged()
    wedged = ok is False
    llm_client.set_inference_wedged(wedged)
    if wedged:
        ollama_state["inference_wedged"] = True
        if not was_wedged:
            logging.error(
                "  [LLM] Ollama answers HTTP but inference is failing at %s -- "
                "skipping Ollama work until it recovers. Host likely needs a reboot.",
                label,
            )
    elif was_wedged:
        logging.info("  [LLM] Ollama inference recovered at %s", label)


def _check_ollama_status_for_report(ollama_state, label):
    """Record LLM availability at one point in the run.

    Checks every provider in use, not just one. Under split routing a stage
    can be skipped for a backend that was never involved, so a single boolean
    would hide which half was actually down -- and the degrade-don't-stop
    design means that degraded state needs to be visible in the run metrics
    rather than only in the logs.

    `up` stays the AND across providers so existing consumers (the n8n report,
    scrape_outcome_history_v1) keep their meaning: for a single-provider
    install, which is every install that hasn't set LLM_FAST_PROVIDER, this is
    byte-identical to the old behavior.
    """
    if label == "run_end":
        # Don't probe here: the n8n suspend follows immediately. Just clear
        # the flag so it can't outlive the run; the next run_start re-probes.
        llm_client.set_inference_wedged(False)
    else:
        _refresh_inference_health(ollama_state, label)

    try:
        by_provider = llm_client.check_all_llm_status()
    except Exception as e:
        logging.warning("  [LLM] Health check failed during %s (%s)", label, e)
        by_provider = {provider: False for provider in llm_client.providers_in_use()}

    is_up = all(by_provider.values()) if by_provider else False

    checked_at = datetime.utcnow().isoformat()
    ollama_state["checks"].append({
        "at": checked_at,
        "label": label,
        "up": is_up,
        "by_provider": by_provider,
    })
    if not is_up:
        ollama_state["went_down_during_run"] = True
        down = [p for p, ok in by_provider.items() if not ok]
        logging.info("  [LLM] Provider(s) unavailable at %s: %s", label, ", ".join(down))
    return is_up


def _build_fetch_report(run_metrics, headline_site_metrics, ollama_state):
    started_at = datetime.fromisoformat(run_metrics["started_at"])
    finished_at = datetime.fromisoformat(run_metrics["finished_at"])
    duration_seconds = int((finished_at - started_at).total_seconds())
    run_totals = run_metrics.get("totals", {})
    headline_scrape = dict(headline_site_metrics.get("scrape", {}).get("articles_by_status", {}))
    edition = headline_site_metrics.get("edition", {}) or {}
    headline_metrics_label = "Latest published edition stats"

    return {
        "status": run_metrics.get("status", "unknown"),
        "started_at": run_metrics["started_at"],
        "finished_at": run_metrics["finished_at"],
        "duration_seconds": duration_seconds,
        # Backward-compatible top-level summary fields for n8n formatters.
        "input_articles": run_totals.get("input_articles", 0),
        "stored_articles": run_totals.get("stored", 0),
        "new_outlets": run_totals.get("new_outlets", 0),
        "stories_touched": run_totals.get("stories_touched", 0),
        "run_scrape_statuses": dict(run_totals.get("scrape_statuses", {})),
        "headline_metrics_label": headline_metrics_label,
        "headline_scrape": headline_scrape,
        "headline_readable_articles": headline_site_metrics.get("scrape", {}).get("readable_articles", 0),
        "headline_fully_read_articles": headline_site_metrics.get("scrape", {}).get("fully_read_articles", 0),
        "headline_blocked_articles": headline_site_metrics.get("scrape", {}).get("blocked_articles", 0),
        "edition": {
            "id": edition.get("id"),
            "date": edition.get("date"),
            "edition_type": edition.get("edition_type"),
        },
        "ollama": ollama_state,
        "run_metrics": run_metrics,
        "headline_metrics": headline_site_metrics,
        "latest_published_edition_metrics": headline_site_metrics,
    }


def _notify_fetch_report(report_payload):
    webhook = os.getenv("N8N_FETCH_REPORT_WEBHOOK_URL")
    if not webhook:
        return

    try:
        response = requests.post(webhook, json=report_payload, timeout=10)
        response.raise_for_status()
        logging.info(
            "  [n8n] Fetch report webhook fired (status %s)",
            response.status_code,
        )
    except Exception as e:
        logging.warning(f"  [n8n] Fetch report webhook failed ({e}) — continuing normally")


_mark_step = pipeline_run.mark_step


def _add_totals(run_metrics, metrics):
    """Add one source's ingest counts to the run's totals."""
    totals = run_metrics["totals"]
    for key in ("input_articles", "stored", "new_outlets", "stories_touched"):
        totals[key] += metrics.get(key, 0) or 0
    for key in ("skipped", "scrape_statuses", "bias_buckets", "bias_sources"):
        _merge_counts(totals[key], metrics.get(key))


def new_run_metrics(run_full_pipeline):
    """What a run's stages build up, saved with its pipeline_runs row."""
    return {
        "ollama_state": {
            "up_at_start": False,
            "up_at_end": False,
            "went_down_during_run": False,
            "inference_wedged": False,
            "checks": [],
        },
        "run_metrics": {
            "status": "ok",
            "started_at": datetime.utcnow().isoformat(),
            "topics": {},
            "rss": None,
            "oped_rss": None,
            "totals": {
                "input_articles": 0,
                "stored": 0,
                "new_outlets": 0,
                "stories_touched": 0,
                "skipped": {},
                "scrape_statuses": {},
                "bias_buckets": {},
                "bias_sources": {},
            },
            "steps": {},
            "run_full_pipeline": run_full_pipeline,
            "timings": [],
        },
    }


# The run's stages (news_fetcher/pipeline_run.py). Each takes the Run, works
# on run.metrics, and queues what comes next.

def stage_fetch(run):
    """Topic fetches and NYT lead paragraphs, then one job per feed."""
    logging.info("=== Starting scheduled fetch run ===")
    run_metrics, ollama_state = run.metrics["run_metrics"], run.metrics["ollama_state"]
    run_full_pipeline = run.full
    _save_json_setting(FETCH_RUN_STATUS_KEY, {
        "status": "running",
        "started_at": run_metrics["started_at"],
        "run_full_pipeline": run_full_pipeline,
    })
    ollama_state["up_at_start"] = _check_ollama_status_for_report(ollama_state, "run_start")

    # NewsAPI and GNews: their scheduled polls (poll_news_apis_if_due,
    # every minute between runs) keep payloads; poll now too if one is
    # due, then store everything the polls brought.
    scheduled_fetches = get_scheduled_fetches()
    if not scheduled_fetches:
        logging.warning(
            "No active scheduled fetches configured -- skipping the NewsAPI/GNews "
            "stage. Add them at /admin/scheduled-fetches. RSS ingestion still runs."
        )
    else:
        _mark_step(run_metrics, "Polling NewsAPI and GNews if due")
        try:
            run_metrics["steps"]["news_api_polls"] = poll_news_apis_if_due(fetches=scheduled_fetches)
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error polling NewsAPI/GNews: {e}")
    # Stories still running after a day: NewsAPI's day-late search can now
    # see their first day's coverage (news_fetcher/story_followup.py).
    _mark_step(run_metrics, "Searching NewsAPI for long-running stories")
    try:
        from news_fetcher.story_followup import search_followups
        run_metrics["steps"]["story_followups"] = search_followups()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error searching story follow-ups: {e}")
        run_metrics["steps"]["story_followups"] = {"status": "error", "reason": str(e)}
    _mark_step(run_metrics, "Storing NewsAPI and GNews articles")
    try:
        polled_topics = ingest_payloads()
        cleanup_old_payloads()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error storing NewsAPI/GNews payloads: {e}")
        run_metrics["status"] = "partial_error"
        polled_topics = {}
    for label, topic_metrics in polled_topics.items():
        run_metrics["topics"][label] = topic_metrics
        for provider_metrics in topic_metrics.get("providers", {}).values():
            if provider_metrics.get("status") == "error":
                run_metrics["status"] = "partial_error"
            run_metrics["totals"]["input_articles"] += provider_metrics.get("input_articles", 0)
            run_metrics["totals"]["stored"] += provider_metrics.get("stored", 0)
            run_metrics["totals"]["new_outlets"] += provider_metrics.get("new_outlets", 0)
            run_metrics["totals"]["stories_touched"] += provider_metrics.get("stories_touched", 0)
            _merge_counts(run_metrics["totals"]["skipped"], provider_metrics.get("skipped"))
            _merge_counts(run_metrics["totals"]["scrape_statuses"], provider_metrics.get("scrape_statuses"))
            _merge_counts(run_metrics["totals"]["bias_buckets"], provider_metrics.get("bias_buckets"))
            _merge_counts(run_metrics["totals"]["bias_sources"], provider_metrics.get("bias_sources"))

    # NYT lead paragraphs first (ten articles a request), so the NYT
    # Opinion and World feed articles below get them too
    # (news_fetcher/nyt_api.py).
    try:
        run_metrics["steps"]["nyt_lead_paragraphs"] = {"searched": nyt_api.search_lead_paragraphs()}
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error searching NYT lead paragraphs: {e}")
        run_metrics["steps"]["nyt_lead_paragraphs"] = {"status": "error", "reason": str(e)}

    # Every feed is its own job, run several at once on the ingest threads:
    # the general news feeds, the NYT articles the Times Wire poller stored,
    # and the Op/Ed feeds (their own table; their counts stay out of the news
    # totals). Each feed's metrics come back to stage_enrich.
    _mark_step(run_metrics, "Fetching feeds (RSS, NYT Times Wire, Op/Ed)")
    payloads = (
        [{"source": "rss", "feed_url": url} for url in active_feed_urls("general")]
        + [{"source": "nyt_wire"}]
        + [{"source": "op_ed", "feed_url": url} for url in active_feed_urls("op_ed")]
    )
    run.fan_out(pipeline_run.FEED_KIND, payloads, then="enrich")


def ingest_feed(payload):
    """One feed job: fetch and store one feed. Returns its metrics."""
    source = payload.get("source")
    if source == "rss":
        return {"source": source, **rss_feed_metrics(payload["feed_url"])}
    if source == "op_ed":
        return {"source": source, **oped_feed_metrics(payload["feed_url"])}
    if source == "nyt_wire":
        return {"source": source, **nyt_api.ingest_wire_items()}
    raise ValueError(f"Unknown feed source {source!r}")


def _collect_feeds(run, run_metrics):
    """The feed jobs' metrics into the run's, as the one-by-one fetch
    reported them."""
    results = run.results(pipeline_run.FEED_KIND)
    run_metrics["rss"] = rss_metrics([r for r in results if r.get("source") == "rss"])
    _add_totals(run_metrics, run_metrics["rss"])
    nyt = next((r for r in results if r.get("source") == "nyt_wire"), None)
    run_metrics["nyt_wire"] = nyt or {"status": "error", "reason": "the NYT Times Wire job failed"}
    if nyt:
        _add_totals(run_metrics, nyt)
    run_metrics["oped_rss"] = oped_rss_metrics([r for r in results if r.get("source") == "op_ed"])
    failed = run.failures(pipeline_run.FEED_KIND)
    if failed:
        logging.error(f"{failed} feed job(s) failed; see Settings › Pipeline › Tasks")
        run_metrics["status"] = "partial_error"
        run_metrics["rss"]["failed_feed_jobs"] = failed


def stage_enrich(run):
    """Card images, then (full runs) bias, grouping review, enrichment,
    copies, headlines, ranking and the edition; then one job per story."""
    run_metrics, ollama_state = run.metrics["run_metrics"], run.metrics["ollama_state"]
    _collect_feeds(run, run_metrics)

    # Cards with no article image fall back to another article's
    # image, then the outlet's logo (news_fetcher/outlet_logos.py). Every
    # run, not only full ones, so new outlets get a logo within the hour.
    _mark_step(run_metrics, "Filling missing card images")
    try:
        run_metrics["steps"]["card_images"] = refresh_card_images()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error filling missing card images: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["card_images"] = {"status": "error", "reason": str(e)}

    if not run.full:
        run_metrics["steps"]["retry_unrated_outlets"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["allsides_sync"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["clear_stale_single_article_headlines"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["review_ambiguous_grouping_matches"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_right_rss_enrichment"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_right_rss_bias_retry"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_right_rss_enrichment_second_pass"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_right_rss_second_pass_bias_retry"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_left_rss_enrichment"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_left_rss_bias_retry"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_left_rss_enrichment_second_pass"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["targeted_left_rss_second_pass_bias_retry"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["headline_generation"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["headline_ranking"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["publish_edition"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["process_current_edition"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["opinion_roundups"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["daily_horoscope"] = {"status": "skipped", "reason": "fetch_only_run"}
        run_metrics["steps"]["static_export"] = {"status": "skipped", "reason": "fetch_only_run"}
        _finish_run(run)
        return

    # Run Bias Checker ONCE after all fetches
    _mark_step(run_metrics, "Retrying unrated outlets (Bias Checker)")
    _check_ollama_status_for_report(ollama_state, "before_retry_unrated_outlets")
    try:
        retry_unrated_outlets()
        run_metrics["steps"]["retry_unrated_outlets"] = {"status": "ok"}
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error checking outlet bias: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["retry_unrated_outlets"] = {"status": "error", "reason": str(e)}

    # Run AllSides sync once a month
    last_sync = get_last_allsides_sync()
    if last_sync is None or (datetime.utcnow() - last_sync).days >= 30:
        _mark_step(run_metrics, "Syncing AllSides bias ratings")
        try:
            sync_allsides_ratings()
            set_last_allsides_sync()
            run_metrics["steps"]["allsides_sync"] = {"status": "ok"}
        except Exception as e:
            db.session.rollback()
            logging.error(f"Error syncing AllSides ratings: {e}")
            run_metrics["status"] = "partial_error"
            run_metrics["steps"]["allsides_sync"] = {"status": "error", "reason": str(e)}
    else:
        days_since = (datetime.utcnow() - last_sync).days
        logging.info(f"--- AllSides sync skipped ({days_since}/30 days) ---")
        run_metrics["steps"]["allsides_sync"] = {
            "status": "skipped",
            "days_since_last_sync": days_since,
        }

    _mark_step(run_metrics, "Running headline ranking")
    _check_ollama_status_for_report(ollama_state, "before_headline_ranking")
    try:
        cleared = clear_stale_single_article_headlines()
        run_metrics["steps"]["clear_stale_single_article_headlines"] = {
            "status": "ok",
            "cleared": cleared,
        }
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error clearing stale single-article headlines: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["clear_stale_single_article_headlines"] = {"status": "error", "reason": str(e)}
    try:
        run_metrics["steps"]["review_ambiguous_grouping_matches"] = review_ambiguous_grouping_matches()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error reviewing ambiguous grouping matches: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["review_ambiguous_grouping_matches"] = {"status": "error", "reason": str(e)}
    # Keyed on the fast provider: the load this guards against is
    # classification and bias rating, both fast-tier, so the global
    # provider is the wrong thing to ask under split routing.
    _mark_step(run_metrics, "Targeted RSS enrichment")
    if llm_client.provider_for_tier(llm_client.TIER_FAST) == "groq":
        # Targeted left/right RSS enrichment adds meaningful classification/
        # bias-rating LLM load on top of the regular fetch; skip it on Groq's
        # free tier. Re-enable once Ollama is back.
        logging.info("--- Skipping targeted left/right RSS enrichment passes (fast tier on groq) ---")
        for step in (
            "targeted_right_rss_enrichment", "targeted_right_rss_bias_retry",
            "targeted_right_rss_enrichment_second_pass", "targeted_right_rss_second_pass_bias_retry",
            "targeted_left_rss_enrichment", "targeted_left_rss_bias_retry",
            "targeted_left_rss_enrichment_second_pass", "targeted_left_rss_second_pass_bias_retry",
        ):
            run_metrics["steps"][step] = {"status": "skipped", "reason": "groq_fast_provider"}
    else:
        _run_targeted_rss_enrichment_pass(RIGHT_RSS_ENRICHMENT_CONFIG, run_metrics, ollama_state)
        _run_targeted_rss_enrichment_pass(LEFT_RSS_ENRICHMENT_CONFIG, run_metrics, ollama_state)

    # Every headline for the run is written here, in one pass. Position
    # matters twice over: it must come after grouping review and both
    # enrichment passes so story membership has settled, and it must sit
    # immediately before ranking -> publish -> summaries, which is the
    # stretch of the run that uses the quality model. Ollama holds one
    # model at a time, so batching here means it loads once and stays
    # loaded for the summary phase instead of being swapped in and out
    # around every grouping and classification call.
    # Copies (one outlet republishing another, or the same wire text
    # under two names) are marked after membership has settled and
    # before headlines, ranking and the edition, which all count
    # sources without them (aggregator/article_copies.py).
    _mark_step(run_metrics, "Marking copied articles")
    try:
        run_metrics["steps"]["article_copies"] = mark_recent_copies()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error marking copied articles: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["article_copies"] = {"status": "error", "reason": str(e)}

    _mark_step(run_metrics, "Generating headlines")
    try:
        run_metrics["steps"]["headline_generation"] = generate_headlines_for_stale_stories()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error generating headlines: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["headline_generation"] = {"status": "error", "reason": str(e)}

    # Single ranking pass for the whole run, positioned after all
    # enrichment so publish_edition() below sees a fully fresh,
    # post-enrichment score. Previously this ran up to 5x per run
    # (once here, plus twice per enrichment direction) — trimmed
    # since collect_all_external_headlines() + the Ollama matching/
    # editorial-rerank calls inside it don't need to be redone
    # mid-run just to catch a few newly-enriched articles.
    try:
        run_metrics["steps"]["headline_ranking"] = run_optional_headline_ranking()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error in headline ranking: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["headline_ranking"] = {"status": "error", "reason": str(e)}

    _mark_step(run_metrics, "Publishing edition")
    try:
        run_metrics["steps"]["publish_edition"] = publish_edition()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error publishing edition: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["publish_edition"] = {"status": "error", "reason": str(e)}

    # Summaries and deep reports, one job per story on the AI thread, behind
    # any NewsBox request (aggregator/workers.py).
    _mark_step(run_metrics, "Processing current edition content")
    _check_ollama_status_for_report(ollama_state, "before_process_current_edition")
    try:
        base, targets = edition_content_targets(backfill_recent=True)
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error processing edition content: {e}")
        run_metrics["status"] = "partial_error"
        base, targets = {"status": "error", "reason": str(e)}, []
    run.metrics["edition_content"] = base
    run.fan_out(pipeline_run.STORY_KIND,
                [{"story_id": story_id, "edition_id": edition_id} for story_id, edition_id in targets],
                then="publish")


def fill_edition_story(payload):
    """One story job: its missing summary, deep report and child summaries."""
    return fill_story(payload["story_id"], payload.get("edition_id"))


def stage_publish(run):
    """Op/Ed roundups, the horoscope, the static export, and the report."""
    run_metrics = run.metrics["run_metrics"]
    base = run.metrics.get("edition_content") or {}
    if base.get("status") == "processed":
        base = edition_content_metrics(base, run.results(pipeline_run.STORY_KIND))
        failed = run.failures(pipeline_run.STORY_KIND)
        if failed:
            run_metrics["status"] = "partial_error"
            base["failed_story_jobs"] = failed
    run_metrics["steps"]["process_current_edition"] = base

    # Op/Ed: link new columns to stories and write roundups. After the
    # edition's summaries (the link check reads them) and before the
    # export, so the published story pages carry the roundups.
    _mark_step(run_metrics, "Linking Op/Ed columns and writing roundups")
    try:
        run_metrics["steps"]["opinion_roundups"] = link_opinion_and_write_roundups()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error linking Op/Ed columns: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["opinion_roundups"] = {"status": "error", "reason": str(e)}

    _mark_step(run_metrics, "Daily horoscope")
    try:
        run_metrics["steps"]["daily_horoscope"] = run_optional_daily_horoscope()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error writing the daily horoscope: {e}")
        run_metrics["steps"]["daily_horoscope"] = {"status": "error", "reason": str(e)}

    _mark_step(run_metrics, "Exporting static site")
    try:
        backfilled_edition_ids = run_metrics["steps"]["process_current_edition"].get(
            "backfilled_edition_ids"
        ) if isinstance(run_metrics["steps"].get("process_current_edition"), dict) else None
        opinion_step = run_metrics["steps"].get("opinion_roundups")
        run_metrics["steps"]["static_export"] = run_optional_static_export(
            extra_edition_ids=backfilled_edition_ids,
            opinion_story_ids=opinion_step.get("changed_story_ids")
            if isinstance(opinion_step, dict) else None,
        )
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error exporting static site: {e}")
        run_metrics["status"] = "partial_error"
        run_metrics["steps"]["static_export"] = {"status": "error", "reason": str(e)}
    _finish_run(run)


def _finish_run(run):
    """Close the last step, save the run's metrics and history, send the
    reports, and mark the run done."""
    run_metrics, ollama_state = run.metrics["run_metrics"], run.metrics["ollama_state"]
    run_full_pipeline = run.full
    _mark_step(run_metrics, None)
    set_last_fetch_time()
    run_metrics["finished_at"] = datetime.utcnow().isoformat()
    ollama_state["up_at_end"] = _check_ollama_status_for_report(ollama_state, "run_end")
    _save_json_setting("last_run_metrics", run_metrics)
    headline_site_metrics = build_headline_site_metrics()
    _save_json_setting("last_headline_site_metrics", headline_site_metrics)
    append_scrape_outcome_history(run_metrics, headline_site_metrics)
    fetch_report = _build_fetch_report(run_metrics, headline_site_metrics, ollama_state)
    _save_json_setting("last_fetch_report", fetch_report)
    logging.info(
        "[Metrics] Run stored=%s input=%s latest_edition=%s %s",
        run_metrics["totals"]["stored"],
        run_metrics["totals"]["input_articles"],
        headline_site_metrics.get("edition", {}).get("date"),
        headline_site_metrics.get("edition", {}).get("edition_type"),
    )
    _notify_fetch_report(fetch_report)

    # Notify n8n pipeline is done — triggers Ollama machine suspend
    _notify_n8n()

    _save_json_setting(FETCH_RUN_STATUS_KEY, {
        "status": "idle",
        "started_at": run_metrics["started_at"],
        "run_full_pipeline": run_full_pipeline,
        "finished_at": run_metrics.get("finished_at"),
        "timings": run_metrics["timings"],
    })

    run.finish(run_metrics.get("status", "ok"))
    logging.info("=== Scheduled fetch run complete ===")


def _run_failed(run, message):
    """A stage errored past its retries: keep what the run recorded."""
    run_metrics = run.metrics.get("run_metrics") or {}
    _mark_step(run_metrics, None)
    run_metrics.update(status="error", error=message, finished_at=datetime.utcnow().isoformat())
    _save_json_setting("last_run_metrics", run_metrics)
    _save_json_setting(FETCH_RUN_STATUS_KEY, {
        "status": "idle",
        "started_at": run_metrics.get("started_at"),
        "run_full_pipeline": run.full,
        "finished_at": run_metrics.get("finished_at"),
        "timings": run_metrics.get("timings") or [],
        "error": message,
    })


pipeline_run.define(
    {"fetch": stage_fetch, "enrich": stage_enrich, "publish": stage_publish},
    feed=ingest_feed,
    story=fill_edition_story,
    failed=_run_failed,
)


def start_pipeline(run_full_pipeline):
    """Start a run (its fetch stage goes on the queue), unless one is going."""
    task_id = "pipeline_full" if run_full_pipeline else "pipeline_fetch"
    with app.app_context():
        run_id = pipeline_run.start(task_id, run_full_pipeline, new_run_metrics(run_full_pipeline))
        return {"run_id": run_id} if run_id else {"skipped": "previous run still going"}


if __name__ == "__main__":
    logging.info("Scheduler starting up...")
    started_at = datetime.utcnow()

    with app.app_context():
        db.create_all()
        jobs.ensure_schema()

    # The job queue's worker threads: the pipeline's stages, feed jobs on
    # INGEST_WORKERS threads, AI work (NewsBox first), and everything else
    # (aggregator/workers.py). Jobs a previous process left running are
    # requeued first, so a run cut off by a restart carries on from the
    # stage it was on. The control tick below is their watchdog.
    workers = Workers(app).start()

    with app.app_context():
        jobs.prune_finished()
        # Only fetch on startup if a scheduled run was missed, and not while
        # an interrupted run is resuming. The run itself is queued as jobs, so
        # it never holds up the interval jobs (Breaking News, NewsBox).
        resuming = pipeline_run.active_run()
        startup_fetch = resuming is None and should_fetch_now()
        startup_full = startup_fetch and should_run_full_pipeline(last_fetch=get_last_fetch_time())
        if resuming is not None:
            logging.info("Resuming pipeline run %s at stage %s.", resuming.id, resuming.stage)
        elif not startup_fetch:
            logging.info("Skipping startup fetch.")
        set_scheduler_started_at()

    def tracked(task_id, func, *args):
        """Run a task in the app context and record its run for the Tasks page."""
        def run():
            with app.app_context():
                begun = datetime.utcnow()
                status, error, result = "ok", None, None
                try:
                    scheduler_tasks.record_start(task_id)
                    result = func(*args)
                except Exception as e:
                    db.session.rollback()
                    status, error = "error", str(e)
                    logging.exception("[Tasks] %s failed", task_id)
                if isinstance(result, dict) and result.get("skipped"):
                    status = "skipped"
                try:
                    scheduler_tasks.record_run(task_id, begun, datetime.utcnow(), status, error, result)
                except Exception:
                    logging.exception("[Tasks] Could not record %s", task_id)
        return run

    def pipeline_starter(full):
        """A scheduled or Run now pipeline run. It only queues the fetch
        stage; the run records itself on the Tasks page when it finishes."""
        def run():
            try:
                result = start_pipeline(full)
                if result.get("skipped"):
                    logging.info("[Tasks] %s skipped: %s", "pipeline_full" if full else "pipeline_fetch",
                                 result["skipped"])
            except Exception:
                logging.exception("[Tasks] Could not start a pipeline run")
        return run

    scheduler = BlockingScheduler()
    if startup_fetch:
        scheduler.add_job(
            pipeline_starter(startup_full),
            trigger="date",
            run_date=datetime.now(timezone.utc),
            id="pipeline_startup",
            name=f"Startup {'full pipeline' if startup_full else 'fetch-only'} run",
            misfire_grace_time=None,
        )

    # The run schedule (Settings › Pipeline › Run schedule), applied on every
    # control tick, so an edit takes effect within CONTROL_SECONDS and no
    # longer needs a restart (which started a catch-up run).
    schedule = {"types": {}, "loaded_at": None}

    def sync_schedule():
        from aggregator.models import PipelineSchedule
        wanted = {f"pipeline_schedule_{e.hour}": (e.hour, e.run_full_pipeline)
                  for e in PipelineSchedule.query.filter_by(is_active=True).all()}
        for job_id in list(schedule["types"]):
            if job_id not in wanted:
                scheduler.remove_job(job_id)
                del schedule["types"][job_id]
        for job_id, (hour, full) in wanted.items():
            if schedule["types"].get(job_id) == full:
                continue
            scheduler.add_job(
                pipeline_starter(full),
                trigger=CronTrigger(hour=hour, minute=0, timezone=TIMEZONE),
                id=job_id,
                name=f"Scheduled {'full pipeline' if full else 'fetch-only'} run at {hour}:00 ({TIMEZONE})",
                replace_existing=True,
            )
            schedule["types"][job_id] = full
        schedule["loaded_at"] = datetime.utcnow()
        db.session.rollback()

    # Each MuckScroller user's scheduled summaries (aggregator/newsbox.py):
    # a cheap check that queues jobs for whatever schedule is due.
    def run_due_newsbox_schedules():
        from aggregator.newsbox import run_due_schedules
        return run_due_schedules()

    # Breaking News (news_fetcher/breaking_feed.py): RSS headlines only,
    # every few minutes, so the Headlines bar and the app see them before
    # the next pipeline run scrapes anything.
    def poll_breaking_feeds():
        from news_fetcher.breaking_feed import poll_feeds
        return poll_feeds()

    # NYT Times Wire (news_fetcher/nyt_api.py): checked every minute, polled
    # on its own schedule so the day's 500 requests last the day.
    def poll_nyt_wire():
        return nyt_api.poll_wire_if_due()

    # NewsAPI and GNews (news_fetcher/fetch_and_store_articles/providers.py):
    # checked every minute, each polled on its own schedule within its daily
    # budget; the next pipeline run stores what they brought.
    def poll_news_apis():
        return poll_news_apis_if_due()

    # Photo focal points (news_fetcher/image_focus.py): every few minutes, so
    # new articles' photos crop around their subject soon after they land.
    def find_image_focus():
        from news_fetcher.image_focus import refresh_image_focus
        return refresh_image_focus()

    from news_fetcher.breaking_feed import POLL_MINUTES as BREAKING_POLL_MINUTES
    from news_fetcher.image_focus import RUN_MINUTES as FOCUS_RUN_MINUTES
    RECURRING = {
        "newsbox_schedules": (run_due_newsbox_schedules, 1, "App scheduled summaries (every minute)"),
        "breaking_feeds": (poll_breaking_feeds, BREAKING_POLL_MINUTES,
                           f"Breaking News feed check (every {BREAKING_POLL_MINUTES} min)"),
        "image_focus": (find_image_focus, FOCUS_RUN_MINUTES, f"Photo focal points (every {FOCUS_RUN_MINUTES} min)"),
        "nyt_wire": (poll_nyt_wire, 1, "NYT Times Wire check (every minute, polls on its schedule)"),
        "news_api_polls": (poll_news_apis, 1, "NewsAPI and GNews check (every minute, poll on their schedules)"),
    }
    for job_id, (func, minutes, name) in RECURRING.items():
        scheduler.add_job(
            tracked(job_id, func),
            trigger="interval",
            minutes=minutes,
            next_run_time=datetime.now(timezone.utc),
            id=job_id,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def control_tick():
        """Every CONTROL_SECONDS: apply the schedule and the Tasks page's
        Pause / Run now, watch the worker threads and pipeline runs, and
        write the heartbeat the page reads."""
        with app.app_context():
            try:
                sync_schedule()
                paused = scheduler_tasks.paused_ids()
                for job in scheduler.get_jobs():
                    if job.id == "control":
                        continue
                    # Pausing "Full pipeline" or "Fetch only" pauses each of its hours.
                    task_id = job.id if job.id in RECURRING else None
                    if job.id in schedule["types"]:
                        task_id = "pipeline_full" if schedule["types"][job.id] else "pipeline_fetch"
                    if task_id is None:
                        continue
                    if task_id in paused and job.next_run_time is not None:
                        job.pause()
                    elif task_id not in paused and job.next_run_time is None:
                        job.resume()
                for task_id in scheduler_tasks.take_run_requests():
                    if task_id in ("pipeline_full", "pipeline_fetch"):
                        full = task_id == "pipeline_full"
                        scheduler.add_job(
                            pipeline_starter(full), trigger="date",
                            run_date=datetime.now(timezone.utc), misfire_grace_time=None,
                            name=f"Run now: {'full pipeline' if full else 'fetch only'}",
                        )
                    elif task_id in RECURRING:
                        func, _, name = RECURRING[task_id]
                        scheduler.add_job(tracked(task_id, func), trigger="date",
                                          run_date=datetime.now(timezone.utc), misfire_grace_time=None,
                                          name=f"Run now: {name}")
                # The watchdog: restart dead worker threads, requeue jobs
                # whose heartbeat stopped, and keep pipeline runs moving.
                workers.check()
                pipeline_run.tick()
                upcoming = [{
                    "id": job.id,
                    "name": job.name,
                    "next_run_at": job.next_run_time.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
                    if job.next_run_time else None,
                } for job in scheduler.get_jobs() if job.id != "control"]
                scheduler_tasks.write_state(started_at, upcoming, workers.all_alive(), schedule["loaded_at"],
                                            workers=workers.status(), restarted=workers.restarted,
                                            requeued=workers.requeued)
            except Exception:
                db.session.rollback()
                logging.exception("[Tasks] Control tick failed")

    with app.app_context():
        sync_schedule()
    scheduler.add_job(
        control_tick,
        trigger="interval",
        seconds=scheduler_tasks.CONTROL_SECONDS,
        next_run_time=datetime.now(timezone.utc),
        id="control",
        name="Tasks page controls and heartbeat",
        max_instances=1,
        coalesce=True,
    )

    logging.info(
        "Scheduler running with %s scheduled run(s) in %s",
        len(schedule["types"]),
        TIMEZONE,
    )
    scheduler.start()
