"""Fetching URLs that came from outside (article images, pages to capture)
without letting them reach the server's own network.

A URL is allowed only when it is http(s), on a normal port, and every
address its host resolves to is public: not loopback, private, link-local
(cloud metadata), multicast or reserved. Redirects are followed by hand so
each hop is checked again, and a download stops at `max_bytes`.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (compatible; MuckScraper time capsule)"
ALLOWED_PORTS = {None, 80, 443}
MAX_REDIRECTS = 4


class BlockedURL(ValueError):
    """The URL points somewhere a server-side fetch must not go."""


def _public(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url):
    """Raise BlockedURL unless `url` is a public http(s) URL. Returns the parsed URL."""
    if not isinstance(url, str) or len(url) > 2000:
        raise BlockedURL("Not a usable URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedURL("Only http(s) URLs are fetched.")
    if parsed.username or parsed.password:
        raise BlockedURL("URLs with credentials are not fetched.")
    try:
        port = parsed.port
    except ValueError:
        raise BlockedURL("Bad port.")
    if port not in ALLOWED_PORTS:
        raise BlockedURL("Only ports 80 and 443 are fetched.")
    host_is_public(parsed.hostname)
    return parsed


def host_is_public(host):
    """Raise BlockedURL unless every address `host` resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise BlockedURL("The host doesn't resolve.")
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_public(a) for a in addresses):
        raise BlockedURL("The host is not on the public internet.")
    return True


def fetch(url, max_bytes, accept_prefix=None, timeout=15):
    """(bytes, content type, final URL) for a public URL, at most `max_bytes`.
    `accept_prefix` (such as "image/") rejects other content types. Raises
    BlockedURL for a refused URL and ValueError for a bad response."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                stream=True, allow_redirects=False)
        try:
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Redirect without a location.")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if accept_prefix and not kind.startswith(accept_prefix):
                raise ValueError(f"Not {accept_prefix.rstrip('/')}: {kind or 'unknown'}")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValueError("Too large.")
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError("Too large.")
            return bytes(body), kind, url
        finally:
            response.close()
    raise ValueError("Too many redirects.")
