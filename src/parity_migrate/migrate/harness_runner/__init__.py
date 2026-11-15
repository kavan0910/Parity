"""Language-neutral characterization-test runner (docs/phase2-plan.md 4.2).

Executes `CharacterizationCase` files against "a base URL where the service under test listens", so the same
suite runs against the legacy service and every target. It needs nothing from the harness except that URL
(and, for `job` cases, the harness job endpoint on the same origin).

    from parity_migrate.migrate.harness_runner import RunnerConfig, run_case, observe
    res = run_case(case, RunnerConfig(base_url="http://127.0.0.1:8080"))   # CaseResult pass|fail|error|...

Modules: runner.py (HTTP/job execution, origin jail), compare.py (status, header allowlist, JSON-semantic body),
jsonpath.py (mask subset `$.a.b`, `$.items[*].id`), stubserver.py (stub server for external systems),
models.py (RunnerConfig, Observed, FieldDiff, CaseResult), wire.py (stdlib-only HTTP/job protocol shared with the
in-network runner), netrunner.py (stdlib-only script run in a sandbox container on a harness internal network by
`migrate.harness.run_cases_in_network`). `python -m parity_migrate.migrate.harness_runner --base-url URL` runs
case files in-process against an explicit base URL (see __main__.py).
"""
from __future__ import annotations

from .compare import compare, filter_headers
from .jsonpath import MASK, MaskError, apply_masks, diff_paths, generalize, json_equal, validate_mask
from .models import DEFAULT_HEADER_ALLOWLIST, DEFAULT_JOB_ENDPOINT, CaseResult, FieldDiff, Observed, RunnerConfig
from .runner import (CaseInvalid, HostNotAllowed, build_url, is_recorded, observe, parse_base_url, result_from_raw,
                     run_case, run_cases, wire_config)
from .stubserver import StubDef, StubServer, load_stubs, stub_path

__all__ = [
    "MASK", "MaskError", "apply_masks", "diff_paths", "generalize", "json_equal", "validate_mask",
    "compare", "filter_headers",
    "DEFAULT_HEADER_ALLOWLIST", "DEFAULT_JOB_ENDPOINT", "CaseResult", "FieldDiff", "Observed", "RunnerConfig",
    "CaseInvalid", "HostNotAllowed", "build_url", "is_recorded", "observe", "parse_base_url", "run_case",
    "run_cases", "result_from_raw", "wire_config", "StubDef", "StubServer", "load_stubs", "stub_path",
]
