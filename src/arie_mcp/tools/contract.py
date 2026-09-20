"""``inspect_api_contract`` (spec § 9.2) — the one V0.1 tool that talks to
the running API process rather than the database, because its entire
purpose is reading the *live* generated OpenAPI schema, not a static file.
No mutation of any kind: a single ``GET``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel

from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import UpstreamUnavailableError
from arie_mcp.limits import MAX_RESPONSE_BYTES

_ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


class RouteInfo(BaseModel):
    method: str
    path: str
    summary: str | None
    tags: list[str]


async def inspect_api_contract_impl(
    api_base_url: str,
    *,
    path_prefix: str | None,
    include_schemas: bool,
    timeout_seconds: float,
) -> ToolOutcome:
    url = api_base_url.rstrip("/") + "/openapi.json"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
            response.raise_for_status()
            spec = response.json()
    except httpx.HTTPError as exc:
        raise UpstreamUnavailableError(
            f"could not reach the API's OpenAPI schema at {api_base_url}"
        ) from exc

    routes: list[dict[str, Any]] = []
    for path, methods in spec.get("paths", {}).items():
        if path_prefix and not path.startswith(path_prefix):
            continue
        for method, operation in methods.items():
            if method.lower() not in _ROUTE_METHODS:
                continue
            routes.append(
                RouteInfo(
                    method=method.upper(),
                    path=path,
                    summary=operation.get("summary"),
                    tags=operation.get("tags", []),
                ).model_dump(mode="json")
            )
    routes.sort(key=lambda r: (r["path"], r["method"]))

    data: dict[str, Any] = {
        "openapi_version": spec.get("openapi"),
        "title": spec.get("info", {}).get("title"),
        "route_count": len(routes),
        "routes": routes,
    }
    truncated = False
    if include_schemas:
        schemas = spec.get("components", {}).get("schemas", {})
        if len(json.dumps(schemas).encode("utf-8")) > MAX_RESPONSE_BYTES:
            truncated = True
        else:
            data["schemas"] = schemas

    return ToolOutcome(data=data, row_count=len(routes), truncated=truncated)
