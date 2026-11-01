"""Shared async HTTP client for rrk.ir, the Official Gazette (روزنامه رسمی) portal.

Every tool goes through `fetch`, which caps concurrency, caches answers briefly and turns HTTP failures
into `ToolError` messages the model can act on. The portal is an Oracle APEX app: pages are HTML, searches are
form-style AJAX POSTs, and the session cookie must persist, so one client is shared.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any

import certifi
import httpx
from curl_cffi.const import CurlOpt
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

SITE = "https://rrk.ir"

HEADERS = {
    # User-Agent, sec-ch-ua and TLS come from the impersonated Chrome profile (see _get_client).
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}
# APEX's own jQuery AJAX POSTs (wwv_flow.ajax); a plain page GET keeps the impersonated Chrome navigation headers.
AJAX_HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": SITE,
    "X-Requested-With": "XMLHttpRequest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "priority": "u=1, i",
}

MAX_CONCURRENCY = 2
JITTER = (0.2, 0.6)  # seconds before each uncached call; the portal is slow and shared
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10
CACHE_TTL = 120  # seconds
CACHE_MAX = 64
RETRY_DELAY = 0.5
MAX_BODY = 5_000_000  # bytes; larger responses are refused
HOSTS = {"rrk.ir"}  # also the host allowlist

# rrk.ir serves its certificate without the intermediate (Certum DV TLS G2 R39 CA); browsers fetch it via AIA,
# curl does not. The public intermediate ships with the package and is added to certifi's roots.
INTERMEDIATE = Path(__file__).with_name("certs") / "certum_dv_tls_g2_r39.pem"
_ca_bundle: str | None = None

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_proxy_index = 0
_retired: set[asyncio.Task] = set()
_cache: dict[tuple, tuple[float, str]] = {}


class _XhrTransport(AsyncCurlTransport):
    """The Chrome profile sends navigation-only headers; a None value makes curl drop them on XHR calls."""

    def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
        accept = req.headers.get("accept")
        p = super()._create_request_params(req)
        extra: list[tuple[bytes, None | bytes]] = [(b"cache-control", None)]
        if req.headers.get("sec-fetch-mode") != "navigate":
            extra += [(b"upgrade-insecure-requests", None), (b"sec-fetch-user", None)]
        p["headers"] = [*p["headers"], *extra, *([(b"accept", b"*/*")] if accept == "*/*" else [])]
        return p


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client and its cookies."""
    global _transport, _client, _limit, _proxy_index
    _transport, _client, _limit, _proxy_index = transport, None, None, 0
    _cache.clear()


def _bundle() -> str:
    """A CA file with certifi's roots plus the intermediate, written once per process."""
    global _ca_bundle
    if _ca_bundle is None:
        data = Path(certifi.where()).read_bytes() + b"\n" + INTERMEDIATE.read_bytes()
        with tempfile.NamedTemporaryFile(prefix="rrk-ca-", suffix=".pem", delete=False) as f:
            f.write(data)
        _ca_bundle = f.name
    return _ca_bundle


def _proxies() -> list[str]:
    """RRK_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("RRK_MCP_PROXY", "").split(",") if p.strip()]


def _next_proxy(client: httpx.AsyncClient) -> None:
    """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

    Only the client that failed may rotate. The old client is closed late because other requests may still use it.
    """
    global _client, _limit, _proxy_index
    if len(_proxies()) < 2 or client is not _client:
        return
    _client, _limit, _proxy_index = None, None, _proxy_index + 1
    t = asyncio.create_task(_close_later(client))
    _retired.add(t)
    t.add_done_callback(_retired.discard)


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(50)  # longer than the 45 s request timeout
    await client.aclose()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        proxies = _proxies()
        # System proxy settings are ignored; the proxy only comes from RRK_MCP_PROXY.
        _client = httpx.AsyncClient(
            transport=_transport
            or _XhrTransport(
                impersonate="chrome",
                default_headers=True,
                curl_options={
                    CurlOpt.NOPROXY: "*",
                    CurlOpt.CAINFO: _bundle(),
                },  # NOPROXY: dropped unless the dict is non-empty
                proxy=proxies[_proxy_index % len(proxies)] if proxies else None,
                max_connections=2 * MAX_CONCURRENCY,
            ),
            headers=HEADERS,
            timeout=httpx.Timeout(45, connect=8),  # the portal can take 10+ s on a full-text search
            limits=httpx.Limits(max_connections=2 * MAX_CONCURRENCY),
            follow_redirects=True,
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


def new_session() -> None:
    """Forget cookies and cached pages (a fresh APEX session on the next call)."""
    global _client, _limit, _proxy_index
    _client, _limit, _proxy_index = None, None, 0
    _cache.clear()


async def fetch(url: str, data: dict[str, str] | None = None, cache: bool = True) -> str:
    """GET a page (or POST `data` as the portal's AJAX calls do) and return the response text.

    Retried up to MAX_RETRIES times on timeout, network error, 5xx or 429, with jittered exponential backoff
    or after Retry-After when sent. 403 is never retried.
    """
    host = httpx.URL(url).host
    if host not in HOSTS:
        raise ApiError(f"Refusing to call unexpected host {host}.")
    key = (url, tuple(sorted((data or {}).items())))
    if cache and (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    headers = {}
    if data is not None:
        # The page the AJAX call belongs to: .../ords/wwv_flow.ajax?p_context=rrs-front/<page>/<instance>
        ctx = httpx.URL(url).params.get("p_context", "").rpartition("/")[0]
        headers = AJAX_HEADERS | {"Referer": f"{SITE}/ords/r/rrs/{ctx}" if ctx else f"{SITE}/"}
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        wait = RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
        try:
            r, content = await _send(url, data, headers, host)
        except ApiError as e:
            if last or e.status is not None:
                raise
            await asyncio.sleep(min(wait, MAX_RETRY_WAIT))
            continue
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            await asyncio.sleep(min(_retry_after(r) or wait, MAX_RETRY_WAIT))
            continue
        break
    if r.status_code >= 400:
        raise ApiError(_status_message(r, host), r.status_code)
    text = content.decode("utf-8", "replace")
    if cache:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic(), text)
    return text


async def _send(url, data, headers, host) -> tuple[httpx.Response, bytes]:
    client, limit = _get_client()
    try:
        async with limit:
            await asyncio.sleep(random.uniform(*JITTER))
            method = "GET" if data is None else "POST"
            async with client.stream(method, url, data=data, headers=headers) as r:
                content = await _read_capped(r, host)
        if r.status_code in (403, 429):
            _next_proxy(client)  # blocked: try the next proxy on the following call
        return r, content
    except httpx.TimeoutException as e:
        raise ApiError(f"{host} did not answer in time. The portal is slow; try again in a moment.") from e
    except httpx.RequestError as e:
        if isinstance(e, httpx.ConnectError | httpx.ProxyError):
            _next_proxy(client)
        raise ApiError(
            f"Could not reach {host} ({type(e).__name__}). Check the internet connection "
            "(or the RRK_MCP_PROXY setting if you use one)."
        ) from e


async def _read_capped(r: httpx.Response, host: str) -> bytes:
    """Read the body, refusing anything over MAX_BODY so one huge answer cannot flood memory or context."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(f"{host} sent a response over {MAX_BODY // 1_000_000} MB; refused.", r.status_code)
    return bytes(buf)


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 429/503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code in (429, 503) and v.replace(".", "", 1).isdigit() else None


def _status_message(r: httpx.Response, host: str) -> str:
    code = r.status_code
    if code == 403:
        return f"{host} blocked the request (HTTP 403). Try another network or set RRK_MCP_PROXY."
    if code == 404:
        return f"{host} found nothing at that address (HTTP 404)."
    if code == 429:
        return f"{host} is rate limiting requests (HTTP 429). Wait a minute before retrying."
    if code >= 500:
        return f"{host} had a server error (HTTP {code}). Try again later."
    return f"{host} rejected the request (HTTP {code})."
