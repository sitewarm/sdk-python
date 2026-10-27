from __future__ import annotations

import asyncio
import json
import random
import re
import socket
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Mapping, Optional, Union
from urllib.parse import quote, urlencode, urlsplit

import httpx

from ._errors import (
    SiteWarmConnectionError,
    SiteWarmDecodeError,
    SiteWarmError,
    SiteWarmResponseTooLargeError,
    SiteWarmTimeoutError,
    SiteWarmWaitTimeoutError,
    api_error_for,
)
from ._generated.operations import OPERATIONS
from ._retry import backoff_delay, parse_retry_after
from ._version import __version__

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TERMINAL = {"completed", "canceled", "failed"}
_MAX_BODY = 32 << 20  # largest response body that is read (32 MiB)
_REPEATABLE = {"GET", "HEAD", "PUT", "OPTIONS"}
_MAX_PAGES = 10_000
_DEFAULT_USER_AGENT = f"sitewarm-python/{__version__} (+https://github.com/sitewarm/sdk-python)"


class _Secret:
    """Holds the API key so that repr/str/vars/pickle of the client never reveal it."""

    __slots__ = ("_v",)

    def __init__(self, v: str) -> None:
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("the API key cannot be pickled")

    def __copy__(self) -> "_Secret":
        return self

    def __deepcopy__(self, memo: Any) -> "_Secret":
        return self


@dataclass(frozen=True)
class ApiResponse:
    data: Any
    status: int
    headers: Mapping[str, str]


@dataclass
class _Prepared:
    method: str
    url: str
    headers: dict[str, str] = field(repr=False)  # carries Authorization
    content: Optional[bytes] = field(repr=False)
    # 429 means "not processed". 5xx, network errors and broken bodies may have been, so they are only retried
    # when repeating is safe: GET, HEAD, PUT, OPTIONS, or a POST carrying an Idempotency-Key. Never DELETE/PATCH.
    retry_ambiguous: bool
    sends_key: bool = False


class _Core:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        max_retry_delay: float = 30.0,
        timeout: float = 30.0,
        user_agent: Optional[str] = None,
    ) -> None:
        # Messages below never echo the key.
        if not isinstance(api_key, str) or not re.fullmatch(r"[\x21-\x7e]+", api_key):
            raise SiteWarmError("api_key must be a non-empty string without whitespace or control characters")
        self._api_key = _Secret(api_key)
        self._base_url = _normalize_base_url(base_url)
        # A negative max_retries or a timeout <= 0 keeps the default.
        self._max_retries = int(max_retries) if _is_num(max_retries) and max_retries >= 0 else 3
        self._retry_base = retry_base_delay
        self._retry_max = max_retry_delay
        self._timeout = float(timeout) if _is_num(timeout) and timeout > 0 else 30.0
        self._user_agent = user_agent or _DEFAULT_USER_AGENT

    def __repr__(self) -> str:  # the key is never part of repr or str
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    def _prepare(
        self,
        operation_id: str,
        path: Optional[Mapping[str, Any]],
        query: Optional[Mapping[str, Any]],
        body: Any,
        idempotency_key: Optional[str],
    ) -> _Prepared:
        try:
            op = OPERATIONS[operation_id]
        except KeyError:
            raise SiteWarmError(f"unknown operation {operation_id!r}") from None
        url = self._base_url + _expand_path(op.path, path) + _query_string(_checked_query(query))
        headers = {
            "Authorization": f"Bearer {self._api_key.reveal()}",
            "Accept": "application/json, application/problem+json",
            "User-Agent": self._user_agent,
        }
        content: Optional[bytes] = None
        if body is not None:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json", exclude_none=True)
            content = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        sends_key = op.idempotency != "none" and op.method == "POST"
        if sends_key:
            # One key per call, reused by every retry, so a retried POST never starts a second run.
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        return _Prepared(op.method, url, headers, content, op.method in _REPEATABLE or sends_key, sends_key)

    def _decide(self, resp: httpx.Response, attempt: int, p: _Prepared) -> Union[ApiResponse, float]:
        """Returns the response to hand back, or the seconds to wait before retrying. Raises on errors."""
        if resp.is_success:
            return ApiResponse(_parse_body(resp), resp.status_code, resp.headers)
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        status = resp.status_code
        err = api_error_for(status, _parse_problem(resp), retry_after, _rate_limit(resp.headers))
        retryable = status == 429 or (status >= 500 and status != 501 and p.retry_ambiguous)
        # A 409 "conflict" on a keyed POST is the original request still running (a timeout retry racing it).
        # "idempotency-key-reused" is a caller bug and is never retried.
        if status == 409 and p.sends_key and err.code == "conflict":
            retryable = True
        if retryable and attempt < self._max_retries:
            if retry_after is not None:
                # Never a hot retry on "Retry-After: 0" or a date in the past; jitter spreads clients out.
                # A server-provided wait longer than the cap is surfaced instead of slept.
                wait = max(float(retry_after), self._retry_base)
                if wait <= self._retry_max:
                    return wait * (1 + random.random() / 4)
            else:
                wait = backoff_delay(attempt, self._retry_base, self._retry_max)
                if wait <= self._retry_max:
                    return float(wait)
        raise err

    def _transport_error(self, err: Exception, attempt: int, p: _Prepared, status: Optional[int] = None) -> float:
        # status is set when the failure happened while reading the body: after a 429 nothing was processed.
        if attempt < self._max_retries and (p.retry_ambiguous or status == 429) and _transient(err):
            return backoff_delay(attempt, self._retry_base, self._retry_max)
        if isinstance(err, httpx.TimeoutException):
            raise SiteWarmTimeoutError(f"request timed out after {self._timeout} s") from err
        raise SiteWarmConnectionError(f"request failed: {type(err).__name__}") from err


def _transient(err: BaseException) -> bool:
    """False for failures that repeating cannot fix: certificate/TLS verification, unknown host, malformed request."""
    if isinstance(err, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        return False
    seen: set[int] = set()
    cur: Optional[BaseException] = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return False
        if isinstance(cur, socket.gaierror) and cur.errno in _DNS_NOT_FOUND:
            return False
        cur = cur.__cause__ or cur.__context__
    return True


_DNS_NOT_FOUND = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}


class SiteWarm(_Core):
    """Synchronous client. ``call()`` reaches every operation of the spec by its ``operationId``.

    >>> sw = SiteWarm(api_key=key, base_url="https://<host>/v1")   # doctest: +SKIP
    >>> run = sw.warm_runs.create(site_id, {"source": {"type": "sitemap", "sitemap_id": sm}})  # doctest: +SKIP
    >>> done = sw.warm_runs.wait(run["id"])  # doctest: +SKIP
    """

    def __init__(self, api_key: str, base_url: str, *, http_client: Optional[httpx.Client] = None, **options: Any) -> None:
        super().__init__(api_key, base_url, **options)
        self._http = http_client or httpx.Client(follow_redirects=False)
        self._owns_http = http_client is None
        self.sites = _Sites(self)
        self.sitemaps = _Sitemaps(self)
        self.warm_runs = _WarmRuns(self)
        self.cache_status_reports = _CacheStatusReports(self)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "SiteWarm":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, operation_id: str, *, path: Optional[Mapping[str, Any]] = None, query: Optional[Mapping[str, Any]] = None,
             body: Any = None, idempotency_key: Optional[str] = None) -> Any:
        return self.call_raw(operation_id, path=path, query=query, body=body, idempotency_key=idempotency_key).data

    def call_raw(self, operation_id: str, *, path: Optional[Mapping[str, Any]] = None, query: Optional[Mapping[str, Any]] = None,
                 body: Any = None, idempotency_key: Optional[str] = None) -> ApiResponse:
        p = self._prepare(operation_id, path, query, body, idempotency_key)
        attempt = 0
        while True:
            status: list[int] = []
            try:
                # follow_redirects=False even for a caller-supplied client, so Authorization never leaves the origin.
                with self._http.stream(p.method, p.url, headers=p.headers, content=p.content, timeout=self._timeout,
                                       follow_redirects=False) as raw:
                    status.append(raw.status_code)
                    resp = _bounded(raw)
            except httpx.HTTPError as err:
                time.sleep(self._transport_error(err, attempt, p, status[0] if status else None))
                attempt += 1
                continue
            out = self._decide(resp, attempt, p)
            if isinstance(out, ApiResponse):
                return out
            time.sleep(out)
            attempt += 1

    def pages(self, operation_id: str, *, max_pages: int = _MAX_PAGES, **args: Any) -> Iterator[Any]:
        """Yields the pages (``{"data": [...], "page": {...}}``) of a list operation.

        Raises if a cursor repeats or if ``max_pages`` (10,000) pages did not reach the end.
        """
        query = dict(args.pop("query", None) or {})
        cursor = query.get("cursor")
        seen = {cursor} if cursor else set()
        for _ in range(max_pages):
            if cursor:
                query["cursor"] = cursor
            page = self.call(operation_id, query=query, **args)
            yield page
            nxt = page["page"].get("next_cursor")
            if not page["page"].get("has_more") or not nxt:
                return
            if nxt in seen:
                raise SiteWarmError("pagination cursor did not advance")
            seen.add(nxt)
            cursor = nxt
        raise SiteWarmError(f"pagination stopped after {max_pages} pages without reaching the end")

    def paginate(self, operation_id: str, **args: Any) -> Iterator[Any]:
        """Yields every item of a list operation across pages."""
        for page in self.pages(operation_id, **args):
            yield from page["data"]


class AsyncSiteWarm(_Core):
    """Asynchronous twin of :class:`SiteWarm` (same arguments, ``await`` on calls)."""

    def __init__(self, api_key: str, base_url: str, *, http_client: Optional[httpx.AsyncClient] = None, **options: Any) -> None:
        super().__init__(api_key, base_url, **options)
        self._http = http_client or httpx.AsyncClient(follow_redirects=False)
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncSiteWarm":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def call(self, operation_id: str, *, path: Optional[Mapping[str, Any]] = None, query: Optional[Mapping[str, Any]] = None,
                   body: Any = None, idempotency_key: Optional[str] = None) -> Any:
        return (await self.call_raw(operation_id, path=path, query=query, body=body, idempotency_key=idempotency_key)).data

    async def call_raw(self, operation_id: str, *, path: Optional[Mapping[str, Any]] = None, query: Optional[Mapping[str, Any]] = None,
                       body: Any = None, idempotency_key: Optional[str] = None) -> ApiResponse:
        p = self._prepare(operation_id, path, query, body, idempotency_key)
        attempt = 0
        while True:
            status: list[int] = []
            try:
                async with self._http.stream(p.method, p.url, headers=p.headers, content=p.content,
                                             timeout=self._timeout, follow_redirects=False) as raw:
                    status.append(raw.status_code)
                    resp = await _abounded(raw)
            except httpx.HTTPError as err:
                await asyncio.sleep(self._transport_error(err, attempt, p, status[0] if status else None))
                attempt += 1
                continue
            out = self._decide(resp, attempt, p)
            if isinstance(out, ApiResponse):
                return out
            await asyncio.sleep(out)
            attempt += 1

    async def pages(self, operation_id: str, *, max_pages: int = _MAX_PAGES, **args: Any) -> AsyncIterator[Any]:
        query = dict(args.pop("query", None) or {})
        cursor = query.get("cursor")
        seen = {cursor} if cursor else set()
        for _ in range(max_pages):
            if cursor:
                query["cursor"] = cursor
            page = await self.call(operation_id, query=query, **args)
            yield page
            nxt = page["page"].get("next_cursor")
            if not page["page"].get("has_more") or not nxt:
                return
            if nxt in seen:
                raise SiteWarmError("pagination cursor did not advance")
            seen.add(nxt)
            cursor = nxt
        raise SiteWarmError(f"pagination stopped after {max_pages} pages without reaching the end")

    async def paginate(self, operation_id: str, **args: Any) -> AsyncIterator[Any]:
        async for page in self.pages(operation_id, **args):
            for item in page["data"]:
                yield item


class _Sites:
    def __init__(self, c: SiteWarm) -> None:
        self._c = c

    def list(self, **query: Any) -> Any:
        return self._c.call("listSites", query=query)

    def list_all(self, **query: Any) -> Iterator[Any]:
        return self._c.paginate("listSites", query=query)

    def get(self, site_id: str) -> Any:
        return self._c.call("getSite", path={"siteId": site_id})

    def create(self, body: Any, *, idempotency_key: Optional[str] = None) -> Any:
        return self._c.call("createSite", body=body, idempotency_key=idempotency_key)

    def update(self, site_id: str, body: Any) -> Any:
        return self._c.call("updateSite", path={"siteId": site_id}, body=body)

    def delete(self, site_id: str) -> Any:
        return self._c.call("deleteSite", path={"siteId": site_id})


class _Sitemaps:
    def __init__(self, c: SiteWarm) -> None:
        self._c = c

    def list(self, site_id: str, **query: Any) -> Any:
        return self._c.call("listSitemaps", path={"siteId": site_id}, query=query)

    def get(self, site_id: str, sitemap_id: str) -> Any:
        return self._c.call("getSitemap", path={"siteId": site_id, "sitemapId": sitemap_id})

    def create(self, site_id: str, body: Any, *, idempotency_key: Optional[str] = None) -> Any:
        return self._c.call("createSitemap", path={"siteId": site_id}, body=body, idempotency_key=idempotency_key)

    def delete(self, site_id: str, sitemap_id: str) -> Any:
        return self._c.call("deleteSitemap", path={"siteId": site_id, "sitemapId": sitemap_id})


class _WarmRuns:
    def __init__(self, c: SiteWarm) -> None:
        self._c = c

    def create(self, site_id: str, body: Any, *, idempotency_key: Optional[str] = None) -> Any:
        """Starts a run (202). Sends ``Idempotency-Key`` (generated if you do not pass one)."""
        return self._c.call("createWarmRun", path={"siteId": site_id}, body=body, idempotency_key=idempotency_key)

    def get(self, run_id: str) -> Any:
        return self._c.call("getWarmRun", path={"runId": run_id})

    def cancel(self, run_id: str) -> Any:
        return self._c.call("cancelWarmRun", path={"runId": run_id})

    def list_for_site(self, site_id: str, **query: Any) -> Any:
        return self._c.call("listWarmRuns", path={"siteId": site_id}, query=query)

    def results(self, run_id: str, **query: Any) -> Any:
        return self._c.call("listWarmRunResults", path={"runId": run_id}, query=query)

    def list_all_results(self, run_id: str, **query: Any) -> Iterator[Any]:
        return self._c.paginate("listWarmRunResults", path={"runId": run_id}, query=query)

    def wait(self, run_id: str, *, timeout: float = 900.0, poll_interval: float = 5.0,
             min_poll_interval: float = 1.0, max_poll_interval: float = 60.0) -> Any:
        """Polls until the run is ``completed``, ``canceled`` or ``failed``, honoring ``Retry-After``.

        Raises :class:`SiteWarmWaitTimeoutError` on timeout; the run is NOT canceled.
        """
        deadline = time.monotonic() + timeout
        while True:
            r = self._c.call_raw("getWarmRun", path={"runId": run_id})
            run = r.data
            if run["status"] in _TERMINAL:
                return run
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SiteWarmWaitTimeoutError(run_id, run["status"], timeout)
            hinted = parse_retry_after(r.headers.get("Retry-After"))
            delay = min(max_poll_interval, max(min_poll_interval, float(hinted) if hinted is not None else poll_interval))
            if delay >= remaining:
                time.sleep(remaining)
                final = self._c.call("getWarmRun", path={"runId": run_id})
                if final["status"] in _TERMINAL:
                    return final
                raise SiteWarmWaitTimeoutError(run_id, final["status"], timeout)
            time.sleep(delay)


class _CacheStatusReports:
    def __init__(self, c: SiteWarm) -> None:
        self._c = c

    def list_for_site(self, site_id: str, **query: Any) -> Any:
        return self._c.call("listCacheStatusReports", path={"siteId": site_id}, query=query)

    def get(self, report_id: str) -> Any:
        return self._c.call("getCacheStatusReport", path={"reportId": report_id})


def _normalize_base_url(raw: str) -> str:
    u = urlsplit(raw)
    if not u.scheme or not u.hostname:
        raise SiteWarmError("base_url must be an absolute URL such as https://<host>/v1")
    if u.scheme != "https" and not (u.scheme == "http" and u.hostname in _LOOPBACK):
        raise SiteWarmError("base_url must use https (http is only allowed for localhost)")
    if u.username or u.password:
        raise SiteWarmError("base_url must not contain credentials")
    if u.query or u.fragment:
        raise SiteWarmError("base_url must not contain a query or fragment")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


def _expand_path(template: str, values: Optional[Mapping[str, Any]]) -> str:
    def sub(m: "re.Match[str]") -> str:
        v = (values or {}).get(m.group(1))
        if v is None or v == "":
            raise SiteWarmError(f'missing path parameter "{m.group(1)}"')
        return quote(str(v), safe="")

    return re.sub(r"\{(\w+)\}", sub, template)


def _query_string(query: Optional[Mapping[str, Any]]) -> str:
    if not query:
        return ""
    pairs: list[tuple[str, str]] = [(k, str(v).lower() if isinstance(v, bool) else str(v)) for k, v in query.items() if v is not None]
    return "?" + urlencode(pairs) if pairs else ""


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _checked_query(query: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """``limit`` must be 1..100 (0 or None = server default, not sent); invalid values never reach the server."""
    if not query or "limit" not in query:
        return query
    limit = query["limit"]
    if limit is None or (isinstance(limit, int) and not isinstance(limit, bool) and limit == 0):
        return {k: v for k, v in query.items() if k != "limit"}
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise SiteWarmError("limit must be between 1 and 100")
    return query


def _rebuild(raw: httpx.Response, data: bytes) -> httpx.Response:
    # iter_bytes() already decoded the body, so the copy must not claim an encoding or a length.
    headers = [(k, v) for k, v in raw.headers.multi_items() if k.lower() not in ("content-encoding", "content-length")]
    return httpx.Response(raw.status_code, headers=headers, content=data, request=raw.request)


def _bounded(raw: httpx.Response) -> httpx.Response:
    """Reads at most _MAX_BODY + 1 bytes; a larger body raises SiteWarmResponseTooLargeError (not retried)."""
    buf = bytearray()
    for chunk in raw.iter_bytes():
        buf += chunk
        if len(buf) > _MAX_BODY:
            raise SiteWarmResponseTooLargeError(_MAX_BODY)
    return _rebuild(raw, bytes(buf))


async def _abounded(raw: httpx.Response) -> httpx.Response:
    buf = bytearray()
    async for chunk in raw.aiter_bytes():
        buf += chunk
        if len(buf) > _MAX_BODY:
            raise SiteWarmResponseTooLargeError(_MAX_BODY)
    return _rebuild(raw, bytes(buf))


def _parse_body(resp: httpx.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise SiteWarmDecodeError(resp.status_code) from None


def _parse_problem(resp: httpx.Response) -> dict[str, Any]:
    fallback: dict[str, Any] = {"type": "about:blank", "title": resp.reason_phrase or f"HTTP {resp.status_code}", "status": resp.status_code}
    try:
        j = resp.json()
    except ValueError:
        return fallback
    if isinstance(j, dict):
        return {**j, "type": j.get("type") if isinstance(j.get("type"), str) else fallback["type"],
                "title": j.get("title") if isinstance(j.get("title"), str) else fallback["title"],
                "status": j.get("status") if isinstance(j.get("status"), int) else resp.status_code}
    return fallback


def _rate_limit(h: Mapping[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for header, key in (("RateLimit-Limit", "limit"), ("RateLimit-Remaining", "remaining"), ("RateLimit-Reset", "reset")):
        v = h.get(header)
        if v is not None and re.fullmatch(r"\d+", v.strip()):
            out[key] = int(v)
    return out
