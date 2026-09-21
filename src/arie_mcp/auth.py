"""A minimal, spec-compliant OAuth 2.1 authorization server for the remote
(Streamable HTTP) MCP transport only -- the stdio server (``arie_mcp.server``)
never imports this module and has no auth of any kind, unchanged.

Not a hand-rolled protocol. This implements the SDK's own
:class:`~mcp.server.auth.provider.OAuthAuthorizationServerProvider` Protocol
-- PKCE verification, the ``/authorize``/``/token``/``/register``/``/revoke``
routes, and OAuth/Protected-Resource discovery metadata are all the SDK's,
wired up exactly as ``mcp.server.mcpserver.MCPServer(auth_server_provider=...)``
expects (the same "legacy combined AS+RS" shape the SDK's own
``examples/servers/simple-auth/mcp_simple_auth/legacy_as_server.py`` uses --
one server acting as both, appropriate for a single-operator tool with no
multi-tenant client population to justify splitting them). This module owns
only what the Protocol leaves to the implementor: who is allowed to complete
the ``/authorize`` step, and where issued codes/tokens are stored.

**Single operator, one password, no username.** There is exactly one
legitimate caller of this server: the person who owns it. `MCP_OWNER_PASSWORD`
(no default -- see ``settings.py``) gates the login page the authorization
flow redirects to; compared with :func:`secrets.compare_digest` so a wrong
guess can't be timed. This is the standard OAuth extension point (every real
authorization server authenticates its resource owner somehow) filled with
the simplest mechanism that actually fits a population of one, not a
different protocol.

**In-memory, deliberately.** Registered clients, authorization codes, and
tokens all live in a process-local dict -- correct for one replica (this
service never scales horizontally; it is a diagnostic tool, not product
traffic) and simplest to audit. A Railway redeploy invalidates every session,
which just means Claude re-runs Dynamic Client Registration and the owner
re-authenticates once -- an inconvenience, not a correctness gap, and no
worse than any other public OAuth client (DCR) is already expected to
tolerate.

**Refresh tokens, rotated.** The SDK's own bundled example leaves these
unimplemented ("not supported in this example"). Claude reactively refreshes
on a 401 and proactively refreshes up to five minutes before expiry, so
skipping this would force the owner to redo the full consent flow roughly
hourly. Each refresh both rotates the refresh token (required for public
clients per OAuth 2.1 token-theft mitigation, which is what Dynamic Client
Registration always yields) and returns the new one in the same response
that invalidates the old one, per Claude's own connector documentation.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

_LOGGER = logging.getLogger("arie_mcp.auth")

_AUTH_CODE_TTL_SECONDS = 300
_ACCESS_TOKEN_TTL_SECONDS = 3600
_REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 3600

# Login rate limiting: a fixed small budget per source IP, sliding one-hour
# window. Generous enough that a legitimate owner mistyping a password twice
# never gets locked out, tight enough that a stranger who finds this server's
# URL can't meaningfully brute-force a 32+ character password before this
# throttles them to noise. Not a substitute for a strong password -- a floor
# under one.
_LOGIN_ATTEMPT_LIMIT = 10
_LOGIN_ATTEMPT_WINDOW_SECONDS = 3600


@dataclass
class _RefreshTokenRecord:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: float
    resource: str | None
    subject: str | None


@dataclass
class _PendingAuthorization:
    """One in-flight ``/authorize`` request, keyed by its ``state`` value,
    between ``authorize()`` handing the visitor to ``/login`` and
    ``handle_login_callback`` completing it. A plain dataclass rather than
    a loosely-typed dict so the fields the login callback reads back are
    checked, not just hoped for."""

    redirect_uri: str
    code_challenge: str
    redirect_uri_provided_explicitly: bool
    client_id: str
    resource: str | None
    scopes: list[str]
    created_at: float


class RateLimitedError(Exception):
    """Raised by :meth:`ArieOAuthProvider.check_login_rate_limit`; caught by
    the ``/login`` route handlers and turned into a 429."""


class ArieOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, *, owner_password: str, login_url: str) -> None:
        if not owner_password:
            raise ValueError(
                "owner_password must be set — refusing to start an unauthenticated login gate"
            )
        self._owner_password = owner_password
        self._login_url = login_url

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, _PendingAuthorization] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, _RefreshTokenRecord] = {}
        self._login_attempts: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------ clients --

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("no client_id provided")
        self.clients[client_info.client_id] = client_info
        _LOGGER.info(
            "DCR: registered client_id=%s redirect_uris=%s",
            client_info.client_id,
            client_info.redirect_uris,
        )

    # --------------------------------------------------------- /authorize --

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        state = params.state or secrets.token_hex(16)
        self._pending[state] = _PendingAuthorization(
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            client_id=client.client_id,
            resource=params.resource,
            scopes=params.scopes or [],
            created_at=time.time(),
        )
        return f"{self._login_url}?state={state}"

    # ------------------------------------------------------- login gate --

    def check_login_rate_limit(self, source_ip: str) -> None:
        now = time.time()
        attempts = self._login_attempts[source_ip]
        attempts[:] = [t for t in attempts if now - t < _LOGIN_ATTEMPT_WINDOW_SECONDS]
        if len(attempts) >= _LOGIN_ATTEMPT_LIMIT:
            raise RateLimitedError(source_ip)
        attempts.append(now)

    def render_login_page(self, state: str, *, error: bool = False) -> HTMLResponse:
        """The only UI this server has. One field, no username -- there is
        exactly one legitimate owner and nothing here identifies *which*
        person is signing in, only *that* they hold the shared secret."""
        error_html = (
            '<p style="color:#b91c1c;margin:0 0 16px">Incorrect password.</p>' if error else ""
        )
        html = f"""<!DOCTYPE html>
<html>
<head>
<title>ARIE MCP — Sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #0b0f14; color: #e5e7eb; display: flex; align-items: center;
          justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #131a22; border: 1px solid #232c37; border-radius: 12px;
           padding: 32px; width: 100%; max-width: 360px; }}
  h1 {{ font-size: 1.05rem; margin: 0 0 4px; }}
  p.sub {{ color: #9aa4b2; font-size: 0.85rem; margin: 0 0 20px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
           border: 1px solid #2b3542; background: #0e141b; color: #e5e7eb; font-size: 0.95rem; }}
  button {{ margin-top: 14px; width: 100%; padding: 10px 12px; border-radius: 8px; border: none;
            background: #4fe3c1; color: #04120f; font-weight: 600; cursor: pointer; }}
</style>
</head>
<body>
  <div class="card">
    <h1>ARIE — read-only diagnostics</h1>
    <p class="sub">Sign in as the server owner to continue.</p>
    {error_html}
    <form action="{self._login_url}/callback" method="post">
      <input type="hidden" name="state" value="{state}">
      <input type="password" name="password" placeholder="Owner password" autofocus required>
      <button type="submit">Continue</button>
    </form>
  </div>
</body>
</html>"""
        return HTMLResponse(content=html, status_code=401 if error else 200)

    async def handle_login_callback(self, request: Request) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        try:
            self.check_login_rate_limit(client_ip)
        except RateLimitedError:
            _LOGGER.warning("login rate limit exceeded for source=%s", client_ip)
            return Response("Too many attempts. Try again later.", status_code=429)

        form = await request.form()
        password = form.get("password")
        state = form.get("state")
        if not isinstance(password, str) or not isinstance(state, str) or not state:
            return Response("Malformed login submission.", status_code=400)

        pending = self._pending.get(state)
        if pending is None:
            return Response(
                "This login link has expired. Reconnect the app and try again.", status_code=400
            )

        if not secrets.compare_digest(password, self._owner_password):
            _LOGGER.warning("login failed (wrong password) for source=%s", client_ip)
            return self.render_login_page(state, error=True)

        _LOGGER.info("login succeeded for source=%s", client_ip)
        del self._pending[state]

        new_code = f"arie_{secrets.token_urlsafe(32)}"
        redirect_uri = pending.redirect_uri
        self._auth_codes[new_code] = AuthorizationCode(
            code=new_code,
            client_id=pending.client_id,
            redirect_uri=AnyHttpUrl(redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            scopes=pending.scopes or ["mcp"],
            code_challenge=pending.code_challenge,
            resource=pending.resource,
            subject="owner",
        )
        return RedirectResponse(
            url=construct_redirect_uri(redirect_uri, code=new_code, state=state), status_code=302
        )

    # ------------------------------------------------------------- codes --

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is not None and code.expires_at < time.time():
            del self._auth_codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self._auth_codes:
            raise TokenError(
                error="invalid_grant",
                error_description="unknown or already-used authorization code",
            )
        del self._auth_codes[authorization_code.code]
        return self._issue_token(
            client,
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
        )

    # ------------------------------------------------------------ tokens --

    def _issue_token(
        self,
        client: OAuthClientInformationFull,
        scopes: list[str],
        resource: str | None,
        subject: str | None,
    ) -> OAuthToken:
        access_token = f"arie_at_{secrets.token_urlsafe(32)}"
        refresh_token = f"arie_rt_{secrets.token_urlsafe(32)}"
        now = time.time()

        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(now) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=resource,
            subject=subject,
        )
        self._refresh_tokens[refresh_token] = _RefreshTokenRecord(
            token=refresh_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + _REFRESH_TOKEN_TTL_SECONDS,
            resource=resource,
            subject=subject,
        )
        _LOGGER.info("issued access+refresh token pair for client_id=%s", client.client_id)
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self._access_tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at and access_token.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access_token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._refresh_tokens.get(refresh_token)
        if record is None or record.client_id != client.client_id:
            return None
        if record.expires_at < time.time():
            del self._refresh_tokens[refresh_token]
            return None
        return RefreshToken(
            token=record.token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=int(record.expires_at),
            resource=record.resource,
            subject=record.subject,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        record = self._refresh_tokens.get(refresh_token.token)
        if record is None or record.client_id != client.client_id:
            raise TokenError(
                error="invalid_grant", error_description="unknown or expired refresh token"
            )
        if record.expires_at < time.time():
            del self._refresh_tokens[refresh_token.token]
            raise TokenError(error="invalid_grant", error_description="refresh token has expired")

        # Rotate: the old refresh token is consumed here, before issuing the
        # replacement — a public client (every DCR-registered client is one)
        # must not be able to redeem the same refresh token twice.
        del self._refresh_tokens[refresh_token.token]
        granted_scopes = scopes or record.scopes
        _LOGGER.info("rotated refresh token for client_id=%s", client.client_id)
        return self._issue_token(client, granted_scopes, record.resource, record.subject)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)
        _LOGGER.info("revoked token for client_id=%s", token.client_id)
