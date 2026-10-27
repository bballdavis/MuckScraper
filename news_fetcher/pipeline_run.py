"""A pipeline run as a chain of jobs on the queue (aggregator/jobs.py).

A run used to be one call holding one lock for an hour or more: a restart
lost it, and nothing else could tell a slow run from a hung one. Now it is
a row in pipeline_runs and a chain of jobs, each queued by the one before:

    fetch    topic fetches, NYT lead paragraphs; queues one job per feed
    feeds    the feed jobs (FEED_KIND), several at once on the ingest threads
    enrich   card images, bias, grouping review, targeted enrichment,
             copies, headlines, ranking, publish; queues one job per story
    stories  the story jobs (STORY_KIND) on the AI thread, behind NewsBox
    publish  Op/Ed roundups, horoscope, static export, the run report

(a fetch-only run stops after enrich's card images). The row's `stage` says
where the run is, and every job retries, so a scheduler restart requeues
the job that was running and the run carries on from that stage. The last
feed or story job to finish queues the next stage; the control tick (tick())
also checks, in case that was missed.

The stage bodies live in news_fetcher/scheduler.py, which hands them over
with define().
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from aggregator import db, jobs, scheduler_tasks
from aggregator.models import PipelineRun

logger = logging.getLogger(__name__)

STAGE_KIND = "pipeline_stage"
FEED_KIND = "pipeline_feed"
STORY_KIND = "pipeline_story"
KINDS = (STAGE_KIND, FEED_KIND, STORY_KIND)
# Story jobs share the AI thread with NewsBox and the admin AI tasks
# (priority 5 and 10), which go first.
STORY_PRIORITY = -5
# Fan-out stages: which job kind each waits on.
WAITING = {"feeds": FEED_KIND, "stories": STORY_KIND}

# A job with no heartbeat for this long is requeued by the watchdog
# (aggregator/workers.py). Every LLM call, scraped page and stored article
# beats, so only a hang goes this long; a stage's steps get more room for
# the plugins (export, ranking) that don't beat.
STAGE_STUCK_AFTER = timedelta(minutes=45)
FEED_STUCK_AFTER = timedelta(minutes=10)
STORY_STUCK_AFTER = timedelta(minutes=20)
# A run still going after this is given up on.
RUN_GIVE_UP_AFTER = timedelta(hours=12)

FETCH_RUN_STATUS_KEY = "fetch_run_status_v1"

_stages = {}
_handlers = {}
_start_lock = threading.Lock()


def define(stages, feed, story, failed):
    """Hand over the stage bodies: `stages` maps a stage name to fn(run),
    `feed` and `story` take a job's payload and return its metrics, and
    `failed(run, message)` closes the books on a run that errored."""
    _stages.update(stages)
    _handlers.update(feed=feed, story=story, failed=failed)


class Run:
    """One pipeline run, as a stage body sees it. `metrics` is saved with
    the row when the stage moves on."""

    def __init__(self, row):
        self.row = row
        self.id = row.id
        self.full = row.run_full_pipeline
        self.task_id = row.task_id
        self.metrics = json.loads(row.metrics) if row.metrics else {}

    def save(self):
        self.row.metrics = json.dumps(self.metrics, default=str)
        db.session.commit()

    def results(self, kind):
        """What this run's jobs of `kind` returned, in the order queued."""
        rows = db.session.execute(db.text(
            "SELECT result FROM jobs WHERE run_id = :run AND kind = :kind AND status = 'done' ORDER BY id"
        ), {"run": self.id, "kind": kind}).fetchall()
        return [json.loads(row.result) for row in rows if row.result]

    def failures(self, kind):
        return db.session.execute(db.text(
            "SELECT count(*) FROM jobs WHERE run_id = :run AND kind = :kind AND status = 'error'"
        ), {"run": self.id, "kind": kind}).scalar() or 0

    def next(self, stage):
        """Queue the next stage."""
        self.row.stage = stage
        self.save()
        _enqueue_stage(self.id, stage)

    def fan_out(self, kind, payloads, then):
        """Queue one `kind` job per payload; stage `then` runs when they're
        all done. A stage rerun after a restart doesn't queue them twice."""
        waiting = next(name for name, k in WAITING.items() if k == kind)
        self.metrics["_then"] = then
        self.row.stage = waiting
        self.save()
        already = db.session.execute(db.text(
            "SELECT count(*) FROM jobs WHERE run_id = :run AND kind = :kind"
        ), {"run": self.id, "kind": kind}).scalar()
        if not already and payloads:
            priority = STORY_PRIORITY if kind == STORY_KIND else 0
            jobs.enqueue_many(kind, [dict(p, run_id=self.id) for p in payloads], priority=priority, run_id=self.id)
        advance(self.id)

    def finish(self, status="ok", error=None):
        self.row.status = "error" if status == "error" else "done"
        self.row.stage = "done"
        self.row.finished_at = datetime.utcnow()
        self.row.error = (error or "")[:2000] or None
        self.save()
        scheduler_tasks.record_run(self.task_id, self.row.started_at, self.row.finished_at,
                                   "error" if status == "error" else "ok", error,
                                   {"run": self.id, "stage_status": status})


def active_run():
    return PipelineRun.query.filter_by(status="running").order_by(PipelineRun.id.desc()).first()


def start(task_id, full, metrics):
    """Start a run unless one is going. Returns its id, or None (skipped)."""
    with _start_lock:
        if active_run() is not None:
            db.session.rollback()
            logger.info("Skipping a pipeline run: the previous one is still going.")
            return None
        row = PipelineRun(task_id=task_id, run_full_pipeline=full, stage="fetch",
                          started_at=datetime.utcnow(), metrics=json.dumps(metrics, default=str))
        db.session.add(row)
        db.session.commit()
        run_id = row.id
    scheduler_tasks.record_start(task_id)
    _enqueue_stage(run_id, "fetch")
    logger.info("=== Pipeline run %s started (%s) ===", run_id, task_id)
    return run_id


def _enqueue_stage(run_id, stage):
    jobs.enqueue(STAGE_KIND, {"run_id": run_id, "stage": stage}, run_id=run_id)


def _running_row(payload):
    row = db.session.get(PipelineRun, payload.get("run_id"))
    if row is None or row.status != "running":
        return None
    return row


def _run_stage(payload):
    row = _running_row(payload)
    if row is None or row.stage != payload.get("stage"):
        logger.info("[Pipeline] Stage job %s is stale; skipped", payload)
        return {"skipped": "stale"}
    logger.info("[Pipeline] Run %s: stage %s", row.id, row.stage)
    _stages[row.stage](Run(row))
    return {"stage": payload.get("stage")}


def _fail_stage(payload, message):
    row = _running_row(payload)
    if row is None:
        return
    logger.error("[Pipeline] Run %s failed at stage %s: %s", row.id, row.stage, message)
    run = Run(row)
    try:
        _handlers["failed"](run, message)
    except Exception:
        db.session.rollback()
        logger.exception("[Pipeline] Could not close the books on run %s", row.id)
    run.finish("error", message)


def _run_part(handler):
    def run(payload):
        if _running_row(payload) is None:
            return {"skipped": "run_not_running"}
        return _handlers[handler](payload)
    return run


def _part_done(payload, message=None):
    """After a feed or story job, success or not: show progress and queue
    the next stage if it was the last one."""
    run_id = payload.get("run_id")
    try:
        _show_progress(run_id)
        advance(run_id)
    except Exception:
        db.session.rollback()
        logger.exception("[Pipeline] Could not advance run %s", run_id)


def advance(run_id):
    """Queue the stage after a fan-out once none of its jobs are left.
    Returns True when this call did it (one caller wins the race)."""
    row = db.session.get(PipelineRun, run_id)
    if row is None or row.status != "running" or row.stage not in WAITING:
        db.session.rollback()
        return False
    waiting = row.stage
    outstanding = db.session.execute(db.text(
        "SELECT count(*) FROM jobs WHERE run_id = :run AND kind = :kind AND status IN ('queued', 'running')"
    ), {"run": run_id, "kind": WAITING[waiting]}).scalar()
    if outstanding:
        db.session.rollback()
        return False
    then = (json.loads(row.metrics or "{}")).get("_then")
    moved = db.session.execute(db.text(
        "UPDATE pipeline_runs SET stage = :then WHERE id = :id AND stage = :waiting AND status = 'running'"
    ), {"then": then, "id": run_id, "waiting": waiting}).rowcount
    db.session.commit()
    if moved:
        _enqueue_stage(run_id, then)
    return bool(moved)


def _show_progress(run_id):
    """'Fetching RSS feeds (37 of 160)' on the live run, from the jobs."""
    row = db.session.get(PipelineRun, run_id)
    if row is None or row.stage not in WAITING:
        db.session.rollback()
        return
    counts = dict(db.session.execute(db.text(
        "SELECT status IN ('queued', 'running') AS open, count(*) FROM jobs "
        "WHERE run_id = :run AND kind = :kind GROUP BY 1"
    ), {"run": run_id, "kind": WAITING[row.stage]}).fetchall())
    metrics = json.loads(row.metrics or "{}")
    db.session.rollback()
    timings = (metrics.get("run_metrics") or {}).get("timings") or []
    if not timings:
        return
    total = sum(counts.values())
    done = counts.get(False, 0)
    write_status(metrics["run_metrics"], f"{timings[-1]['step']} ({done} of {total})", timings[-1]["started_at"])


def write_status(run_metrics, step, step_started_at):
    """The live run on Settings › Pipeline › Tasks."""
    scheduler_tasks.write_json(FETCH_RUN_STATUS_KEY, {
        "status": "running",
        "started_at": run_metrics["started_at"],
        "run_full_pipeline": run_metrics.get("run_full_pipeline"),
        "step": step,
        "step_started_at": step_started_at,
        "timings": run_metrics.get("timings") or [],
    })


def mark_step(run_metrics, label):
    """Log a pipeline step and time it. Closes the previous step's timing in
    run_metrics["timings"] and shows the new one as the live run's current
    step. None closes the last step."""
    if label is not None:
        logging.info(f"--- {label} ---")
    jobs.beat()
    now = datetime.utcnow()
    timings = run_metrics.setdefault("timings", [])
    if timings and timings[-1].get("seconds") is None:
        started = datetime.fromisoformat(timings[-1]["started_at"])
        timings[-1]["seconds"] = round((now - started).total_seconds(), 1)
    if label is None:
        return
    timings.append({"step": label, "started_at": now.isoformat(), "seconds": None})
    try:
        write_status(run_metrics, label, now.isoformat())
    except Exception:
        logging.exception("Could not record the pipeline step")


def tick():
    """From the scheduler's control tick: move on a fan-out whose last job
    finished without doing it, requeue a stage whose job went missing, and
    give up on a run that has gone on far too long."""
    now = datetime.utcnow()
    for row in PipelineRun.query.filter_by(status="running").all():
        run_id, stage, started_at = row.id, row.stage, row.started_at
        db.session.rollback()
        if started_at and now - started_at > RUN_GIVE_UP_AFTER:
            _fail_stage({"run_id": run_id}, f"Gave up after {RUN_GIVE_UP_AFTER}")
            continue
        if stage in WAITING:
            advance(run_id)
            continue
        recent = db.session.execute(db.text(
            "SELECT count(*) FROM jobs WHERE run_id = :run AND (status IN ('queued', 'running') "
            "OR finished_at > (now() at time zone 'utc') - interval '1 minute')"
        ), {"run": run_id}).scalar()
        db.session.rollback()
        if not recent:
            logger.warning("[Pipeline] Run %s had no job for stage %s; queueing it again", run_id, stage)
            _enqueue_stage(run_id, stage)


def _register():
    jobs.register(STAGE_KIND, _run_stage, _fail_stage, retry=True, stuck_after=STAGE_STUCK_AFTER)
    jobs.register(FEED_KIND, _run_part("feed"), _part_done, retry=True, stuck_after=FEED_STUCK_AFTER,
                  after=_part_done)
    jobs.register(STORY_KIND, _run_part("story"), _part_done, retry=True, stuck_after=STORY_STUCK_AFTER,
                  after=_part_done)


_register()
