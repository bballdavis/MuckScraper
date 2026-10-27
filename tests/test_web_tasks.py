"""Settings › Pipeline › Tasks (admin.tasks_page) and the scheduler state it
reads and writes (aggregator/scheduler_tasks.py).

Needs the scratch Postgres database named by API_TEST_DATABASE_URL, like
test_api; skipped without it.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_DB, "API_TEST_DATABASE_URL not set")
class TasksPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aggregator import create_app, db
        env = {"DATABASE_URL": TEST_DB, "SECRET_KEY": "test-secret"}
        with mock.patch.dict(os.environ, env):
            cls.app = create_app()
        cls.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)
        with cls.app.app_context():
            db.drop_all()
            db.create_all()
            from aggregator.models import PipelineSchedule, User
            admin = User(username="admin", email="a@example.com", role="admin")
            admin.set_password("password123")
            db.session.add_all([admin, PipelineSchedule(hour=7, run_full_pipeline=True, is_active=True)])
            db.session.commit()
            cls.admin_id = admin.id

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        from aggregator import db
        from aggregator.models import AppSetting, Job
        Job.query.delete()
        AppSetting.query.delete()
        db.session.commit()
        db.session.remove()
        self.ctx.pop()

    def client(self):
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.admin_id)
        return client

    def test_pause_and_run_now_round_trip(self):
        from aggregator import scheduler_tasks
        client = self.client()
        client.post("/admin/tasks/breaking_feeds/pause")
        self.assertEqual(scheduler_tasks.paused_ids(), {"breaking_feeds"})
        client.post("/admin/tasks/breaking_feeds/pause", data={"paused": "0"})
        self.assertEqual(scheduler_tasks.paused_ids(), set())
        client.post("/admin/tasks/pipeline_full/run")
        client.post("/admin/tasks/not_a_task/run")
        self.assertEqual(scheduler_tasks.take_run_requests(), ["pipeline_full"])
        self.assertEqual(scheduler_tasks.take_run_requests(), [])

    def test_page_shows_runs_steps_and_a_down_scheduler(self):
        from aggregator import scheduler_tasks
        now = datetime.utcnow()
        scheduler_tasks.record_run("breaking_feeds", now - timedelta(seconds=20), now, "ok",
                                   summary={"feeds": 73, "new": 12, "breaking": 2})
        scheduler_tasks.write_json("fetch_run_status_v1", {
            "status": "running", "started_at": (now - timedelta(minutes=9)).isoformat(),
            "run_full_pipeline": True, "step": "Generating headlines",
            "step_started_at": (now - timedelta(minutes=1)).isoformat(),
            "timings": [{"step": "Fetching RSS feeds", "started_at": now.isoformat(), "seconds": 480}],
        })
        scheduler_tasks.write_json("scrape_outcome_history_v1", [{
            "started_at": (now - timedelta(hours=3)).isoformat(), "run_full_pipeline": False,
            "status": "ok", "duration_seconds": 900, "stored_articles": 40,
            "timings": [{"step": "Fetching RSS feeds", "seconds": 840}, {"step": "Filling missing card images", "seconds": 60}],
        }])
        html = self.client().get("/admin/tasks").get_data(as_text=True)
        self.assertIn("The scheduler isn't running", html)  # no heartbeat yet
        self.assertIn("12 new, 2 breaking", html)
        self.assertIn("Generating headlines", html)
        self.assertIn("Slowest: Fetching RSS feeds (14m)", html)
        self.assertIn("One-off jobs", html)

        scheduler_tasks.write_state(now, [], True, now)
        partial = self.client().get("/admin/tasks?partial=1").get_data(as_text=True)
        self.assertNotIn("isn't running", partial)
        self.assertNotIn("One-off jobs", partial)

    def test_cancel_only_touches_queued_jobs(self):
        from aggregator import db, jobs
        from aggregator.models import Job
        queued = jobs.enqueue("article_bias", {"article_id": None})
        running = Job(kind="article_bias", payload="{}", status="running")
        db.session.add(running)
        db.session.commit()
        client = self.client()
        client.post(f"/admin/tasks/jobs/{queued}/cancel")
        client.post(f"/admin/tasks/jobs/{running.id}/cancel")
        self.assertEqual(db.session.get(Job, queued).status, "cancelled")
        self.assertEqual(db.session.get(Job, running.id).status, "running")

    def test_schedule_edit_waits_only_while_the_scheduler_is_down(self):
        from aggregator import scheduler_tasks
        from aggregator.blueprints.admin.config_crud import pipeline_schedule_restart_needed
        scheduler_tasks.write_json("pipeline_schedule_changed_at", None)
        self.assertFalse(pipeline_schedule_restart_needed())
        from aggregator.models import AppSetting, db
        db.session.add(AppSetting(key="pipeline_schedule_changed_at", value=datetime.utcnow().isoformat()))
        db.session.commit()
        self.assertTrue(pipeline_schedule_restart_needed())
        scheduler_tasks.write_state(datetime.utcnow(), [], True, datetime.utcnow() + timedelta(seconds=1))
        self.assertFalse(pipeline_schedule_restart_needed())


if __name__ == "__main__":
    unittest.main()
