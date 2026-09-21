"""The remote MCP entrypoint: Streamable HTTP transport + OAuth, for
deployment as its own service (Railway service ``arie-mcp`` — see
``docs/mcp-architecture.md``).

Launch: ``python -m arie_mcp.http_server``. Deliberately a separate module
from ``arie_mcp.server``, never imported by it and never the other way
around either — the stdio entrypoint stays a pure, dependency-light stdio
server with zero awareness that a network transport exists. This module is
the only place ``mcp.server.auth`` or a bound TCP port appear anywhere in
``arie_mcp``.

Same 11 tools, same DB role, same redaction/caps/audit logging as stdio —
:func:`arie_mcp.server.build_server` is called here exactly as it is there,
just with OAuth wiring supplied. Nothing tool-shaped is redefined; the only
things this module adds are the transport and the authorization layer in
front of it.
"""

from __future__ import annotations

import logging

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from arie_mcp.auth import ArieOAuthProvider
from arie_mcp.server import build_server
from arie_mcp.settings import get_settings

_LOGGER = logging.getLogger("arie_mcp.http_server")

_SCOPE = "mcp"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    if not settings.remote_auth_configured:
        # Loud and immediate, not a degraded start: an MCP server that
        # skipped auth wiring because a variable was missing would still
        # bind a public port and serve production diagnostics to anyone who
        # found the URL. Compare this to `database_configured` in
        # settings.py, which *does* degrade gracefully — that's a genuinely
        # optional dependency (DB-backed tools report DB_UNAVAILABLE); an
        # unauthenticated remote listener is not an equivalent kind of gap.
        raise SystemExit(
            "ARIE_MCP_PUBLIC_URL and MCP_OWNER_PASSWORD must both be set to start the "
            "remote MCP server — refusing to bind a public port with no authentication "
            "configured. (The stdio server, `python -m arie_mcp.server`, needs neither.)"
        )

    public_url = settings.public_url.rstrip("/")
    login_url = f"{public_url}/login"

    oauth_provider = ArieOAuthProvider(owner_password=settings.owner_password, login_url=login_url)

    auth_settings = AuthSettings(
        issuer_url=public_url,  # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(
            enabled=True,  # RFC 7591 Dynamic Client Registration — how Claude registers itself
            valid_scopes=[_SCOPE],
            default_scopes=[_SCOPE],
        ),
        required_scopes=[_SCOPE],
        # Legacy combined AS+RS mode (this one service is both) — see
        # arie_mcp.auth's module docstring for why that's the right shape
        # here, not a split authorization server.
        resource_server_url=None,
    )

    mcp = build_server(settings, auth_server_provider=oauth_provider, auth=auth_settings)

    # mypy: MCPServer.custom_route's decorator return type isn't annotated
    # precisely enough for strict mode to carry the wrapped function's own
    # signature through — narrow, targeted ignores rather than a broader
    # suppression; each function above/below the decorator keeps its own
    # explicit, checked signature.
    @mcp.custom_route("/login", methods=["GET"])  # type: ignore[untyped-decorator]
    async def login_page(request: Request) -> Response:
        state = request.query_params.get("state")
        if not state:
            return Response("Missing state parameter.", status_code=400)
        return oauth_provider.render_login_page(state)

    @mcp.custom_route("/login/callback", methods=["POST"])  # type: ignore[untyped-decorator]
    async def login_callback(request: Request) -> Response:
        return await oauth_provider.handle_login_callback(request)

    @mcp.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(request: Request) -> Response:
        # Deliberately not `get_system_health` (an authenticated MCP tool
        # that queries the database): Railway's health check is a bare
        # process-liveness probe and must never depend on the database or a
        # bearer token, or a transient DB hiccup would take the whole
        # service out of rotation for no reason.
        return JSONResponse({"status": "ok", "service": "arie-mcp"})

    _LOGGER.info(
        "arie-mcp remote server starting: issuer=%s host=%s port=%s",
        public_url,
        settings.http_host,
        settings.http_port,
    )
    mcp.run(transport="streamable-http", host=settings.http_host, port=settings.http_port)


if __name__ == "__main__":
    main()
