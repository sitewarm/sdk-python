from __future__ import annotations

import copy
import pickle

import httpx
import pytest

from sitewarm import (
    AsyncSiteWarm,
    SiteWarm,
    SiteWarmConnectionError,
    SiteWarmError,
    SiteWarmResponseTooLargeError,
    SiteWarmServerError,
)

KEY = "swk_secret_key"
BASE = "https://api.example.test/v1"


def make(handler, **kw) -> SiteWarm:
    kw.setdefault("retry_base_delay", 0.0)
    return SiteWarm(KEY, BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


class _Broken(httpx.SyncByteStream):
    def __iter__(self):
        yield b'{"id"'
        raise httpx.ReadError("connection reset")


def test_non_positive_timeout_and_negative_retries_keep_defaults():
    for t in (0, -1, 0.0):
        assert SiteWarm(KEY, BASE, timeout=t)._timeout == 30.0
    assert SiteWarm(KEY, BASE, timeout=2)._timeout == 2.0
    assert SiteWarm(KEY, BASE, max_retries=-1)._max_retries == 3
    assert SiteWarm(KEY, BASE, max_retries=0)._max_retries == 0


def test_body_read_error_is_retried_when_repeatable():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(200, stream=_Broken())
        return httpx.Response(200, json={"id": "s1"})

    assert make(h).sites.get("s1") == {"id": "s1"} and n["c"] == 2


def test_body_read_error_on_unkeyed_post_is_not_retried(monkeypatch):
    from sitewarm._generated.operations import OPERATIONS, Operation

    monkeypatch.setitem(OPERATIONS, "fakePost", Operation("POST", "/fake", "none"))
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(200, stream=_Broken())

    with pytest.raises(SiteWarmConnectionError):
        make(h, max_retries=2).call("fakePost")
    assert n["c"] == 1


def test_oversized_response_is_typed_and_not_retried():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(200, content=b"x" * ((32 << 20) + 1))

    with pytest.raises(SiteWarmResponseTooLargeError):
        make(h).sites.get("s1")
    assert n["c"] == 1


@pytest.mark.parametrize("method,want", [("GET", 3), ("HEAD", 3), ("PUT", 3), ("OPTIONS", 3), ("DELETE", 1), ("PATCH", 1)])
def test_only_repeatable_methods_retry_5xx(monkeypatch, method, want):
    from sitewarm._generated.operations import OPERATIONS, Operation

    monkeypatch.setitem(OPERATIONS, "fakeOp", Operation(method, "/fake", "none"))
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(503)

    with pytest.raises(SiteWarmServerError):
        make(h, max_retries=2).call("fakeOp")
    assert n["c"] == want


def test_429_is_retried_for_every_method(monkeypatch):
    from sitewarm._generated.operations import OPERATIONS, Operation

    monkeypatch.setitem(OPERATIONS, "fakeOp", Operation("DELETE", "/fake", "none"))
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if n["c"] < 3 else httpx.Response(200, json={})

    make(h).call("fakeOp")
    assert n["c"] == 3


def test_pagination_cursor_cycle_and_page_cap():
    def cyc(req):
        nxt = "a" if req.url.params.get("cursor") == "b" else "b"
        return httpx.Response(200, json={"data": [1], "page": {"has_more": True, "next_cursor": nxt}})

    with pytest.raises(SiteWarmError, match="did not advance"):
        list(make(cyc).sites.list_all())

    n = {"c": 0}

    def endless(req):
        n["c"] += 1
        return httpx.Response(200, json={"data": [], "page": {"has_more": True, "next_cursor": f"c{n['c']}"}})

    with pytest.raises(SiteWarmError, match="stopped after 5 pages"):
        list(make(endless).paginate("listSites", max_pages=5))
    assert n["c"] == 5


def test_list_limit_is_validated_before_sending():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [], "page": {"has_more": False, "next_cursor": None}})

    c = make(h)
    for bad in (-1, 101, "5", True, 1.5):
        with pytest.raises(SiteWarmError, match="limit"):
            c.sites.list(limit=bad)
    assert seen == []
    for ok in (1, 100):
        c.sites.list(limit=ok)
    c.sites.list(limit=0)
    c.sites.list(limit=None)
    assert [r.url.params.get("limit") for r in seen] == ["1", "100", None, None]


def test_key_not_in_repr_vars_or_pickle():
    for c in (SiteWarm(KEY, BASE), AsyncSiteWarm(KEY, BASE)):
        for d in (repr(c), str(c), repr(vars(c)), repr(c.__dict__), repr(copy.deepcopy(c._api_key))):
            assert KEY not in d
        with pytest.raises(TypeError):
            pickle.dumps(c._api_key)
    assert KEY not in repr(SiteWarm(KEY, BASE)._prepare("getSite", {"siteId": "x"}, None, None, None))


def test_supplied_client_that_follows_redirects_never_forwards_authorization():
    hosts = []

    def h(req):
        hosts.append(req.url.host)
        if req.url.host == "api.example.test":
            return httpx.Response(302, headers={"location": "https://other.test/x"})
        return httpx.Response(200, json={})

    mine = httpx.Client(transport=httpx.MockTransport(h), follow_redirects=True)
    with pytest.raises(SiteWarmError) as e:
        SiteWarm(KEY, BASE, http_client=mine, max_retries=0).sites.get("s1")
    assert getattr(e.value, "status", None) == 302 and hosts == ["api.example.test"]


async def test_async_body_error_retry_oversize_and_redirect():
    class ABroken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id"'
            raise httpx.ReadError("reset")

    n = {"c": 0}
    hosts = []

    def h(req):
        hosts.append(req.url.host)
        if req.url.path.endswith("/redir"):
            return httpx.Response(302, headers={"location": "https://other.test/x"})
        if req.url.path.endswith("/big"):
            return httpx.Response(200, content=b"x" * ((32 << 20) + 1))
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(200, stream=ABroken())
        return httpx.Response(200, json={"id": "ok"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(h), follow_redirects=True)
    async with AsyncSiteWarm(KEY, BASE, retry_base_delay=0.0, http_client=http) as c:
        assert await c.call("getSite", path={"siteId": "a"}) == {"id": "ok"} and n["c"] == 2
        with pytest.raises(SiteWarmResponseTooLargeError):
            await c.call("getSite", path={"siteId": "big"})
        with pytest.raises(SiteWarmError):
            await c.call("getSite", path={"siteId": "redir"})
    assert "other.test" not in hosts
