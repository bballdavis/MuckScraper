"""The scheduler container's job worker threads, and the watchdog over them.

One container, several threads, each running jobs from the queue
(aggregator/jobs.py) for its own kinds, so a long pipeline stage never holds
up a reader's NewsBox summary:

    pipeline  the pipeline's stage jobs, one at a time
    ingest    one feed each (INGEST_WORKERS threads)
    ai        NewsBox summaries, admin AI tasks, article summaries and lean
              ratings, then the pipeline's story summaries, in that order
    general   everything else: bulk actions, Breaking scrapes, snapshots

check() runs on the scheduler's control tick. A thread that died is started
again (its job requeued or failed); a job whose heartbeat stopped for longer
than its kind allows is requeued and its thread replaced, since Python
can't stop a thread stuck in a call. The stuck thread is told to exit when
the call returns, and anything it then records is dropped (jobs._finish
checks the attempt), so a hang becomes a restart instead of a dead run.
"""
import logging
import os
import threading
from dataclasses import dataclass, field

from aggregator import jobs

logger = logging.getLogger(__name__)

INGEST_WORKERS = max(1, int(os.getenv("INGEST_WORKERS", "8")))

PIPELINE_KINDS = ("pipeline_stage",)
INGEST_KINDS = ("pipeline_feed",)
# Reader- and admin-facing AI work first (by priority), batch summaries last.
AI_KINDS = ("newsbox_summary", "ai_task", "article_summary", "article_bias", "pipeline_story")


@dataclass
class Pool:
    name: str
    size: int
    kinds: tuple = ()
    exclude: tuple = ()


def pools():
    return [
        Pool("pipeline", 1, kinds=PIPELINE_KINDS),
        Pool("ingest", INGEST_WORKERS, kinds=INGEST_KINDS),
        Pool("ai", 1, kinds=AI_KINDS),
        Pool("general", 1, exclude=PIPELINE_KINDS + INGEST_KINDS + AI_KINDS),
    ]


@dataclass
class Worker:
    name: str
    pool: Pool
    thread: threading.Thread
    stop: threading.Event = field(default_factory=threading.Event)


class Workers:
    def __init__(self, app):
        self.app = app
        self.workers = {}
        self._spawned = 0
        self.restarted = 0
        self.requeued = 0

    def start(self):
        """Deal with jobs a previous process left running (requeue or fail),
        then start every pool's threads."""
        with self.app.app_context():
            jobs.register_handlers()
            jobs.fail_interrupted_jobs()
        for pool in pools():
            for i in range(pool.size):
                self._spawn(f"{pool.name}-{i + 1}", pool)
        return self

    def _spawn(self, slot, pool):
        self._spawned += 1
        stop = threading.Event()
        thread = threading.Thread(
            target=jobs.worker_loop,
            kwargs={"app": self.app, "stop_event": stop, "kinds": pool.kinds or None,
                    "exclude": pool.exclude or None, "recover": False},
            # A unique thread name: jobs.running_by_thread is keyed on it.
            name=f"job-{slot}#{self._spawned}",
            daemon=True,
        )
        self.workers[slot] = Worker(slot, pool, thread, stop)
        thread.start()

    def check(self):
        """Restart dead threads and requeue stuck jobs. Call in an app context."""
        for slot, worker in list(self.workers.items()):
            if worker.thread.is_alive():
                continue
            logger.error("[Workers] Thread %s died; starting a new one", worker.thread.name)
            held = jobs.running_by_thread.pop(worker.thread.name, None)
            if held:
                if jobs.requeue_or_fail(held[0], "The worker thread running it died.") == "requeued":
                    self.requeued += 1
            self._spawn(slot, worker.pool)
            self.restarted += 1

        holders = {job[0]: name for name, job in list(jobs.running_by_thread.items())}
        for job_id, kind, minutes in jobs.stuck_jobs():
            message = f"No heartbeat for {minutes:.0f} minutes."
            outcome = jobs.requeue_or_fail(job_id, message)
            if outcome == "requeued":
                self.requeued += 1
            thread_name = holders.get(job_id)
            slot = next((s for s, w in self.workers.items() if w.thread.name == thread_name), None)
            if slot is None:
                continue
            logger.error("[Workers] Job %s (%s) on %s is stuck (%s); replacing the thread",
                         job_id, kind, thread_name, message)
            # It exits once the stuck call returns; its result is dropped.
            self.workers[slot].stop.set()
            self._spawn(slot, self.workers[slot].pool)
            self.restarted += 1

    def all_alive(self):
        return all(w.thread.is_alive() for w in self.workers.values())

    def status(self):
        """For the Tasks page heartbeat: each thread and the job it holds."""
        out = []
        for slot, worker in self.workers.items():
            job = jobs.running_by_thread.get(worker.thread.name)
            out.append({"name": slot, "pool": worker.pool.name, "alive": worker.thread.is_alive(),
                        "job_id": job[0] if job else None, "kind": job[1] if job else None})
        return out
