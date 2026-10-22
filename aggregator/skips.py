"""Stories a user passed on in the app's Swipe deck (models.StorySkip).

A skip only stops scheduled runs (newsbox.pick_stories) from summarizing the
story for that user and drops it from their deck; asking for it from the
story page still works. Skipping is idempotent; unskipping something never
skipped does nothing. Callers commit.
"""
from datetime import datetime

from aggregator import db
from aggregator.models import Story, StorySkip


def skip(user_id, story_id, when=None):
    """Returns the StorySkip, or None when the story doesn't exist."""
    if Story.query.get(story_id) is None:
        return None
    row = StorySkip.query.filter_by(user_id=user_id, story_id=story_id).first()
    if row is None:
        row = StorySkip(user_id=user_id, story_id=story_id, skipped_at=when or datetime.utcnow())
        db.session.add(row)
        db.session.flush()
    return row


def unskip(user_id, story_id):
    """Returns whether it had been skipped."""
    return bool(StorySkip.query.filter_by(user_id=user_id, story_id=story_id).delete(synchronize_session=False))


def skipped_ids(user_id, ids=None):
    """The story ids this user skipped, optionally only among `ids`."""
    query = db.session.query(StorySkip.story_id).filter(StorySkip.user_id == user_id)
    if ids is not None:
        ids = list(ids)
        if not ids:
            return set()
        query = query.filter(StorySkip.story_id.in_(ids))
    return {row[0] for row in query.all()}


def recent(user_id, offset=0, limit=20):
    """This user's skips, most recent first."""
    return (StorySkip.query.filter_by(user_id=user_id)
            .order_by(StorySkip.skipped_at.desc(), StorySkip.id.desc())
            .offset(offset).limit(limit).all())
