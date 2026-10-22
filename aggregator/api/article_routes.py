"""Ask for an article's own summary or lean rating, as the web article page's
round buttons do. Nothing runs a model in the request: the work is queued
for the scheduler's worker (aggregator/article_jobs.py) and the reply is the
article, with is_summarizing / is_rating_bias set while it waits. Poll
/articles/{id} to see the result. Both need the summarize scope and count
against the daily summary limit.
"""
from flask import g, jsonify

from aggregator import article_jobs

from . import api
from .auth import require_token
from .errors import ApiError
from .params import query_args
from .ratelimit import check_summaries
from .routes import article_payload, load_article


@api.route("/articles/<int:article_id>/summarize", methods=["POST"])
@require_token("summarize")
def summarize_article(article_id):
    """200 when it already has a summary, 202 once one is queued."""
    query_args()
    row = load_article(article_id)
    if (row.summary or "").strip():
        return jsonify(article_payload(row))
    if not (row.content or "").strip():
        raise ApiError(409, "no_text", "MuckScraper couldn't read this article, so there is nothing to summarize.")
    if article_jobs.SUMMARY_JOB not in article_jobs.pending(row.id):
        check_summaries(g.user)
        article_jobs.queue(article_jobs.SUMMARY_JOB, row.id)
    return jsonify(article_payload(row)), 202


@api.route("/articles/<int:article_id>/rate-bias", methods=["POST"])
@require_token("summarize")
def rate_article_bias(article_id):
    """Rate the article's own lean from its title and opening (202). The
    rating replaces the outlet's for this article, in its leaning and in the
    story's L / C / R counts."""
    query_args()
    row = load_article(article_id)
    if article_jobs.BIAS_JOB not in article_jobs.pending(row.id):
        check_summaries(g.user)
        article_jobs.queue(article_jobs.BIAS_JOB, row.id)
    return jsonify(article_payload(row)), 202
