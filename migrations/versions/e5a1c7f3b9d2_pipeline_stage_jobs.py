"""Pipeline stage jobs: jobs get a heartbeat, an attempt count, a run id and
a result, and pipeline_runs holds each run's stage and metrics.

A pipeline run is now a chain of jobs (fetch -> one job per feed -> enrich
-> one job per edition story -> publish), so a scheduler restart resumes at
the stage it was on and the watchdog can requeue a job whose heartbeat
stopped (news_fetcher/pipeline_run.py, aggregator/workers.py).

Written with IF NOT EXISTS: the scheduler adds the same columns at start
(jobs.ensure_schema()) and create_all() makes pipeline_runs, so on a running
install they can already exist.
"""
from alembic import op

revision = "e5a1c7f3b9d2"
down_revision = "c4e9a2f7d1b3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITHOUT TIME ZONE")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id INTEGER")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_run_id ON jobs (run_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id SERIAL PRIMARY KEY,
            task_id VARCHAR(32) NOT NULL,
            run_full_pipeline BOOLEAN NOT NULL DEFAULT TRUE,
            status VARCHAR(16) NOT NULL DEFAULT 'running',
            stage VARCHAR(32) NOT NULL DEFAULT 'fetch',
            started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            finished_at TIMESTAMP WITHOUT TIME ZONE,
            metrics TEXT,
            error TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_pipeline_runs_status ON pipeline_runs (status)")


def downgrade():
    op.drop_table("pipeline_runs")
    op.drop_index("ix_jobs_run_id", table_name="jobs")
    for column in ("result", "run_id", "heartbeat_at", "attempts"):
        op.drop_column("jobs", column)
