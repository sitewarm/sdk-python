from __future__ import annotations

import socket
import ssl

import httpx
import pytest

import sitewarm
from sitewarm import SiteWarm, SiteWarmApiError, SiteWarmConnectionError, SiteWarmDecodeError, SiteWarmError
from sitewarm import _client

KEY = "swk_secret_key"
BASE = "https://api.example.test/v1"
SITE = {"hostname": "example.com"}


def make(handler, **kw) -> SiteWarm:
    kw.setdefault("retry_base_delay", 0.0)
    return SiteWarm(KEY, BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


@pytest.mark.parametrize("code,want", [("conflict", 3), ("idempotency-key-reused", 1)])
def test_409_conflict_on_keyed_post_is_retried_but_key_reused_is_not(code, want):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(409, json={"type": f"https://x.test/problems/{code}", "title": "c", "status": 409})

    with pytest.raises(SiteWarmApiError) as ei:
        make(handler, max_retries=2).sites.create(SITE)
    assert ei.value.code == code and n["c"] == want


def test_409_conflict_on_unkeyed_operation_is_not_retried():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(409, json={"type": "https://x.test/problems/conflict", "title": "c", "status": 409})

    with pytest.raises(SiteWarmApiError):
        make(handler, max_retries=2).sites.delete("s1")
    assert n["c"] == 1


def _chain(outer: Exception, inner: BaseException) -> Exception:
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize("make_exc,retried", [
    (lambda: _chain(httpx.ConnectError("x"), ssl.SSLCertVerificationError("bad cert")), False),
    (lambda: _chain(httpx.ConnectError("x"), socket.gaierror(socket.EAI_NONAME, "not found")), False),
    (lambda: httpx.LocalProtocolError("Illegal header value"), False),
    (lambda: httpx.UnsupportedProtocol("bad scheme"), False),
    (lambda: _chain(httpx.ConnectError("x"), socket.gaierror(socket.EAI_AGAIN, "try again")), True),
    (lambda: httpx.ConnectError("refused"), True),
])
def test_network_error_classification(make_exc, retried):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        raise make_exc()

    with pytest.raises(SiteWarmConnectionError):
        make(handler, max_retries=2).sites.get("s1")
    assert n["c"] == (3 if retried else 1)


def test_retry_after_zero_waits_at_least_the_base_with_jitter(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(_client.time, "sleep", slept.append)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if n["c"] == 1 else httpx.Response(200, json={})

    make(handler, retry_base_delay=0.2, max_retry_delay=5.0).sites.get("s1")
    assert len(slept) == 1 and 0.2 <= slept[0] <= 0.25


def test_retry_after_past_http_date_is_floored(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(_client.time, "sleep", slept.append)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        h = {"Retry-After": "Mon, 01 Jan 2001 00:00:00 GMT"}
        return httpx.Response(429, headers=h) if n["c"] == 1 else httpx.Response(200, json={})

    make(handler, retry_base_delay=0.2, max_retry_delay=5.0).sites.get("s1")
    assert 0.2 <= slept[0] <= 0.25


def test_retry_after_above_cap_is_surfaced_not_slept(monkeypatch):
    monkeypatch.setattr(_client.time, "sleep", lambda s: pytest.fail("slept"))
    with pytest.raises(SiteWarmApiError) as ei:
        make(lambda r: httpx.Response(429, headers={"Retry-After": "60"}), max_retry_delay=5.0).sites.get("s1")
    assert ei.value.retry_after_seconds == 60


def test_non_json_2xx_is_a_typed_decode_error_without_body():
    c = make(lambda r: httpx.Response(200, content=b"<html>secret-proxy-page</html>"))
    with pytest.raises(SiteWarmDecodeError) as ei:
        c.sites.get("s1")
    assert ei.value.status == 200 and isinstance(ei.value, SiteWarmError) and "secret-proxy-page" not in str(ei.value)


def test_user_agent_default_empty_custom_and_base_path_encoding():
    seen: list[str] = []

    def handler(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(200, json={})

    for ua in (None, "", "mine/1"):
        make(handler, user_agent=ua).sites.get("s1")
    default = f"sitewarm-python/{sitewarm.__version__} (+https://github.com/sitewarm/sdk-python)"
    assert seen == [default, default, "mine/1"]
    c = SiteWarm(KEY, "https://x.test/a%2Fb/v1")
    assert c._base_url == "https://x.test/a%2Fb/v1"
