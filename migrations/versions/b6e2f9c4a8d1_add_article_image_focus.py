"""Store where each article photo's subject is (articles.image_focus_*).

Revision ID: b6e2f9c4a8d1
Revises: f3c8a1e6d2b9
Create Date: 2026-10-27

`image_focus_x` / `image_focus_y` are the photo's focal point, 0..1 from the
left and top, so crops keep the subject in frame; `image_focus_at` marks the
photo as checked (news_fetcher/image_focus.py). Additive and nullable: a
photo with no focus is cropped around its center, as before.
"""
from alembic import op


revision = 'b6e2f9c4a8d1'
down_revision = 'f3c8a1e6d2b9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_focus_x DOUBLE PRECISION")
    op.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_focus_y DOUBLE PRECISION")
    op.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_focus_at TIMESTAMP WITHOUT TIME ZONE")


def downgrade():
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS image_focus_at")
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS image_focus_y")
    op.execute("ALTER TABLE articles DROP COLUMN IF EXISTS image_focus_x")
