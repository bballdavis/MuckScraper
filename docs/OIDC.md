# Optional OpenID Connect login

OIDC adds identity-provider sign-in to the existing MuckScraper accounts and
Reader, Scraper and Admin roles. It does not change the news pipeline.
It is disabled by default. Existing password login remains available unless
explicitly disabled.

## Configure a provider

Register a confidential web client with your provider. Enable authorization
code flow and PKCE S256. Register this exact callback, using your own origin:

```text
https://reader.example.com/auth/oidc/callback
```

Then fill in **Settings › Sign-in** (`/admin/settings/sign-in`) as an admin.
Nothing goes in `.env`: the settings live in the database, and the client
secret is encrypted like the LLM and source API keys (Fernet, with a key from
`CONFIG_ENCRYPTION_KEY`, else derived from `SECRET_KEY`). The page shows only
its last four characters; it never comes back to the browser and is never
logged. A save reaches every web worker within 15 seconds, without a restart.

| Field | What it does |
| --- | --- |
| OIDC sign-in on | Adds "Sign in with …" to the sign-in page |
| Issuer URL | Must match the provider's `iss` exactly |
| Client ID, Client secret | From the client you registered |
| Callback URL | Prefilled with this site's `/auth/oidc/callback` |
| Button label | "Sign in with *label*" |
| Scopes | Space separated; must include `openid` |
| Groups claim | Defaults to `groups` |
| Allowed groups | Comma separated; blank allows any verified identity |
| Admin groups, Scraper groups | Role mapping, see below |
| Create accounts on first sign-in | Off by default |
| Provider only | Hides the password form and refuses password sign-in |
| Go straight to the provider | Auto-login from the sign-in page |
| Session length | 1 minute to 24 hours, default 60 minutes |

Turning sign-in on needs the settings page itself opened over HTTPS (directly
or through a proxy that sends `X-Forwarded-Proto: https`), because it makes
the session and remember-me cookies HTTPS-only. Settings that are saved as on
but incomplete, or a secret that no longer decrypts after a key change, leave
OIDC unavailable and password sign-in working.

Apply database migrations before enabling OIDC. The identity migration only
creates a new table; it does not reset users, change passwords, or copy news data.
The issuer and callback must use HTTPS. The callback is explicit rather than
derived from untrusted forwarded headers. Configure any reverse-proxy trust
separately and register the same external URL at the provider.

The provider must return a signed ID token with a verified email address.
Group restrictions and role mappings use a string array in the signed token;
the claim name defaults to `groups`, configurable as **Groups claim**.
Claims available only from a separate UserInfo endpoint are not used.

## Accounts and linking

Identity is the exact issuer and subject pair. Email and username are not
identity keys, and matching emails never automatically link existing accounts.
Email and username collisions refuse sign-in rather than overwriting an account.
Existing email length and uniqueness constraints remain in force.

Automatic account creation is off. An administrator can explicitly bind an
independently verified provider subject to an existing active account, under
**Linked accounts** on Settings › Sign-in (which also unlinks one) or from the
shell:

```sh
flask --app aggregator.app link-oidc-identity --username existing-reader --subject verified-subject
flask --app aggregator.app link-oidc-identity --username existing-reader --subject verified-subject --apply
```

The first command is a dry-run. The second creates one identity binding.
Repeated application to the same account is harmless; replacing a binding with
another account is refused. The command never looks up a subject by email and
does not change the account's password or role.

Turn on **Create accounts on first sign-in** to allow verified identities to create accounts.
Generated usernames do not use the provider's display name. New accounts have
no usable local password and start as Readers unless explicit role mapping
grants a higher role. Disabled local accounts cannot be reactivated by OIDC.

## Access and roles

Blank **Allowed groups** accepts any otherwise valid identity from the
configured issuer. A list requires membership of at least one named group.
With both **Admin groups** and **Scraper groups** blank, existing roles remain
locally managed (Settings › Users).

A role mapping is authoritative at each successful OIDC sign-in. The
highest matching role wins; an identity with no mapped role becomes a Reader.
This also applies to explicitly linked local accounts. Admin mappings grant
full administrative access, so use narrowly controlled groups. A change that
would demote the final active administrator refuses sign-in and leaves the
account unchanged. Retain and test an administrator recovery route before
disabling local login.

**Provider only** removes the password form and refuses password sign-in
POSTs. It can only be turned on by an admin who signed in with the provider,
so a broken provider setup can't lock every admin out. If the provider later
breaks, `flask --app aggregator.app disable-oidc` on the server turns OIDC off
and password sign-in back on. It does not invalidate sessions issued before the setting changed;
plan a session-key rotation separately if existing local sessions must be revoked.
`PUBLIC_READ_ACCESS` remains an independent, explicit setting.

Turn on **Go straight to the provider** to send signed-out visitors from the login page
directly to the configured provider. The default is false. Public reading pages
remain public when enabled separately. The original local return path is
preserved through the existing OIDC flow.

Use `/auth/login?manual=1` to show the sign-in page without an automatic redirect.
This does not enable password login if it is disabled. After sign-out or a failed
or cancelled provider attempt, automatic login pauses for that browser session.
The provider sign-in button remains available for an explicit retry. A successful
sign-in clears the pause. The option initiates the normal provider login flow;
it does not bypass provider authentication or consent.

## Sessions, profile and logout

Authlib validates the signed ID token, issuer, audience, expiry and nonce.
Authorization requests use browser-bound, single-use state and PKCE. Only one
pending OIDC sign-in is retained per browser session; a new start removes its
old Authlib state before creating another attempt. Invalid or superseded callbacks
are refused without consuming the current attempt.
Return URLs are restricted to local paths. Authentication responses are not
cached. OIDC responses do not send referrers; the password page allows same-origin
referrers so HTTPS form submissions retain CSRF protection. Errors expose only an exception class, never provider
responses, tokens or user attributes.

No access, refresh or ID token is stored in the identity table or logged. The
application session records the identity binding and an absolute expiry. Expiry
is enforced server-side on subsequent requests; it does not slide with activity.
The default is one hour, configurable from 60 seconds to 24 hours. Provider group
changes take effect at sign-in, not immediately in existing sessions.

During an OIDC session, the profile explains that identity details are managed
at the provider and refuses local email/password changes. Sign-out requires a
CSRF-protected POST. It clears the local session and any remember-me cookie,
but does not end the provider's own session. RP-initiated or back-channel logout
is not implemented.

## Verification

```sh
API_TEST_DATABASE_URL=postgresql://... PYTHONPATH=. pytest -q tests/test_oidc.py
```

The tests use the scratch Postgres database the API tests use (its tables are
dropped and recreated) and save settings the way the settings page does.

Authentication tests exercise the real Authlib verifier with signed fixture
tokens, SQL identity bindings, Flask sessions and CSRF. Provider HTTP boundaries
are mocked and outbound socket access is refused. Tests cover both the default
password flow and enabled OIDC, including invalid signatures/claims, state replay,
account collisions, explicit linking, role mapping, disabled users and expiry.

Deployment acceptance must also exercise real provider login and logout in a
fresh browser. On iPhone, test Safari and a separately installed home-screen web
app, including the return from the provider. A manifest or passing unit test is
not evidence that this browser flow has passed. This feature does not create a
native App Store client.
