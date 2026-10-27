"""Tests for the pipeline as stage jobs (news_fetcher/pipeline_run.py), the
job queue's retries and heartbeat (aggregator/jobs.py), the worker threads'
watchdog (aggregator/workers.py) and parallel ingestion
(news_fetcher/fetch_and_store_articles/ingestion.py).

They run against the scratch Postgres database named by
API_TEST_DATABASE_URL, like tests/test_api.py, and are skipped without it.
Its tables are dropped and recreated.
"""
import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


class _DbTest(unittest.TestCase):
    def setUp(self):
        from aggregator import create_app, db
        with mock.patch.dict(os.environ, {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}):
            self.app = create_app()
            # Builds its own app at import time.
            import news_fetcher.fetch_and_store_articles  # noqa: F401
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        from aggregator import db
        db.session.remove()
        self.ctx.pop()

    def job(self, job_id):
        from aggregator import db
        from aggregator.models import Job
        db.session.expire_all()
        return db.session.get(Job, job_id)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pipeline job tests")
class JobRetryTests(_DbTest):
    def setUp(self):
        super().setUp()
        from aggregator import jobs
        self.calls = []
        jobs.register("t_retry", lambda p: self.calls.append(p) or {"n": len(self.calls)}, retry=True,
                      stuck_after=timedelta(minutes=5))
        jobs.register("t_once", lambda p: None)

    def test_claim_counts_attempts_and_keeps_the_result(self):
        from aggregator import jobs
        job_id = jobs.enqueue("t_retry", {"x": 1})
        self.assertTrue(jobs.run_next_job(kinds=("t_retry",)))
        job = self.job(job_id)
        self.assertEqual((job.status, job.attempts), ("done", 1))
        self.assertEqual(json.loads(job.result), {"n": 1})
        self.assertIsNotNone(job.heartbeat_at)

    def test_a_restart_requeues_retry_kinds_and_fails_the_rest(self):
        from aggregator import db, jobs
        retry_id = jobs.enqueue("t_retry")
        once_id = jobs.enqueue("t_once")
        db.session.execute(db.text("UPDATE jobs SET status = 'running', attempts = 1"))
        db.session.commit()
        self.assertEqual(jobs.fail_interrupted_jobs(), 2)
        self.assertEqual(self.job(retry_id).status, "queued")
        self.assertEqual(self.job(once_id).status, "error")

    def test_a_job_is_failed_after_max_attempts(self):
        from aggregator import db, jobs
        job_id = jobs.enqueue("t_retry")
        db.session.execute(db.text("UPDATE jobs SET status = 'running', attempts = :n"), {"n": jobs.MAX_ATTEMPTS})
        db.session.commit()
        self.assertEqual(jobs.requeue_or_fail(job_id, "stuck"), "failed")
        self.assertEqual(self.job(job_id).status, "error")

    def test_a_requeued_jobs_late_finish_is_dropped(self):
        """The watchdog requeued it while its old thread was still in a
        call; when that call returns, its finish must not count."""
        from aggregator import jobs
        job_id = jobs.enqueue("t_retry")
        claimed = jobs._claim_next(kinds=("t_retry",))
        self.assertEqual(jobs.requeue_or_fail(job_id, "stuck"), "requeued")
        self.assertFalse(jobs._finish(job_id, attempts=claimed[3]))
        self.assertEqual(self.job(job_id).status, "queued")

    def test_stuck_jobs_are_the_ones_with_an_old_heartbeat(self):
        from aggregator import db, jobs
        old = jobs.enqueue("t_retry")
        fresh = jobs.enqueue("t_retry")
        once = jobs.enqueue("t_once")
        db.session.execute(db.text(
            "UPDATE jobs SET status = 'running', heartbeat_at = (now() at time zone 'utc') - interval '10 minutes'"
        ))
        db.session.execute(db.text("UPDATE jobs SET heartbeat_at = (now() at time zone 'utc') WHERE id = :id"),
                           {"id": fresh})
        db.session.commit()
        self.assertEqual([j[0] for j in jobs.stuck_jobs()], [old])
        self.assertNotIn(once, [j[0] for j in jobs.stuck_jobs()])

    def test_newsbox_goes_ahead_of_batch_story_summaries(self):
        from aggregator import jobs
        from aggregator.workers import AI_KINDS
        jobs.enqueue("pipeline_story", {}, priority=-5)
        newsbox = jobs.enqueue("newsbox_summary", {}, priority=5)
        self.assertEqual(jobs._claim_next(kinds=AI_KINDS)[0], newsbox)


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pipeline job tests")
class PipelineRunTests(_DbTest):
    def setUp(self):
        super().setUp()
        from news_fetcher import pipeline_run
        self.pr = pipeline_run
        self.seen = []

        def fetch(run):
            run.metrics["steps"] = ["fetch"]
            run.fan_out(pipeline_run.FEED_KIND, [{"feed": n} for n in range(3)], then="enrich")

        def enrich(run):
            run.metrics["steps"].append("enrich")
            run.metrics["feeds"] = sorted(r["feed"] for r in run.results(pipeline_run.FEED_KIND))
            run.fan_out(pipeline_run.STORY_KIND, [], then="publish")

        def publish(run):
            run.metrics["steps"].append("publish")
            run.finish("ok")

        def feed(payload):
            self.seen.append(payload["feed"])
            return {"feed": payload["feed"]}

        self.saved = (dict(pipeline_run._stages), dict(pipeline_run._handlers))
        pipeline_run.define({"fetch": fetch, "enrich": enrich, "publish": publish},
                            feed=feed, story=lambda p: {}, failed=lambda run, message: None)

    def tearDown(self):
        self.pr._stages.clear()
        self.pr._stages.update(self.saved[0])
        self.pr._handlers.update(self.saved[1])
        super().tearDown()

    def drain(self):
        from aggregator import jobs
        while jobs.run_next_job(kinds=self.pr.KINDS):
            pass

    def run_row(self, run_id):
        from aggregator import db
        from aggregator.models import PipelineRun
        db.session.expire_all()
        return db.session.get(PipelineRun, run_id)

    def test_a_run_goes_through_its_stages(self):
        run_id = self.pr.start("pipeline_full", True, {})
        self.drain()
        row = self.run_row(run_id)
        self.assertEqual((row.status, row.stage), ("done", "done"))
        metrics = json.loads(row.metrics)
        self.assertEqual(metrics["steps"], ["fetch", "enrich", "publish"])
        self.assertEqual(metrics["feeds"], [0, 1, 2])

    def test_only_one_run_at_a_time(self):
        self.assertIsNotNone(self.pr.start("pipeline_full", True, {}))
        self.assertIsNone(self.pr.start("pipeline_fetch", False, {}))

    def test_a_restart_resumes_at_the_stage_it_was_on(self):
        from aggregator import jobs
        run_id = self.pr.start("pipeline_full", True, {})
        jobs.run_next_job(kinds=(self.pr.STAGE_KIND,))   # fetch: queues three feeds
        jobs.run_next_job(kinds=(self.pr.FEED_KIND,))    # one feed done
        jobs._claim_next(kinds=(self.pr.FEED_KIND,))      # one cut off by the restart
        # The new process: the cut-off feed goes back in the queue.
        jobs.fail_interrupted_jobs()
        self.assertEqual(self.run_row(run_id).stage, "feeds")
        self.drain()
        row = self.run_row(run_id)
        self.assertEqual(row.status, "done")
        self.assertEqual(json.loads(row.metrics)["feeds"], [0, 1, 2])
        self.assertEqual(sorted(self.seen), [0, 1, 2])

    def test_tick_moves_on_a_fan_out_whose_last_job_missed_it(self):
        from aggregator import db, jobs
        run_id = self.pr.start("pipeline_full", True, {})
        jobs.run_next_job(kinds=(self.pr.STAGE_KIND,))
        # The feeds finished but nothing queued the next stage.
        db.session.execute(db.text("UPDATE jobs SET status = 'done', result = '{\"feed\": 0}' "
                                   "WHERE kind = 'pipeline_feed'"))
        db.session.commit()
        self.pr.tick()
        self.assertEqual(self.run_row(run_id).stage, "enrich")

    def test_a_failed_stage_ends_the_run(self):
        from aggregator import jobs

        def broken(run):
            raise RuntimeError("boom")
        self.pr._stages["fetch"] = broken
        run_id = self.pr.start("pipeline_full", True, {})
        jobs.run_next_job(kinds=(self.pr.STAGE_KIND,))
        row = self.run_row(run_id)
        self.assertEqual(row.status, "error")
        self.assertIn("boom", row.error)
        self.assertIsNone(self.pr.active_run())


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pipeline job tests")
class WatchdogTests(_DbTest):
    def setUp(self):
        super().setUp()
        from aggregator import jobs
        # LISTEN/NOTIFY is psycopg2's API; poll instead, quickly.
        patches = [mock.patch.object(jobs, "_listen_connection", return_value=(None, None)),
                   mock.patch.object(jobs, "_wait_for_notify", side_effect=lambda conn, t: time.sleep(0.05))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_dead_thread_is_restarted_and_its_job_requeued(self):
        from aggregator import jobs, workers
        jobs.register("t_retry", lambda p: None, retry=True)
        w = workers.Workers(self.app)
        dead = threading.Thread(target=lambda: None, name="job-x#1")
        dead.start()
        dead.join()
        w.workers["ingest-1"] = workers.Worker("ingest-1", workers.Pool("ingest", 1, kinds=("t_none",)), dead)
        job_id = jobs.enqueue("t_retry")
        jobs._claim_next(kinds=("t_retry",))
        jobs.running_by_thread["job-x#1"] = (job_id, "t_retry", 1)
        w.check()
        self.assertEqual(self.job(job_id).status, "queued")
        self.assertTrue(w.workers["ingest-1"].thread.is_alive())
        self.assertEqual(w.restarted, 1)
        w.workers["ingest-1"].stop.set()

    def test_a_stuck_job_is_requeued_and_its_thread_replaced(self):
        from aggregator import db, jobs, workers
        release = threading.Event()
        jobs.register("t_hang", lambda p: release.wait(10), retry=True, stuck_after=timedelta(minutes=1))
        w = workers.Workers(self.app)
        w._spawn("ingest-1", workers.Pool("ingest", 1, kinds=("t_hang",)))
        job_id = jobs.enqueue("t_hang")
        for _ in range(50):
            if self.job(job_id).status == "running":
                break
            time.sleep(0.1)
        old_thread = w.workers["ingest-1"].thread
        db.session.execute(db.text(
            "UPDATE jobs SET heartbeat_at = (now() at time zone 'utc') - interval '5 minutes' WHERE id = :id"
        ), {"id": job_id})
        db.session.commit()
        w.check()
        self.assertIsNot(w.workers["ingest-1"].thread, old_thread)
        self.assertEqual(w.requeued, 1)
        # The replacement picks the job up again; let both finish.
        release.set()
        for _ in range(50):
            if self.job(job_id).status == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.job(job_id).status, "done")
        self.assertEqual(self.job(job_id).attempts, 2)
        w.workers["ingest-1"].stop.set()


@unittest.skipUnless(TEST_DB, "set API_TEST_DATABASE_URL to run the pipeline job tests")
class ParallelIngestTests(_DbTest):
    def setUp(self):
        super().setUp()
        from aggregator import db
        db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.session.commit()

    def _articles(self, prefix, n, shared=()):
        now = datetime.utcnow()
        own = [{"title": f"{prefix} story number {i} about a distinct topic {i}", "content": "",
                "url": f"https://{prefix}.example/{i}", "source_name": f"{prefix} News", "published_at": now}
               for i in range(n)]
        return own + [dict(a) for a in shared]

    def test_two_feeds_at_once_store_a_shared_article_once(self):
        from aggregator import db
        from aggregator.models import Article
        from news_fetcher.fetch_and_store_articles import ingestion
        from news_fetcher.scraper import ScrapeResult

        def slow_scrape(url, fallback_content=None):
            time.sleep(0.2)
            return ScrapeResult(content=f"<p>Body of {url}</p>", status="success")

        wire = [{"title": "Wire: senate passes the budget bill late at night", "content": "",
                 "url": "https://wire.example/budget", "source_name": "Wire Service",
                 "published_at": datetime.utcnow()}]
        app = self.app
        out = {}

        def run(name, batch):
            with app.app_context():
                try:
                    out[name] = ingestion.store_articles(batch, "Global News", provider="rss")
                finally:
                    db.session.remove()

        with mock.patch.object(ingestion, "scrape_article", side_effect=slow_scrape), \
                mock.patch.object(ingestion, "classify_article", return_value=["Other"]), \
                mock.patch.object(ingestion, "get_outlet_bias_from_llm", return_value=None), \
                mock.patch.object(ingestion, "get_allsides_score", return_value=None), \
                mock.patch.object(ingestion.llm_client, "get_embeddings", side_effect=lambda t: [None] * len(t)), \
                mock.patch("news_fetcher.story_grouper.llm_client.is_configured", return_value=False):
            started = time.monotonic()
            threads = [threading.Thread(target=run, args=(n, self._articles(n, 4, wire))) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            took = time.monotonic() - started

        db.session.expire_all()
        self.assertEqual(Article.query.filter_by(url="https://wire.example/budget").count(), 1)
        self.assertEqual(Article.query.count(), 9)
        self.assertEqual(out["a"]["stored"] + out["b"]["stored"], 9)
        # Ten 0.2s scrapes, side by side rather than one after another.
        self.assertLess(took, 1.5)


if __name__ == "__main__":
    unittest.main()
