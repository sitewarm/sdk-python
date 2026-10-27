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

- **Auth**: `Authorization: Bearer <key>`. The key is not in `repr()`, error messages or logs (the SDK never logs). `api_key` must be printable ASCII without whitespace. `base_url` must be https (http only for localhost) and have no credentials; redirects are not followed, so the key never goes to another origin.
- **No default `base_url`**: pass the API host you were given.
- **Retries**: up to `max_retries` (3) on `429`, `5xx` (not `501`) and network errors, with exponential backoff and jitter capped at `max_retry_delay` (30 s). `Retry-After` is honored; if it exceeds the cap the error is raised instead of sleeping. `429` is always retried; `5xx` and network errors only when repeating is safe (not a POST, or a POST with an `Idempotency-Key`).
- **Idempotency**: every POST that accepts `Idempotency-Key` sends one (a random UUID unless you pass `idempotency_key`), generated once per call and reused by all its retries, so a retried `warm_runs.create` never starts two runs.
- **Errors**: `SiteWarmApiError` (`status`, `problem` as RFC 9457, `code`, `retry_after_seconds`, `rate_limit`) with subclasses for 401, 403, 404, 409, 400/422, 429 and 5xx; `SiteWarmConnectionError` and `SiteWarmTimeoutError` for transport; `SiteWarmWaitTimeoutError` from `warm_runs.wait`, which does not cancel the run.

## Development

```
uv venv && uv pip install -e . --group dev      # or: pip install -e . then the dev group
.venv/bin/python scripts/generate.py            # regenerate src/sitewarm/_generated from openapi/openapi.yaml
.venv/bin/python -m pytest && .venv/bin/python -m mypy
```

To update the spec, replace `openapi/openapi.yaml` and run the generator; CI fails if `src/sitewarm/_generated` is out of date. The package carries the `Private :: Do Not Upload` classifier, so PyPI refuses an upload until that line is removed on purpose.

## License

Apache-2.0, see [LICENSE](LICENSE).
