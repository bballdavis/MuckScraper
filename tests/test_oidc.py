"""Real signed-token verification, SQL identity bindings and browser sessions.

Only provider HTTP boundaries are mocked. No live identity, key or network is used.
Settings are saved the way Settings › Sign-in saves them, not read from the
environment. Needs the scratch Postgres database named by API_TEST_DATABASE_URL,
like test_api (its tables are dropped and recreated); run with pytest.
"""
import os
import re
import socket
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from aggregator import create_app, db, oidc, oidc_settings
from aggregator.models import AppSetting, ConfigSetting, OIDCIdentity, User
from aggregator.oidc import identity_key


TEST_DB = os.environ.get("API_TEST_DATABASE_URL", "")
ENV = {
    "SECRET_KEY": "fixture-only-oidc-session-secret",
    "DATABASE_URL": TEST_DB,
    "OIDC_ISSUER_URL": "https://issuer.example/tenant",
}
# Saved the way Settings › Sign-in saves them (aggregator/oidc_settings.py).
SETTINGS = {
    "enabled": True,
    "issuer_url": ENV["OIDC_ISSUER_URL"],
    "client_id": "fixture-client",
    "redirect_uri": "https://reader.example/auth/oidc/callback",
    "allowed_groups": ["readers"],
    "create_users": True,
}
CLIENT_SECRET = "SECRET_SENTINEL"


def configure(app, secret=None, **changes):
    """Change the saved OIDC settings, as a save on Settings › Sign-in does."""
    with app.app_context():
        values = oidc_settings.load()
        values.update(changes)
        oidc_settings.save(values, "test")
        if secret is not None:
            oidc_settings.save_secret(secret, "test")
        db.session.commit()


def fresh_app(monkeypatch):
    """The web app on an empty copy of the full schema, OIDC off."""
    if not TEST_DB:
        pytest.skip("set API_TEST_DATABASE_URL to run the OIDC tests")
    for name in ("SECRET_KEY", "DATABASE_URL"):
        monkeypatch.setenv(name, ENV[name])
    from news_fetcher import llm_client
    monkeypatch.setattr(llm_client, "EMBEDDING_PROVIDER", "ollama")
    oidc_settings.reset()
    application = create_app()
    application.config.update(TESTING=True, SERVER_NAME="reader.example")
    with application.app_context():
        db.drop_all()
        db.create_all()
    return application


@pytest.fixture
def app(monkeypatch):
    if not TEST_DB:
        pytest.skip("set API_TEST_DATABASE_URL to run the OIDC tests")
    for name in ("SECRET_KEY", "DATABASE_URL"):
        monkeypatch.setenv(name, ENV[name])
    # The embedding identity admission is separate from authentication.
    from news_fetcher import llm_client
    monkeypatch.setattr(llm_client, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("network forbidden")))
    application = create_app()
    application.config.update(TESTING=True, SERVER_NAME="reader.example")
    with application.app_context():
        # These are actual app models, not a mock identity store.
        db.drop_all()
        db.create_all()
    configure(application, secret=CLIENT_SECRET, **SETTINGS)
    # Do not keep an app context across browser requests: Flask's g caches
    # user/CSRF state for that context, unlike separate production requests.
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    oidc_settings.reset()
    socket.socket.connect.assert_not_called()
    socket.getaddrinfo.assert_not_called()


@pytest.fixture
def flow(app, monkeypatch):
    key = RSAKey.generate_key(2048)
    key.ensure_kid()
    with app.app_context():
        remote = oidc.remote(oidc.current_config(fresh=True))
    metadata = {
        "issuer": ENV["OIDC_ISSUER_URL"],
        "authorization_endpoint": "https://issuer.example/authorize",
        "token_endpoint": "https://issuer.example/token",
        "jwks_uri": "https://issuer.example/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    monkeypatch.setattr(remote, "load_server_metadata", Mock(return_value=metadata))
    monkeypatch.setattr(remote, "fetch_jwk_set", Mock(return_value={"keys": [key.as_dict()]}))
    client = app.test_client()

    def start(next="/headlines", browser=client):
        response = browser.get("/auth/oidc/login", query_string={"next": next}, base_url="https://reader.example")
        assert response.status_code == 302
        return parse_qs(urlsplit(response.location).query)

    def finish(params, browser=client, raw_token=None, **overrides):
        claims = {
            "iss": ENV["OIDC_ISSUER_URL"], "sub": "fixture-subject", "aud": "fixture-client",
            "iat": int(time.time()), "exp": int(time.time()) + 120, "nonce": params["nonce"][0],
            "email": "reader@example.com", "email_verified": True, "groups": ["readers"],
            **overrides,
        }
        token = raw_token if raw_token is not None else jwt.encode(
            {"alg": "RS256", "kid": key.as_dict()["kid"]}, claims, key)
        exchange = Mock(return_value={"id_token": token})
        monkeypatch.setattr(remote, "fetch_access_token", exchange)
        response = browser.get("/auth/oidc/callback", query_string={
            "state": params["state"][0], "code": "fixture-code"}, base_url="https://reader.example")
        return response, exchange

    return client, start, finish


def test_real_pkce_signed_token_identity_and_session(app, flow):
    client, start, finish = flow
    params = start("/auth/profile")
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    response, exchange = finish(params)
    assert response.status_code == 302 and response.location == "/auth/profile"
    assert exchange.call_args.kwargs["code_verifier"]
    with app.app_context():
        binding = OIDCIdentity.query.one()
        assert binding.issuer == ENV["OIDC_ISSUER_URL"]
        assert binding.subject == "fixture-subject"
        assert binding.user.role == "reader" and not binding.user.is_admin
        assert not binding.user.check_password("anything")
        user_id = binding.user_id
    with client.session_transaction() as session:
        assert session["_user_id"] == str(user_id)
        assert session["auth_method"] == "oidc"
        assert 3590 <= session["oidc_expires_at"] - time.time() <= 3600
        assert not any("token" in key for key in session)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["REMEMBER_COOKIE_SECURE"] is True
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"


def test_auto_login_is_opt_in_and_manual_page_remains_available(app, flow):
    client, _, _ = flow
    with app.app_context():
        assert oidc_settings.load()["auto_login"] is False
    assert client.get("/auth/login", base_url="https://reader.example").status_code == 200
    configure(app, auto_login=True)
    response = client.get("/auth/login?next=/auth/profile", base_url="https://reader.example")
    assert response.location == "/auth/oidc/login?next=/auth/profile"
    assert response.headers["Cache-Control"] == "no-store"
    configure(app, allow_password_login=False)
    response = client.get("/auth/login?manual=1", base_url="https://reader.example")
    assert response.status_code == 200
    assert b'name="password"' not in response.data


@pytest.mark.parametrize("next_path, expected", [
    ("/auth/profile", "/auth/profile"), ("https://outside.example", "/"),
])
def test_auto_login_uses_verified_flow_and_safe_return(app, flow, next_path, expected):
    client, _, finish = flow
    configure(app, auto_login=True)
    response = client.get("/auth/login", query_string={"next": next_path}, base_url="https://reader.example")
    provider = client.get(response.location, base_url="https://reader.example")
    params = parse_qs(urlsplit(provider.location).query)
    assert params["code_challenge_method"] == ["S256"]
    assert finish(params)[0].location == expected
    with client.session_transaction() as session:
        assert "oidc_auto_login_paused" not in session


def test_failed_auto_login_does_not_loop(app, flow):
    client, start, finish = flow
    configure(app, auto_login=True)
    assert finish(start(), email_verified=False)[0].status_code == 403
    assert client.get("/auth/login", base_url="https://reader.example").status_code == 200
    # The normal sign-in button can retry after a failure.
    assert finish(start())[0].status_code == 302


def test_auto_login_logout_stays_signed_out(app, flow):
    client, start, finish = flow
    configure(app, auto_login=True)
    finish(start())
    response = client.get("/auth/profile", base_url="https://reader.example")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
    response = client.post("/auth/logout", data={"csrf_token": csrf},
                           headers={"Referer": "https://reader.example/auth/profile"},
                           base_url="https://reader.example")
    assert response.status_code == 302
    assert client.get("/auth/login", base_url="https://reader.example").status_code == 200
    with client.session_transaction() as session:
        assert "_user_id" not in session


def test_verified_display_name_is_separate_from_identity_and_escaped(app, flow):
    client, start, finish = flow
    assert finish(start(), preferred_username="<b>Reader</b>")[0].status_code == 302
    with app.app_context():
        original_id = User.query.one().id
        assert User.query.one().username.startswith("oidc_")
    profile = client.get("/auth/profile", base_url="https://reader.example")
    assert b"&lt;b&gt;Reader&lt;/b&gt;" in profile.data
    assert b"<b>Reader</b>" not in profile.data
    assert finish(start(), preferred_username="Updated name")[0].status_code == 302
    with app.app_context():
        assert User.query.one().id == original_id
    with client.session_transaction() as session:
        assert session["oidc_display_name"] == "Updated name"


@pytest.mark.parametrize("claims, expected", [
    ({"preferred_username": " Alice ", "name": "Other"}, "Alice"),
    ({"preferred_username": None, "name": "Alice Smith"}, "Alice Smith"),
    ({"preferred_username": "x" * 300, "email": "reader@example.com"}, "reader@example.com"),
    ({"preferred_username": "bad\nname"}, "Account"),
])
def test_display_name_fallbacks(claims, expected):
    from aggregator.oidc import display_name
    assert display_name(claims) == expected


@pytest.mark.parametrize("overrides", [
    {"iss": "https://attacker.example"}, {"aud": "wrong-client"},
    {"aud": ["fixture-client", "other-client"], "azp": "other-client"},
    {"exp": 1}, {"nonce": "wrong"}, {"nonce": None, "nonce_supported": False},
    {"nonce": "wrong", "nonce_supported": False}, {"email_verified": False},
    {"groups": "readers"}, {"groups": ["outsiders"]}, {"sub": ""},
])
def test_invalid_token_or_access_claims_cannot_create_users(app, flow, overrides):
    _, start, finish = flow
    assert finish(start(), **overrides)[0].status_code == 403
    with app.app_context():
        assert User.query.count() == 0 and OIDCIdentity.query.count() == 0


def test_unsigned_serialization_refused(app, flow):
    _, start, finish = flow
    assert finish(start(), raw_token='{"payload":"e30","signatures":[]}')[0].status_code == 403
    with app.app_context():
        assert not User.query.first()


def test_invalid_signature_is_refused_by_real_verifier(app, flow):
    _, start, finish = flow
    params = start()
    other_key = RSAKey.generate_key(2048)
    claims = {
        "iss": ENV["OIDC_ISSUER_URL"], "sub": "fixture-subject", "aud": "fixture-client",
        "iat": int(time.time()), "exp": int(time.time()) + 120, "nonce": params["nonce"][0],
        "email": "reader@example.com", "email_verified": True, "groups": ["readers"],
    }
    token = jwt.encode({"alg": "RS256"}, claims, other_key)
    assert finish(params, raw_token=token)[0].status_code == 403
    with app.app_context():
        assert not User.query.first()


def test_state_is_browser_bound_and_single_use(app, flow):
    _, start, finish = flow
    params = start()
    response, exchange = finish(params, browser=app.test_client())
    assert response.status_code == 403
    exchange.assert_not_called()
    assert finish(params)[0].status_code == 302
    response, exchange = finish(params)
    assert response.status_code == 403
    exchange.assert_not_called()


@pytest.mark.parametrize("target", ["https://attacker.example", "//attacker.example", "/\\attacker.example", "javascript:alert(1)", "/\n/attacker.example"])
def test_only_local_return_paths_are_allowed(app, flow, target):
    _, start, finish = flow
    assert finish(start(target))[0].location == "/"


def test_email_collision_is_not_account_linking(app, flow):
    with app.app_context():
        local = User(username="local", email="reader@example.com", role="reader", is_active=True)
        local.set_password("keep-local-password")
        db.session.add(local)
        db.session.commit()
    _, start, finish = flow
    assert finish(start())[0].status_code == 403
    with app.app_context():
        assert User.query.count() == 1 and not OIDCIdentity.query.first()
        assert User.query.one().check_password("keep-local-password")


def test_automatic_account_creation_requires_explicit_opt_in(app, flow):
    configure(app, create_users=False)
    _, start, finish = flow
    assert finish(start())[0].status_code == 403
    with app.app_context():
        assert User.query.count() == 0 and OIDCIdentity.query.count() == 0


def test_unmapped_existing_roles_remain_local_and_data_owner_is_preserved(app, flow):
    with app.app_context():
        local = User(username="existing", email="reader@example.com", role="scraper", is_active=True)
        db.session.add(local)
        db.session.flush()
        local_id = local.id
        db.session.add(OIDCIdentity(user=local, identity_key=identity_key(ENV["OIDC_ISSUER_URL"], "fixture-subject"),
                                    issuer=ENV["OIDC_ISSUER_URL"], subject="fixture-subject"))
        db.session.commit()
    _, start, finish = flow
    assert finish(start(), groups=["readers", "admins"])[0].status_code == 302
    with app.app_context():
        assert User.query.one().id == local_id
        assert User.query.one().role == "scraper" and not User.query.one().is_admin


def test_link_command_is_explicit_dry_run_idempotent_and_non_replacing(app, flow):
    with app.app_context():
        local = User(username="existing", email="reader@example.com", role="reader", is_active=True)
        local.set_password("keep-local-password")
        other = User(username="other", email="other@example.com", role="reader", is_active=True)
        db.session.add_all([local, other])
        db.session.commit()
        local_id = local.id
    runner = app.test_cli_runner()
    args = ["link-oidc-identity", "--username", "existing", "--subject", "fixture-subject"]
    assert runner.invoke(args=args).exit_code == 0
    with app.app_context():
        assert not OIDCIdentity.query.first()
    assert runner.invoke(args=args + ["--apply"]).exit_code == 0
    assert runner.invoke(args=args + ["--apply"]).exit_code == 0
    with app.app_context():
        assert OIDCIdentity.query.count() == 1
    assert runner.invoke(args=["link-oidc-identity", "--username", "other", "--subject", "fixture-subject", "--apply"]).exit_code != 0
    _, start, finish = flow
    assert finish(start())[0].status_code == 302
    with app.app_context():
        assert OIDCIdentity.query.one().user_id == local_id
        assert User.query.filter_by(id=local_id).one().check_password("keep-local-password")


def test_disabled_account_not_reactivated(app, flow):
    _, start, finish = flow
    finish(start())
    with app.app_context():
        user = User.query.one()
        user.is_active = False
        db.session.commit()
    assert finish(start())[0].status_code == 403
    with app.app_context():
        assert User.query.one().is_active is False


def test_role_mapping_is_opt_in_and_uses_existing_permission_model(app, flow):
    client, start, finish = flow
    configure(app, role_mapping={"operators": "scraper", "admins": "admin"})
    finish(start(), groups=["readers", "operators"])
    with app.app_context():
        user = User.query.one()
        assert user.has_role("scraper") and not user.has_role("admin")
    # The public read role must not turn into scraper/admin access.
    assert client.get("/admin/users", base_url="https://reader.example").status_code == 403
    finish(start(), groups=["readers", "admins"])
    with app.app_context():
        assert User.query.one().is_admin is True
    # Removing the final administrator is refused, not silently granted forever.
    assert finish(start(), groups=["readers"])[0].status_code == 403
    with app.app_context():
        assert User.query.one().role == "admin"


def test_password_forms_and_profile_do_not_bypass_oidc_only(app, flow):
    client, start, finish = flow
    configure(app, allow_password_login=False)
    response = client.get("/auth/login", base_url="https://reader.example")
    assert b"OpenID Connect" in response.data and b'name="password"' not in response.data
    assert b"SECRET_SENTINEL" not in response.data
    # Isolate the password admission check from the independent CSRF check.
    app.config["WTF_CSRF_ENABLED"] = False
    assert client.post("/auth/login", data={"username": "anything", "password": "anything"}, base_url="https://reader.example").status_code == 403
    app.config["WTF_CSRF_ENABLED"] = True
    finish(start())
    response = client.get("/auth/profile", base_url="https://reader.example")
    assert response.status_code == 200 and b"identity provider" in response.data
    assert b'name="current_password"' not in response.data
    # Real CSRF is still required for writes, including logout.
    assert client.get("/auth/logout", base_url="https://reader.example").status_code == 405
    assert client.post("/auth/logout", base_url="https://reader.example").status_code == 400
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
    # A real HTTPS form supplies a same-origin Referer as well as its token.
    headers = {"Referer": "https://reader.example/auth/profile"}
    assert client.post("/auth/profile", data={"csrf_token": csrf}, headers=headers, base_url="https://reader.example").status_code == 403
    assert client.post("/auth/logout", data={"csrf_token": csrf}, headers=headers, base_url="https://reader.example").status_code == 302
    with client.session_transaction() as session:
        assert "_user_id" not in session


def test_oidc_expiry_is_enforced_server_side(app, flow, monkeypatch):
    client, start, finish = flow
    finish(start())
    with client.session_transaction() as session:
        session["oidc_expires_at"] = 1
    response = client.get("/auth/profile", base_url="https://reader.example")
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert "_user_id" not in session


def test_provider_errors_are_redacted(app, flow, monkeypatch, caplog):
    client, start, _ = flow
    params = start()
    exchange = Mock(side_effect=RuntimeError("SECRET_SENTINEL"))
    monkeypatch.setattr(app.extensions["muckscraper_oidc"], "authorize_access_token", exchange)
    response = client.get("/auth/oidc/callback", query_string={"state": params["state"][0], "code": "fixture-code"}, base_url="https://reader.example")
    assert response.status_code == 403
    exchange.assert_called_once()
    assert "SECRET_SENTINEL" not in caplog.text + response.get_data(as_text=True)


def test_disabled_default_does_not_require_identity_table(monkeypatch):
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", ENV["SECRET_KEY"])
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    from news_fetcher import llm_client
    monkeypatch.setattr(llm_client, "EMBEDDING_PROVIDER", "ollama")
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="reader.example")
    with app.app_context():
        User.__table__.create(db.engine)
        local = User(username="local", email="local@example.com", role="reader", is_active=True)
        local.set_password("local-password")
        db.session.add(local)
        db.session.commit()
    client = app.test_client()
    html = client.get("/auth/login").get_data(as_text=True)
    assert 'name="password"' in html and "OpenID Connect" not in html
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert client.post("/auth/login", data={"username": "local", "password": "local-password", "csrf_token": token}).status_code == 302
    assert client.get("/auth/oidc/login").status_code == 404
    assert client.get("/auth/oidc/callback").status_code == 404
    # Password login shares the same local-return-path policy as OIDC.
    client.get("/auth/logout")
    html = client.get("/auth/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert client.post("/auth/login?next=javascript:alert(1)", data={
        "username": "local", "password": "local-password", "csrf_token": token}).location == "/"
    with app.app_context():
        db.session.remove()
        User.__table__.drop(db.engine)


def test_oidc_disabled_legacy_remember_me_logout_clears_cookie(monkeypatch):
    monkeypatch.setenv("OIDC_ENABLED", "false")
    monkeypatch.setenv("SECRET_KEY", ENV["SECRET_KEY"])
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    from news_fetcher import llm_client
    monkeypatch.setattr(llm_client, "EMBEDDING_PROVIDER", "ollama")
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="reader.example")
    with app.app_context():
        User.__table__.create(db.engine)
        user = User(username="local", email="local@example.com", role="reader", is_active=True)
        user.set_password("local-password")
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    html = client.get("/auth/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    response = client.post("/auth/login", data={"username": "local", "password": "local-password", "remember": "on", "csrf_token": csrf})
    assert any("remember_token=" in value for value in response.headers.getlist("Set-Cookie"))
    response = client.get("/auth/logout")
    assert any("remember_token=;" in value for value in response.headers.getlist("Set-Cookie"))
    with app.app_context():
        db.session.remove()
        User.__table__.drop(db.engine)


@pytest.mark.parametrize("update, has_secret", [
    ({}, False), ({"issuer_url": "http://issuer.example"}, True),
    ({"redirect_uri": "https://user@reader.example/callback"}, True),
    ({"issuer_url": ""}, True), ({"client_id": ""}, True),
    ({"scopes": "email"}, True), ({"groups_claim": "has space"}, True),
    ({"session_lifetime_seconds": 0}, True), ({"provider_name": ""}, True),
])
def test_settings_errors_refuse_without_values(update, has_secret):
    values = {**oidc_settings.DEFAULTS, **SETTINGS, **update}
    problems = oidc_settings.errors(values, has_secret)
    assert problems
    assert CLIENT_SECRET not in " ".join(problems)


def test_settings_turned_off_save_as_a_draft():
    assert oidc_settings.errors({**oidc_settings.DEFAULTS, "issuer_url": "https://issuer.example"}, False) == []


def test_clean_drops_malformed_saved_values():
    cleaned = oidc_settings.clean({"enabled": "yes", "allowed_groups": "readers", "role_mapping": {"ops": "owner"},
                                   "session_lifetime_seconds": 5, "auto_login": True})
    assert cleaned["enabled"] is False and cleaned["allowed_groups"] == [] and cleaned["role_mapping"] == {}
    assert cleaned["session_lifetime_seconds"] == 3600 and cleaned["auto_login"] is True


def test_repeated_abandoned_logins_keep_one_pending_state(app, flow):
    client, start, finish = flow
    with client.session_transaction() as data:
        data['unrelated_preference'] = 'keep'
        data['_state_other_client_fixture'] = {'data': {'note': 'keep'}, 'exp': time.time() + 3600}
    for _ in range(20):
        params = start('/auth/profile')
        with client.session_transaction() as data:
            assert sum(key.startswith('_state_muckscraper_') for key in data) == 1
            assert len(app.session_interface.get_signing_serializer(app).dumps(dict(data))) < 3800
            assert data['unrelated_preference'] == 'keep'
            assert '_state_other_client_fixture' in data
    assert finish(params)[0].location == '/auth/profile'


def test_old_callback_cannot_consume_a_new_login_attempt(app, flow):
    client, start, finish = flow
    old = start()
    latest = start('/auth/profile')
    response, exchange = finish(old)
    assert response.status_code == 403
    exchange.assert_not_called()
    with client.session_transaction() as data:
        assert data['oidc_pending']['state'] == latest['state'][0]
    assert finish(latest)[0].location == '/auth/profile'


def test_identity_migration_preserves_existing_accounts(app):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with app.app_context():
        user = User(username='existing', email='existing@example.org', role='admin', is_admin=True, is_active=True)
        user.set_password('existing-password')
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        # Postgres: the session's read lock on users would block the DROP.
        db.session.remove()
        OIDCIdentity.__table__.drop(db.engine)
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/a958c3be014a_add_oidc_identities.py'
        spec = importlib.util.spec_from_file_location('identity_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        assert User.query.one().id == user_id
        assert User.query.one().check_password('existing-password')
        assert User.query.one().role == 'admin'
        assert OIDCIdentity.query.count() == 0


def test_login_cleans_legacy_accumulated_state(app, flow):
    client, start, finish = flow
    remote = app.extensions['muckscraper_oidc']
    with client.session_transaction() as data:
        for index in range(8):
            remote.framework.set_state_data(data, 'abandoned-' + str(index), {'nonce': 'old'})
        data['unrelated_preference'] = 'keep'
    params = start()
    with client.session_transaction() as data:
        assert [key for key in data if key.startswith('_state_muckscraper_')] == ['_state_muckscraper_' + params['state'][0]]
        assert data['unrelated_preference'] == 'keep'
    assert finish(params)[0].status_code == 302


@pytest.mark.parametrize('invalid_state', ['', 'wrong-state', '\u2603'])
def test_invalid_callback_state_preserves_the_pending_attempt(app, flow, invalid_state):
    client, start, finish = flow
    params = start('/auth/profile')
    response = client.get('/auth/oidc/callback', query_string={'state': invalid_state, 'code': 'fixture-code'}, base_url='https://reader.example')
    assert response.status_code == 403
    assert finish(params)[0].location == '/auth/profile'


def test_manual_password_page_keeps_same_origin_referrer_for_csrf(app, flow):
    client, start, _ = flow
    response = client.get('/auth/login?manual=1', base_url='https://reader.example')
    assert response.headers['Referrer-Policy'] == 'same-origin'
    assert response.headers['Cache-Control'] == 'no-store'
    start()
    response = client.get('/auth/oidc/callback?state=invalid', base_url='https://reader.example')
    assert response.status_code == 403
    assert response.headers['Referrer-Policy'] == 'no-referrer'


def _admin_browser(app, base_url="https://reader.example"):
    """A browser signed in with a password as an admin; CSRF off for brevity."""
    with app.app_context():
        admin = User(username="boss", email="boss@example.com", role="admin", is_admin=True, is_active=True)
        admin.set_password("boss-password")
        db.session.add(admin)
        db.session.commit()
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    response = client.post("/auth/login", data={"username": "boss", "password": "boss-password"}, base_url=base_url)
    assert response.status_code == 302
    return client


def _form(**changes):
    form = {"enabled": "on", "issuer_url": SETTINGS["issuer_url"], "client_id": "fixture-client",
            "redirect_uri": SETTINGS["redirect_uri"], "provider_name": "Pocket ID",
            "scopes": "openid profile email groups", "groups_claim": "groups",
            "allowed_groups": "readers, editors", "admin_groups": "admins", "scraper_groups": "operators",
            "create_users": "on", "session_minutes": "90"}
    form.update(changes)
    return {k: v for k, v in form.items() if v is not None}


def test_settings_page_saves_secret_encrypted_and_never_shows_it(app, caplog):
    browser = _admin_browser(app)
    new_secret = "NEW-SECRET-SENTINEL-wxyz"
    response = browser.post("/admin/settings/sign-in", data=_form(OIDC_CLIENT_SECRET=new_secret),
                            base_url="https://reader.example")
    assert response.status_code == 302
    with app.app_context():
        row = ConfigSetting.query.get("OIDC_CLIENT_SECRET")
        assert row.hint == "wxyz" and row.value is None and new_secret not in row.secret_value
        values = oidc_settings.load()
        assert values["provider_name"] == "Pocket ID" and values["allowed_groups"] == ["readers", "editors"]
        assert values["role_mapping"] == {"admins": "admin", "operators": "scraper"}
        assert values["session_lifetime_seconds"] == 5400
        assert oidc.current_config(fresh=True)["CLIENT_SECRET"] == new_secret
        assert new_secret not in AppSetting.query.get(oidc_settings.KEY).value
    page = browser.get("/admin/settings/sign-in", base_url="https://reader.example").get_data(as_text=True)
    assert "Saved key ending wxyz" in page and new_secret not in page and CLIENT_SECRET not in page
    assert "https://reader.example/auth/oidc/callback" in page
    # A blank secret field keeps the saved one.
    browser.post("/admin/settings/sign-in", data=_form(), base_url="https://reader.example")
    with app.app_context():
        assert oidc.current_config(fresh=True)["CLIENT_SECRET"] == new_secret
    assert new_secret not in caplog.text and CLIENT_SECRET not in caplog.text
    browser.post("/auth/logout", base_url="https://reader.example")
    html = app.test_client().get("/auth/login", base_url="https://reader.example").get_data(as_text=True)
    assert "Sign in with Pocket ID" in html and new_secret not in html


def test_settings_refuse_turning_on_over_plain_http(app):
    configure(app, enabled=False)
    browser = _admin_browser(app, base_url="http://reader.example")
    browser.post("/admin/settings/sign-in", data=_form(), base_url="http://reader.example")
    with app.app_context():
        assert oidc_settings.load()["enabled"] is False
    # Behind a TLS proxy the scheme comes from X-Forwarded-Proto.
    browser.post("/admin/settings/sign-in", data=_form(), base_url="http://reader.example",
                 headers={"X-Forwarded-Proto": "https"})
    with app.app_context():
        assert oidc_settings.load()["enabled"] is True


def test_provider_only_needs_an_admin_signed_in_with_the_provider(app, flow):
    browser = _admin_browser(app)
    browser.post("/admin/settings/sign-in", data=_form(oidc_only="on"), base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["allow_password_login"] is True
    client, start, finish = flow
    configure(app, role_mapping={"admins": "admin"})
    assert finish(start(), groups=["readers", "admins"])[0].status_code == 302
    app.config["WTF_CSRF_ENABLED"] = False
    client.post("/admin/settings/sign-in", data=_form(oidc_only="on"), base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["allow_password_login"] is False


def test_disable_oidc_command_restores_password_sign_in(app):
    configure(app, allow_password_login=False)
    result = app.test_cli_runner().invoke(args=["disable-oidc"])
    assert result.exit_code == 0
    with app.app_context():
        assert oidc_settings.load()["enabled"] is False
        assert oidc.current_config(fresh=True) is None
    html = app.test_client().get("/auth/login", base_url="https://reader.example").get_data(as_text=True)
    assert 'name="password"' in html


def test_settings_page_links_and_unlinks_accounts(app, flow):
    browser = _admin_browser(app)
    with app.app_context():
        reader = User(username="existing", email="reader@example.com", role="reader", is_active=True)
        db.session.add(reader)
        db.session.commit()
        reader_id = reader.id
    browser.post("/admin/settings/sign-in/link", data={"user_id": reader_id, "subject": "fixture-subject"},
                 base_url="https://reader.example")
    with app.app_context():
        binding = OIDCIdentity.query.one()
        assert binding.user_id == reader_id and binding.subject == "fixture-subject"
        key = binding.identity_key
    page = browser.get("/admin/settings/sign-in", base_url="https://reader.example").get_data(as_text=True)
    assert "fixture-subject" in page and "existing" in page
    _, start, finish = flow
    assert finish(start())[0].status_code == 302
    with app.app_context():
        assert User.query.filter_by(email="reader@example.com").one().id == reader_id
    browser.post(f"/admin/settings/sign-in/unlink/{key}", base_url="https://reader.example")
    with app.app_context():
        assert OIDCIdentity.query.count() == 0


def test_settings_page_is_admin_only(app, flow):
    client, start, finish = flow
    finish(start())
    assert client.get("/admin/settings/sign-in", base_url="https://reader.example").status_code == 403


def test_default_role_sets_new_accounts_and_unmapped_groups(app, flow):
    _, start, finish = flow
    configure(app, default_role="scraper")
    assert finish(start())[0].status_code == 302
    with app.app_context():
        user = User.query.one()
        assert user.role == "scraper" and not user.is_admin
    # With groups mapped, anyone outside them gets the default role too.
    configure(app, default_role="reader", role_mapping={"admins": "admin"})
    finish(start(), groups=["readers"])
    with app.app_context():
        assert User.query.one().role == "reader"


def test_default_role_is_never_admin():
    assert oidc_settings.clean({"default_role": "admin"})["default_role"] == "reader"
    assert oidc_settings.clean({"default_role": "scraper"})["default_role"] == "scraper"
    values = {**oidc_settings.DEFAULTS, "default_role": "admin"}
    assert oidc_settings.errors(values, False)


def test_settings_page_saves_default_role_and_users_page_preselects_it(app):
    browser = _admin_browser(app)
    browser.post("/admin/settings/sign-in", data=_form(default_role="admin"), base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["default_role"] == "reader"
    browser.post("/admin/settings/sign-in", data=_form(default_role="scraper"), base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["default_role"] == "scraper"
    page = browser.get("/admin/settings/sign-in", base_url="https://reader.example").get_data(as_text=True)
    assert '<option value="scraper" selected>' in page and '<option value="admin"' not in page
    users = browser.get("/admin/users", base_url="https://reader.example").get_data(as_text=True)
    assert '<option value="scraper" selected>Scraper' in users


def test_matching_claim_links_an_existing_account_once(app, flow):
    with app.app_context():
        admin = User(username="boss", email="reader@example.com", role="admin", is_admin=True, is_active=True)
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    _, start, finish = flow
    # Off by default: the matching email is refused, never linked.
    assert finish(start())[0].status_code == 403
    configure(app, match_claim="email")
    assert finish(start())[0].status_code == 302
    with app.app_context():
        assert User.query.count() == 1
        assert OIDCIdentity.query.one().user_id == admin_id and User.query.one().is_admin
    # A second identity with the same email can't take the linked account.
    assert finish(start(), sub="other-subject")[0].status_code == 403


def test_matching_claim_by_custom_claim_and_username(app, flow):
    with app.app_context():
        db.session.add(User(username="reader", email="reader@example.com", role="scraper", is_active=True))
        db.session.commit()
    configure(app, match_claim="nickname")
    _, start, finish = flow
    assert finish(start(), nickname="PHILIP", email="p@example.com")[0].status_code == 302
    with app.app_context():
        assert OIDCIdentity.query.one().user.username == "reader"


def test_matching_claim_refuses_ambiguous_or_disabled_accounts(app, flow):
    with app.app_context():
        db.session.add_all([User(username="reader@example.com", email="x@example.com", is_active=True),
                            User(username="someone", email="reader@example.com", is_active=True)])
        db.session.commit()
    configure(app, match_claim="email")
    _, start, finish = flow
    assert finish(start())[0].status_code == 403
    with app.app_context():
        assert OIDCIdentity.query.count() == 0


def test_settings_page_saves_custom_matching_claim(app):
    browser = _admin_browser(app)
    browser.post("/admin/settings/sign-in", data=_form(match_claim="custom", match_claim_custom="upn"),
                 base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["match_claim"] == "upn"
    page = browser.get("/admin/settings/sign-in", base_url="https://reader.example").get_data(as_text=True)
    assert '<option value="custom" selected>' in page and 'value="upn"' in page
    browser.post("/admin/settings/sign-in", data=_form(match_claim="email"), base_url="https://reader.example")
    with app.app_context():
        assert oidc_settings.load()["match_claim"] == "email"
