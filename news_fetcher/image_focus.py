# news_fetcher/image_focus.py
"""Where a photo's subject is, so crops keep it in frame.

Cards, the article page and the iOS app crop every photo to their own shape,
and a center crop of a tall photo of a person cuts off their head. Each
article image gets a focal point here: `articles.image_focus_x` / `_y`, 0..1
from the left and top. /api/v1 sends it as `image_focus`; the web turns it
into CSS object-position and the iOS app crops around it. Unknown means the
center, which is what every client did before.

No model: the photo is shrunk to about 96px and scored with two cheap maps.
- Spectral residual saliency (Hou & Zhang, 2007): what stands out from the
  rest of the picture, from one FFT of the gray image.
- Skin tone in YCbCr, since most news photos are of people and a face is
  what a crop should keep. Weighted toward the upper middle, where faces
  sit in a news photo; hands and arms lower down count for less.
The focus is the peak of the blurred sum. A flat map (a landscape, a
document) keeps the center.
"""

import io
import logging
from datetime import datetime, timedelta

import numpy as np
import requests

logger = logging.getLogger(__name__)

RUN_MINUTES = 10
FOCUS_DAYS = 3
FOCUS_PER_RUN = 60
TIMEOUT = 8
MAX_BYTES = 8 * 1024 * 1024
SIZE = 96
# The peak must stand this far above the map's mean, or the photo keeps the
# center.
MIN_CONTRAST = 1.6


def _blur(a, radius):
    """Box blur, three passes (close to a Gaussian), edges clamped."""
    if radius < 1:
        return a
    for axis in (0, 1):
        for _ in range(3):
            pad = [(0, 0), (0, 0)]
            pad[axis] = (radius, radius)
            p = np.pad(a, pad, mode="edge")
            c = np.cumsum(p, axis=axis, dtype=np.float64)
            c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
            n = a.shape[axis]
            hi = np.take(c, np.arange(2 * radius + 1, 2 * radius + 1 + n), axis=axis)
            lo = np.take(c, np.arange(0, n), axis=axis)
            a = (hi - lo) / (2 * radius + 1)
    return a


def _norm(a):
    lo, hi = float(a.min()), float(a.max())
    return (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)


def _saliency(gray):
    spectrum = np.fft.fft2(gray)
    log_amp = np.log(np.abs(spectrum) + 1e-6)
    residual = log_amp - _blur(log_amp, 1)
    sal = np.abs(np.fft.ifft2(np.exp(residual + 1j * np.angle(spectrum)))) ** 2
    return _norm(_blur(sal, max(1, gray.shape[1] // 24)))


def _skin(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    mask = (cr > 136) & (cr < 173) & (cb > 77) & (cb < 127) & (y > 60) & (r > g) & (r > b)
    return mask.astype(np.float64)


def focus_of_pixels(rgb):
    """(x, y) in 0..1 for an HxWx3 array, or None when nothing stands out."""
    rgb = np.asarray(rgb, dtype=np.float64)
    h, w = rgb.shape[:2]
    if h < 8 or w < 8:
        return None
    gray = rgb.mean(axis=2)
    if gray.std() < 4:
        return None
    sal = _saliency(gray)
    skin = _skin(rgb)
    # A few stray pixels are noise, not a person.
    if skin.mean() < 0.004:
        skin = np.zeros_like(skin)
    skin = _norm(_blur(skin, max(1, w // 32)))

    ys = (np.arange(h) + 0.5)[:, None] / h
    xs = (np.arange(w) + 0.5)[None, :] / w
    upright = np.exp(-((ys - 0.35) ** 2) / (2 * 0.3 ** 2))
    centered = np.exp(-((xs - 0.5) ** 2) / (2 * 0.4 ** 2)) * np.exp(-((ys - 0.45) ** 2) / (2 * 0.45 ** 2))
    score = (sal + 1.3 * skin * upright) * centered
    score = _blur(score, max(1, min(h, w) // 10))

    mean = float(score.mean())
    if mean <= 0 or float(score.max()) / mean < MIN_CONTRAST:
        return None
    # The weighted center of the peak's neighborhood, not one pixel.
    top = score >= float(score.max()) * 0.85
    weights = np.where(top, score, 0.0)
    total = float(weights.sum())
    yy, xx = np.mgrid[0:h, 0:w]
    x = float((weights * (xx + 0.5)).sum() / total / w)
    y = float((weights * (yy + 0.5)).sum() / total / h)
    return round(x, 3), round(y, 3)


def focus_of_bytes(data):
    """(x, y) for an encoded image, or None when it can't be read."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            # JPEG decodes straight to a fraction of its size.
            im.draft("RGB", (SIZE * 2, SIZE * 2))
            im = im.convert("RGB")
            im.thumbnail((SIZE, SIZE))
            return focus_of_pixels(np.asarray(im))
    except Exception as exc:
        logger.info("  [Focus] Unreadable image: %s", exc)
        return None


def _download(url, session):
    from news_fetcher.scraper import HEADERS_DEFAULT
    response = session.get(url, headers=HEADERS_DEFAULT, timeout=TIMEOUT, stream=True, allow_redirects=True)
    try:
        kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if response.status_code != 200 or (kind and not kind.startswith("image/")) or "svg" in kind:
            return None
        chunks, size = [], 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def articles_needing_focus(now=None, limit=FOCUS_PER_RUN):
    from aggregator.models import Article
    now = now or datetime.utcnow()
    return (
        Article.query
        .filter(
            Article.image_url.isnot(None),
            Article.image_url != "",
            Article.image_focus_at.is_(None),
            Article.date >= now - timedelta(days=FOCUS_DAYS),
        )
        .order_by(Article.date.desc())
        .limit(limit)
        .all()
    )


def refresh_image_focus(now=None, session=None, download=None):
    """Find the focal point of each recent article photo, once per photo.
    Articles sharing a photo (wire pictures) share the one download."""
    from aggregator import db
    now = now or datetime.utcnow()
    session = session or requests.Session()
    download = download or (lambda url: _download(url, session))
    stats = {"checked": 0, "found": 0}
    done = {}
    for article in articles_needing_focus(now):
        article.image_focus_at = now
        url = article.image_url
        if not url.startswith(("http://", "https://")):
            continue
        if url not in done:
            stats["checked"] += 1
            try:
                data = download(url)
            except requests.RequestException as exc:
                logger.info("  [Focus] Download failed for %s: %s", url[:80], exc)
                data = None
            done[url] = focus_of_bytes(data) if data else None
            if done[url]:
                stats["found"] += 1
        focus = done[url]
        article.image_focus_x, article.image_focus_y = focus if focus else (None, None)
    db.session.commit()
    logger.info("  [Focus] %s photos checked, %s focal points found", stats["checked"], stats["found"])
    return stats

