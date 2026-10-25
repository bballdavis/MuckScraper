"""Per-article AI work asked for from /api/v1: the article's own summary (what
the web article page's summary button writes) and its own lean rating (the
page's Rate bias). Both run as jobs in the scheduler's worker; the API only
queues them and the article reply says which are still waiting.

A rated article's bias_score overrides its outlet's in the story's L / C / R
counts, as on the web.
"""
import json

from aggregator import db, jobs
from aggregator.models import Article, Job

SUMMARY_JOB = "article_summary"
BIAS_JOB = "article_bias"
JOB_PRIORITY = 5


def pending(article_id):
    """The kinds of this article's jobs that are queued or running."""
    rows = (db.session.query(Job.kind, Job.payload)
            .filter(Job.kind.in_((SUMMARY_JOB, BIAS_JOB)), Job.status.in_(("queued", "running"))).all())
    return {kind for kind, payload in rows if json.loads(payload or "{}").get("article_id") == article_id}


def queue(kind, article_id):
    """Queue `kind` for the article unless it is already waiting. Returns
    whether a job was added."""
    if kind in pending(article_id):
        return False
    jobs.enqueue(kind, {"article_id": article_id}, priority=JOB_PRIORITY)
    return True


def run_summary(payload):
    article = Article.query.get(payload.get("article_id"))
    if article is None or (article.summary or "").strip():
        return
    from news_fetcher.llm_client import TIER_QUALITY
    from news_fetcher.summarizer import check_ollama_status, summarize_article
    if not check_ollama_status(TIER_QUALITY):
        raise RuntimeError("The summarization model is offline.")
    summary = summarize_article(article)
    if not summary:
        raise RuntimeError("No article summary was written.")
    article.summary = summary
    db.session.commit()


def run_bias(payload):
    article = Article.query.get(payload.get("article_id"))
    if article is None:
        return
    from aggregator import article_lean
    from news_fetcher.outlet_bias_llm import get_article_lean_from_llm
    score, reason = get_article_lean_from_llm(article.title, article.content)
    if score is None:
        raise RuntimeError("The model couldn't rate this article.")
    article_lean.apply_ai(article, score, reason)


jobs.register(SUMMARY_JOB, run_summary)
jobs.register(BIAS_JOB, run_bias)
