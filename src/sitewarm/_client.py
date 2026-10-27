from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator, Mapping, Optional, Union
from urllib.parse import quote, urlencode, urlsplit

import httpx

from ._errors import (
    SiteWarmConnectionError,
    SiteWarmError,
    SiteWarmTimeoutError,
    SiteWarmWaitTimeoutError,
    api_error_for,
)
from ._generated.operations import OPERATIONS
from ._retry import backoff_delay, parse_retry_after

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TERMINAL = {"completed", "canceled", "failed"}


@dataclass(frozen=True)
class ApiResponse:
    data: Any
    status: int
    headers: Mapping[str, str]


@dataclass
class _Prepared:
    method: str
    url: str
    headers: dict[str, str]
    content: Optional[bytes]
    # 429 means "not processed". 5xx and network errors may have been, so they are only retried when
    # repeating is safe: not a POST, or a POST carrying an Idempotency-Key.
    retry_ambiguous: bool


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
        self._api_key = api_key
        self._base_url = _normalize_base_url(base_url)
        self._max_retries = max_retries
        self._retry_base = retry_base_delay
        self._retry_max = max_retry_delay
        self._timeout = timeout
        self._user_agent = user_agent or "sitewarm-python"

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
        url = self._base_url + _expand_path(op.path, path) + _query_string(query)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
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
        return _Prepared(op.method, url, headers, content, op.method != "POST" or sends_key)

    def _decide(self, resp: httpx.Response, attempt: int, p: _Prepared) -> Union[ApiResponse, float]:
        """Returns the response to hand back, or the seconds to wait before retrying. Raises on errors."""
        if resp.is_success:
            return ApiResponse(_parse_body(resp), resp.status_code, resp.headers)
        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        status = resp.status_code
        retryable = status == 429 or (status >= 500 and status != 501 and p.retry_ambiguous)
        if retryable and attempt < self._max_retries:
            # A server-provided wait longer than we are willing to sleep is surfaced instead of ignored.
            wait = retry_after if retry_after is not None else backoff_delay(attempt, self._retry_base, self._retry_max)
            if wait <= self._retry_max:
                return float(wait)
        raise api_error_for(status, _parse_problem(resp), retry_after, _rate_limit(resp.headers))

    def _transport_error(self, err: Exception, attempt: int, p: _Prepared) -> float:
        if attempt < self._max_retries and p.retry_ambiguous:
            return backoff_delay(attempt, self._retry_base, self._retry_max)
        if isinstance(err, httpx.TimeoutException):
            raise SiteWarmTimeoutError(f"request timed out after {self._timeout} s") from err
        raise SiteWarmConnectionError(f"request failed: {type(err).__name__}") from err


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
            try:
                resp = self._http.request(p.method, p.url, headers=p.headers, content=p.content, timeout=self._timeout)
            except httpx.HTTPError as err:
                time.sleep(self._transport_error(err, attempt, p))
                attempt += 1
                continue
            out = self._decide(resp, attempt, p)
            if isinstance(out, ApiResponse):
                return out
            time.sleep(out)
            attempt += 1

    def pages(self, operation_id: str, *, max_pages: int = 10_000, **args: Any) -> Iterator[Any]:
        """Yields the pages (``{"data": [...], "page": {...}}``) of a list operation."""
        query = dict(args.pop("query", None) or {})
        cursor = query.get("cursor")
        for _ in range(max_pages):
            if cursor:
                query["cursor"] = cursor
            page = self.call(operation_id, query=query, **args)
            yield page
            nxt = page["page"].get("next_cursor")
            if not page["page"].get("has_more") or not nxt:
                return
            if nxt == cursor:
                raise SiteWarmError("pagination cursor did not advance")
            cursor = nxt

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
            try:
                resp = await self._http.request(p.method, p.url, headers=p.headers, content=p.content, timeout=self._timeout)
            except httpx.HTTPError as err:
                await asyncio.sleep(self._transport_error(err, attempt, p))
                attempt += 1
                continue
            out = self._decide(resp, attempt, p)
            if isinstance(out, ApiResponse):
                return out
            await asyncio.sleep(out)
            attempt += 1

    async def pages(self, operation_id: str, *, max_pages: int = 10_000, **args: Any) -> AsyncIterator[Any]:
        query = dict(args.pop("query", None) or {})
        cursor = query.get("cursor")
        for _ in range(max_pages):
            if cursor:
                query["cursor"] = cursor
            page = await self.call(operation_id, query=query, **args)
            yield page
            nxt = page["page"].get("next_cursor")
            if not page["page"].get("has_more") or not nxt:
                return
            if nxt == cursor:
                raise SiteWarmError("pagination cursor did not advance")
            cursor = nxt

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


def _parse_body(resp: httpx.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise SiteWarmError(f"invalid JSON in {resp.status_code} response") from None


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
