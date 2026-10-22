"""The capture container: PDFs and screenshots of saved articles' pages
for bookmark time capsules (aggregator/bookmarks.py).

Only this container has a headless browser (Dockerfile.capture). It runs a
job worker that claims nothing but "bookmark_capture" jobs, and beats a
heartbeat so the scheduler's worker knows captures can be queued.

    python -m aggregator.capture

The browser can only load public http(s) addresses: every request a page
makes is checked by aggregator.safe_fetch and refused if it points at the
server's own network. JavaScript runs (pages need it to render) in a fresh
context per page, with downloads off.
"""
import logging
import threading
from urllib.parse import urlparse

from aggregator import bookmarks, jobs
from aggregator.models import BookmarkSnapshot, db
from aggregator.safe_fetch import BlockedURL, check_url, host_is_public

logger = logging.getLogger(__name__)

PAGE_TIMEOUT_MS = 30_000
SETTLE_MS = 1_500
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_SHOT_BYTES = 10 * 1024 * 1024
# Screenshots stop at this height so one endless page can't fill the disk.
MAX_SHOT_HEIGHT = 12_000
VIEWPORT = {"width": 1280, "height": 900}
HEARTBEAT_SECONDS = 60


def _guard(route):
    """Let a page load only public http(s) resources."""
    url = route.request.url
    parsed = urlparse(url)
    if parsed.scheme in ("data", "blob", "about"):
        return route.continue_()
    try:
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BlockedURL(url)
        host_is_public(parsed.hostname)
    except BlockedURL:
        return route.abort("blockedbyclient")
    return route.continue_()


def capture_page(browser, url):
    """(pdf bytes, jpeg bytes) for one page; either may be None."""
    check_url(url)
    context = browser.new_context(viewport=VIEWPORT, accept_downloads=False, java_script_enabled=True,
                                  user_agent="Mozilla/5.0 (compatible; MuckScraper time capsule)")
    try:
        context.route("**/*", _guard)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        height = min(page.evaluate("document.documentElement.scrollHeight") or VIEWPORT["height"], MAX_SHOT_HEIGHT)
        shot = page.screenshot(type="jpeg", quality=70,
                               clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height},
                               full_page=True)
        page.emulate_media(media="print")
        pdf = page.pdf(format="Letter", print_background=True)
        return (pdf if len(pdf) <= MAX_PDF_BYTES else None,
                shot if len(shot) <= MAX_SHOT_BYTES else None)
    finally:
        context.close()


def run_capture(payload, browser_factory=None):
    """Job handler: capture each saved article's page into the version."""
    snapshot = BookmarkSnapshot.query.get(payload["snapshot_id"])
    if snapshot is None or snapshot.status != "ready":
        return
    snapshot.captures = "running"
    db.session.commit()
    data = bookmarks.read_snapshot(snapshot)
    data.setdefault("captures", [])
    budget = bookmarks.Budget(snapshot.bookmark.user_id, snapshot.id, already=snapshot.size_bytes)
    targets = bookmarks.capture_targets(data)
    done = failed = 0
    state = "done"
    with (browser_factory or _chromium)() as browser:
        for article_id, url in targets:
            try:
                pdf, shot = capture_page(browser, url)
            except Exception as e:
                logger.info("[Capture] %s: %s", url[:200], e)
                data["captures"].append({"article_id": article_id, "url": url, "pdf": None, "screenshot": None,
                                         "error": str(e)[:200]})
                failed += 1
                continue
            try:
                bookmarks.add_capture(snapshot, data, article_id, url, pdf, shot, budget)
                done += 1
            except bookmarks.OverQuota:
                state = "over_quota"
                break
    if state != "over_quota":
        state = "done" if failed == 0 else ("partial" if done else "failed")
    bookmarks.finish_captures(snapshot, data, state)


class _chromium:
    """A headless Chromium for one job, closed afterwards."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


jobs.register(bookmarks.CAPTURE_JOB, run_capture, bookmarks.fail_capture)


def _heartbeat(app, stop_event):
    while not stop_event.is_set():
        try:
            with app.app_context():
                bookmarks.beat()
        except Exception:
            logger.exception("[Capture] Heartbeat failed")
        stop_event.wait(HEARTBEAT_SECONDS)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from aggregator import create_app
    app = create_app()
    stop_event = threading.Event()
    threading.Thread(target=_heartbeat, args=(app, stop_event), name="capture-heartbeat", daemon=True).start()
    jobs.worker_loop(app, stop_event, kinds=(bookmarks.CAPTURE_JOB,))


if __name__ == "__main__":
    main()
