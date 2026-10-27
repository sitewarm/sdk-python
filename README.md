# sitewarm (Python)

Python client for the sitewarm API. Pydantic models are generated from the OpenAPI 3.1 spec in [`openapi/openapi.yaml`](openapi/openapi.yaml) with [datamodel-code-generator](https://github.com/koxudaxi/datamodel-code-generator); the runtime is a thin, hand-written [`httpx`](https://www.python-httpx.org) client (sync and async). Python 3.10+.

**Status: pre-release (0.1.0, not published to PyPI).** The API and this client can still change. The spec describes the whole planned surface; the service may not offer every operation yet.

```python
from sitewarm import SiteWarm

with SiteWarm(api_key=KEY, base_url="https://<api host>/v1") as sw:
    site = sw.sites.create({"hostname": "example.com"})
    sitemap = sw.sitemaps.create(site["id"], {"url": "https://example.com/sitemap.xml"})
    started = sw.warm_runs.create(site["id"], {"source": {"type": "sitemap", "sitemap_id": sitemap["id"]}})
    run = sw.warm_runs.wait(started["id"])
    for r in sw.warm_runs.list_all_results(run["id"], final_state="miss"):
        print(r["url"], r["final_state"])
```

A site must be verified before a run is accepted; verification happens outside this API.

## Surface

- Resource helpers: `sites`, `sitemaps`, `warm_runs` (`create`, `get`, `cancel`, `results`, `list_all_results`, `wait`), `cache_status_reports`. Responses are plain `dict`s (decoded JSON).
- Every operation of the spec is reachable through `sw.call("<operationId>", path={...}, query={...}, body=...)`, for example `sw.call("listSchedules", path={"siteId": site_id})`; `call_raw` also returns status and headers. The operation table is generated and a test fails if it drifts from the spec.
- `AsyncSiteWarm` has the same `call`, `call_raw`, `pages` and `paginate` (use `async with` / `async for`).
- Typing and validation: `sitewarm.models` holds the generated pydantic models. You can pass a model as a request body, or validate a response with `models.WarmRun.model_validate(run)`.
- Pagination: `sw.paginate(op, ...)` yields items, `sw.pages(op, ...)` yields pages. Cursors are opaque.
- Webhook callbacks (the `webhooks:` section of the spec) are server-to-customer and not covered.

## Behavior

- **Auth**: `Authorization: Bearer <key>`. The key is held in a redacting wrapper, so it is not in `repr()`, `str()`, `vars()`, pickles, error messages or logs (the SDK never logs). `api_key` must be printable ASCII without whitespace. `base_url` must be https (http only for localhost) and have no credentials. Redirects are never followed, even with your own `httpx` client (`follow_redirects=False` is set on every request), so the key never goes to another origin; a 3xx is raised as an API error.
- **No default `base_url`**: pass the API host you were given.
- **Retries**: up to `max_retries` (3; a negative value keeps the default) on `429`, `5xx` (not `501`) and network errors, with exponential backoff and jitter capped at `max_retry_delay` (30 s). `Retry-After` is honored; if it exceeds the cap the error is raised instead of sleeping. `429` is always retried; `5xx`, network errors and a response body that breaks off only when repeating is safe (`GET`, `HEAD`, `PUT`, `OPTIONS`, or a POST with an `Idempotency-Key`; never `DELETE`/`PATCH`). `timeout <= 0` keeps the default (30 s).
- **More retry rules**: a `409` `conflict` on a keyed POST is retried (the first attempt may still be running); `idempotency-key-reused` never. Certificate/TLS verification failures, unknown hosts and malformed requests are not retried. `Retry-After` has a floor of the retry base delay and gets up to +25% jitter; a `Retry-After` above the cap is raised, not slept.
- **Idempotency**: every POST that accepts `Idempotency-Key` sends one (a random UUID unless you pass `idempotency_key`), generated once per call and reused by all its retries, so a retried `warm_runs.create` never starts two runs.
- **Limits**: response bodies over 32 MiB raise `SiteWarmResponseTooLargeError` (not retried); `pages`/`paginate` raise on a repeated cursor or after 10,000 pages instead of stopping silently; a `limit` query value outside 1..100 raises before the request (0 or `None` = server default).
- **Errors**: `SiteWarmApiError` (`status`, `problem` as RFC 9457, `code`, `retry_after_seconds`, `rate_limit`) with subclasses for 401, 403, 404, 409, 400/422, 429 and 5xx; `SiteWarmConnectionError` and `SiteWarmTimeoutError` for transport; `SiteWarmWaitTimeoutError` from `warm_runs.wait`, which does not cancel the run; `SiteWarmDecodeError` (`status`) for a 2xx body that is not JSON (the body is never included). The `User-Agent` is `sitewarm-python/<version> (+https://github.com/sitewarm/sdk-python)`; an empty `user_agent` keeps it.

## Development

```
uv venv && uv pip install -e . --group dev      # or: pip install -e . then the dev group
.venv/bin/python scripts/generate.py            # regenerate src/sitewarm/_generated from openapi/openapi.yaml
.venv/bin/python -m pytest && .venv/bin/python -m mypy
```

To update the spec, replace `openapi/openapi.yaml` and run the generator; CI fails if `src/sitewarm/_generated` is out of date. The package carries the `Private :: Do Not Upload` classifier, so PyPI refuses an upload until that line is removed on purpose.

## License

Apache-2.0, see [LICENSE](LICENSE).
