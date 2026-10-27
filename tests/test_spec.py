from __future__ import annotations

from pathlib import Path

import yaml

from sitewarm._generated.operations import OPERATIONS

SPEC = yaml.safe_load((Path(__file__).resolve().parent.parent / "openapi" / "openapi.yaml").read_text())


def test_operation_table_matches_spec():
    expected = {
        op["operationId"]: (m.upper(), p)
        for p, item in SPEC["paths"].items()
        for m, op in item.items()
        if m in ("get", "put", "post", "patch", "delete")
    }
    assert {k: (v.method, v.path) for k, v in OPERATIONS.items()} == expected
    assert len(OPERATIONS) == 36


def test_create_warm_run_requires_idempotency_key():
    assert OPERATIONS["createWarmRun"].idempotency == "required"
    assert OPERATIONS["getWarmRun"].idempotency == "none"


def test_models_import():
    import sitewarm.models as m

    assert hasattr(m, "WarmRunCreate")


def test_no_private_extensions_in_spec():
    assert "x-open-question" not in (Path(__file__).resolve().parent.parent / "openapi" / "openapi.yaml").read_text()
