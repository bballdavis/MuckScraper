"""The API's own Flask app: only the /api/v1 blueprint, for the `api`
container (docker compose --profile api). It never registers the web
pages, admin or login, so exposing it to the internet exposes nothing else.

    gunicorn -b 0.0.0.0:5050 "aggregator.api_app:create_api_app()"

API_PROXY_HOPS (default 1) is how many reverse proxies or tunnels sit in
front, so ProxyFix takes the client's address from exactly that many
X-Forwarded-For entries and a client can't spoof it with its own header.
Set 0 when nothing is in front.

API_CLIENT_IP_HEADER names a header a CDN sets to the visitor's address,
e.g. CF-Connecting-IP behind Cloudflare. Without it, every visitor arriving
through one Cloudflare edge shares that edge's address, and so one per-IP
rate limit. Set it only when the API is reachable through that CDN alone,
since anyone reaching the origin directly could send the header themselves.
"""
import ipaddress
import logging
import os

from flask import Flask, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from aggregator import db

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
STATEMENT_TIMEOUT_MS = 5000


class ClientIpHeader:
    """WSGI middleware: take REMOTE_ADDR from a trusted CDN header when it
    holds a valid address, otherwise leave it as the proxies set it."""

    def __init__(self, wsgi_app, header):
        self.wsgi_app = wsgi_app
        self.key = "HTTP_" + header.strip().upper().replace("-", "_")

    def __call__(self, environ, start_response):
        value = (environ.get(self.key) or "").strip()
        try:
            environ["REMOTE_ADDR"] = str(ipaddress.ip_address(value))
        except ValueError:
            pass
        return self.wsgi_app(environ, start_response)


def create_api_app():
    from aggregator.api import api
    from aggregator.api.errors import GENERIC, error_response

    app = Flask(__name__, static_folder=None)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"},
        "pool_pre_ping": True,
    }
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.json.sort_keys = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    client_ip_header = os.environ.get("API_CLIENT_IP_HEADER", "").strip()
    if client_ip_header:
        app.wsgi_app = ClientIpHeader(app.wsgi_app, client_ip_header)
    hops = int(os.environ.get("API_PROXY_HOPS", "1") or 0)
    if hops > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=0, x_port=0, x_prefix=0)

    db.init_app(app)
    app.register_blueprint(api, url_prefix="/api/v1")

    @app.errorhandler(HTTPException)
    def _http_error(e):
        code, message = GENERIC.get(e.code, ("error", e.name))
        return error_response(e.code, code, message)

    @app.errorhandler(Exception)
    def _server_error(e):
        logger.exception("[api] Unhandled error on %s %s", request.method, request.path)
        db.session.rollback()
        code, message = GENERIC[500]
        return error_response(500, code, message)

    @app.after_request
    def _headers(response):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, no-cache"
        if request.method == "GET" and response.status_code == 200 and response.is_json:
            response.add_etag()
            response.make_conditional(request)
        return response

    return app

