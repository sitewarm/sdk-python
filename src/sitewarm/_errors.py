from __future__ import annotations

import re
from typing import Any, Optional


class SiteWarmError(Exception):
    """Base class of every error raised by this SDK. Never contains the API key."""


class SiteWarmConnectionError(SiteWarmError):
    """The request never produced an HTTP response (DNS, TLS, reset, ...)."""


class SiteWarmTimeoutError(SiteWarmConnectionError):
    """The per-request timeout elapsed."""


class SiteWarmResponseTooLargeError(SiteWarmError):
    """The response body was larger than the SDK reads (32 MiB). Not retried."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"response body is larger than {limit} bytes")
        self.limit = limit


class SiteWarmApiError(SiteWarmError):
    """The API answered with a non-2xx status.

    ``problem`` is the RFC 9457 body, ``code`` the problem type slug when present,
    ``retry_after_seconds`` the parsed ``Retry-After`` and ``rate_limit`` the RateLimit-* headers.
    """

    def __init__(
        self,
        status: int,
        problem: dict[str, Any],
        retry_after_seconds: Optional[int] = None,
        rate_limit: Optional[dict[str, int]] = None,
    ) -> None:
        title, detail = problem.get("title"), problem.get("detail")
        super().__init__(
            f"sitewarm API error {status}" + (f": {title}" if title else "") + (f" ({detail})" if detail else "")
        )
        self.status = status
        self.problem = problem
        self.code = _problem_code(problem)
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit = rate_limit or {}


class SiteWarmAuthenticationError(SiteWarmApiError): ...
class SiteWarmPermissionError(SiteWarmApiError): ...
class SiteWarmNotFoundError(SiteWarmApiError): ...
class SiteWarmConflictError(SiteWarmApiError): ...
class SiteWarmValidationError(SiteWarmApiError):
    """400 and 422: inspect ``problem["errors"]`` for field-level details."""
class SiteWarmRateLimitError(SiteWarmApiError): ...
class SiteWarmServerError(SiteWarmApiError): ...


class SiteWarmWaitTimeoutError(SiteWarmError):
    """``wait_for_run`` gave up before the run finished. The run keeps going server side."""

    def __init__(self, run_id: str, last_status: str, timeout: float) -> None:
        super().__init__(f"warm run {run_id} did not finish within {timeout} s (last status: {last_status})")
        self.run_id = run_id
        self.last_status = last_status


def _problem_code(problem: dict[str, Any]) -> Optional[str]:
    code = problem.get("code")
    if isinstance(code, str):
        return code
    t = problem.get("type")
    if isinstance(t, str):
        slug = [p for p in t.split("/") if p][-1:] or [""]
        if re.fullmatch(r"[a-z0-9-]+", slug[0]):
            return slug[0]
    return None


def api_error_for(
    status: int,
    problem: dict[str, Any],
    retry_after_seconds: Optional[int] = None,
    rate_limit: Optional[dict[str, int]] = None,
) -> SiteWarmApiError:
    cls: type[SiteWarmApiError]
    if status == 401:
        cls = SiteWarmAuthenticationError
    elif status == 403:
        cls = SiteWarmPermissionError
    elif status == 404:
        cls = SiteWarmNotFoundError
    elif status == 409:
        cls = SiteWarmConflictError
    elif status in (400, 422):
        cls = SiteWarmValidationError
    elif status == 429:
        cls = SiteWarmRateLimitError
    elif status >= 500:
        cls = SiteWarmServerError
    else:
        cls = SiteWarmApiError
    return cls(status, problem, retry_after_seconds, rate_limit)
