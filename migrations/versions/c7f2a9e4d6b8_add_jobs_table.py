"""Add the jobs table: a Postgres job queue for background work.

Revision ID: c7f2a9e4d6b8
Revises: b5e8d1c3a7f2
Create Date: 2026-10-22

Admin bulk actions and per-story/article AI tasks used to run in threads
inside gunicorn workers, where a worker restart or timeout killed them
mid-run. They are now rows here, run by the scheduler container's worker
(aggregator/jobs.py).
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7f2a9e4d6b8'
down_revision = 'b5e8d1c3a7f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_jobs_queued", "jobs", ["status", "priority", "id"])


def downgrade():
    op.drop_index("ix_jobs_queued", table_name="jobs")
    op.drop_table("jobs")
