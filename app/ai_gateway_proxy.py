"""
Local reverse proxy for Databricks foundation APIs (OAuth M2M).

OpenClaw sends the local key as ``Authorization: Bearer``, ``x-api-key`` (Anthropic),
or ``x-goog-api-key`` (Gemini), all matched against ``AI_GATEWAY_PROXY_LOCAL_KEY``; this process
exchanges DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET for workspace OAuth
tokens (/oidc/v1/token), refreshes before expiry, and forwards with
Authorization: Bearer <access_token>.

Upstream routing (same loopback port):
- Paths under /serving-endpoints → https://<DATABRICKS_HOST> (pay-per-token
  OpenAI Responses, Gemini /gemini/..., etc.). OpenAI Responses clients may use
  .../v1/responses; Databricks REST uses .../responses — we rewrite that prefix
  when forwarding.   Gemini clients may call .../gemini/models/... without ``v1beta``;
  Databricks expects .../gemini/v1beta/models/... — we insert ``/v1beta`` when missing.
  ``GEMINI_FORWARD_STREAMING_RPC`` in this module is ``True``: ``:streamGenerateContent`` and
  ``alt=sse`` are forwarded as-is for Databricks-native SSE. If you need unary downgrade instead,
  set it to ``False``. Drop ``alt=sse`` from the query only when that downgrade runs.
- Other paths (e.g. /openai/v1, /anthropic) → https://<WORKSPACE_ID>.ai-gateway...

Listens on 127.0.0.1 only. Configure OpenClaw baseUrl under the matching prefix.

OpenClaw uses transport sse where configured so this HTTP-only proxy avoids
WebSocket termination.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Strip client credentials so only workspace OAuth reaches Databricks.
_STRIP_CLIENT_AUTH = frozenset({"authorization", "x-api-key", "x-goog-api-key"})

# When True, do not rewrite :streamGenerateContent → :generateContent for Gemini on serving-endpoints.
GEMINI_FORWARD_STREAMING_RPC = True


def _normalize_host(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if raw.startswith("https://"):
        return raw[len("https://") :]
    if raw.startswith("http://"):
        return raw[len("http://") :]
    return raw


def _ai_gateway_origin() -> str:
    wsid = os.environ["DATABRICKS_WORKSPACE_ID"].strip()
    return f"https://{wsid}.ai-gateway.cloud.databricks.com"


def _workspace_origin() -> str:
    host = _normalize_host(os.environ["DATABRICKS_HOST"])
    return f"https://{host}"


def _upstream_origin_for_path(path: str) -> str:
    """Pay-per-token serving uses workspace host; AI Gateway uses workspace-id subdomain."""
    if path.startswith("/serving-endpoints"):
        return _workspace_origin()
    return _ai_gateway_origin()


def _rewrite_upstream_path(path: str) -> tuple[str, bool]:
    """Normalize paths for Databricks pay-per-token REST.

    Returns (upstream_path, gemini_stream_rpc_downgraded) — when True, caller
    should drop ``alt=sse`` from the query (Databricks docs show ``generateContent``;
    ``:streamGenerateContent`` often 404s on serving).
    """
    gemini_stream_rpc_downgraded = False
    prefix = "/serving-endpoints/v1/responses"
    if path == prefix:
        path = "/serving-endpoints/responses"
    elif path.startswith(prefix + "/"):
        path = "/serving-endpoints/responses" + path[len(prefix) :]

    # Gemini: Databricks uses .../gemini/v1beta/models/...:generateContent (see Databricks Gemini API).
    # Some clients call .../gemini/models/... without v1beta → 404.
    g_short = "/serving-endpoints/gemini/models"
    if path.startswith(g_short) and not path.startswith("/serving-endpoints/gemini/v1beta"):
        path = "/serving-endpoints/gemini/v1beta/models" + path[len(g_short) :]

    # If baseUrl already included v1beta and the client adds another, collapse.
    dup = "/serving-endpoints/gemini/v1beta/v1beta"
    while dup in path:
        path = path.replace(dup, "/serving-endpoints/gemini/v1beta", 1)

    # Gemini: Databricks docs use a literal colon before the RPC name (e.g. ``...pro:generateContent``).
    # Some clients send ``%3A``; forwarding percent-encoded colons can 404 on serving.
    if "/serving-endpoints/gemini/" in path and "%3A" in path:
        path = path.replace("%3A", ":")

    # Gemini streaming RPC: optional rewrite to unary (see module docstring / GEMINI_FORWARD_STREAMING_RPC).
    if "/serving-endpoints/gemini/" in path and not GEMINI_FORWARD_STREAMING_RPC:
        path2, n = re.subn(
            r":streamGenerateContent",
            ":generateContent",
            path,
            count=1,
            flags=re.IGNORECASE,
        )
        if n:
            path = path2
            gemini_stream_rpc_downgraded = True

    return path, gemini_stream_rpc_downgraded


class OAuthTokenCache:
    """Workspace OAuth M2M (client credentials) with refresh before expiry."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def bearer(self, client: httpx.AsyncClient, *, force_refresh: bool = False) -> str:
        async with self._lock:
            now = time.time()
            skew = float(os.environ.get("AI_GATEWAY_OAUTH_REFRESH_SKEW_SEC", "120"))
            if (
                not force_refresh
                and self._token
                and now < self._expires_at - skew
            ):
                return self._token

            host = _normalize_host(os.environ["DATABRICKS_HOST"])
            token_url = f"https://{host}/oidc/v1/token"
            cid = os.environ["DATABRICKS_CLIENT_ID"]
            secret = os.environ["DATABRICKS_CLIENT_SECRET"]
            scope = os.environ.get("DATABRICKS_OAUTH_SCOPE", "all-apis")

            r = await client.post(
                token_url,
                auth=(cid, secret),
                data={"grant_type": "client_credentials", "scope": scope},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30.0,
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            ttl = int(data.get("expires_in", 3600))
            self._expires_at = now + max(ttl, 60)
            return self._token


_token_cache = OAuthTokenCache()
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _http_client


def _local_key() -> str | None:
    v = os.environ.get("AI_GATEWAY_PROXY_LOCAL_KEY")
    return v if v else None


def _local_auth_ok(request: Request, key: str) -> bool:
    """OpenAI-style Bearer, Anthropic x-api-key, Gemini x-goog-api-key."""
    auth = (request.headers.get("authorization") or "").strip()
    if auth:
        if auth == key:
            return True
        pfx = "bearer "
        if auth.lower().startswith(pfx) and auth[len(pfx) :].strip() == key:
            return True
    if (request.headers.get("x-api-key") or "").strip() == key:
        return True
    if (request.headers.get("x-goog-api-key") or "").strip() == key:
        return True
    return False


def _check_local_auth(request: Request) -> Response | None:
    key = _local_key()
    if not key:
        return None
    if not _local_auth_ok(request, key):
        return Response("proxy auth required", status_code=401)
    return None


def _forward_headers(request: Request, bearer: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in request.headers.items():
        ln = name.lower()
        if ln in HOP_BY_HOP or ln in ("host", "content-length") or ln in _STRIP_CLIENT_AUTH:
            continue
        out[name] = value
    out["Authorization"] = f"Bearer {bearer}"
    return out


def _filter_response_headers(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in resp.headers.items():
        if name.lower() in HOP_BY_HOP:
            continue
        out[name] = value
    return out


async def healthz(_: Request) -> Response:
    return Response(content=b"ok", status_code=200, media_type="text/plain")


async def proxy(request: Request) -> Response:
    if err := _check_local_auth(request):
        return err

    client = _client()
    path = request.url.path
    origin = _upstream_origin_for_path(path)
    if path.startswith("/serving-endpoints"):
        upstream_path, gemini_rpc_downgrade = _rewrite_upstream_path(path)
    else:
        upstream_path, gemini_rpc_downgrade = path, False

    q = request.url.query
    if gemini_rpc_downgrade and q:
        pairs = [
            (k, v)
            for k, v in parse_qsl(q, keep_blank_values=True)
            if not (k == "alt" and v == "sse")
        ]
        q = urlencode(pairs) if pairs else ""

    url = origin + upstream_path
    if q:
        url = f"{url}?{q}"

    body: bytes | None = None
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    async def do_upstream(force_refresh: bool) -> httpx.Response:
        token = await _token_cache.bearer(client, force_refresh=force_refresh)
        headers = _forward_headers(request, token)
        req = client.build_request(
            request.method,
            url,
            headers=headers,
            content=body,
        )
        return await client.send(req, stream=True)

    upstream = await do_upstream(force_refresh=False)
    if upstream.status_code == 401:
        await upstream.aclose()
        upstream = await do_upstream(force_refresh=True)

    if upstream.is_error and upstream.status_code != 401:
        content = await upstream.aread()
        hdrs = _filter_response_headers(upstream)
        sc = upstream.status_code
        await upstream.aclose()
        return Response(content=content, status_code=sc, headers=hdrs)

    resp_headers = _filter_response_headers(upstream)

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def shutdown() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


routes = [
    Route("/healthz", healthz, methods=["GET", "HEAD"]),
    Route(
        "/{path:path}",
        proxy,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    ),
]

app = Starlette(routes=routes, on_shutdown=[shutdown])


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("AI_GATEWAY_PROXY_PORT", "18080"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
