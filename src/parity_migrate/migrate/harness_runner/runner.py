"""Execute CharacterizationCase files against a base URL (stdlib urllib only).

Safety rules:
* Only the scheme://host:port of `RunnerConfig.base_url` is ever contacted. A case path is joined to the base
  and the resulting URL must still point to that origin (absolute URLs, `//host` paths, control characters and
  `..` segments are refused). Environment proxies are ignored (ProxyHandler({})).
* Redirects are not followed by default: a 3xx is a recorded response like any other. With
  `follow_redirects=True` only same-origin redirects are followed; any other target is an error result
  (`error_class="host_refused"`).
* Every request has a timeout; response bodies are capped (`max_body_bytes`).

Kinds: `http` and `replay` (HTTP request), `job` (POST {"job", "args"} to the harness job endpoint on the same
origin, which answers {"exit_code": int, "output": any}; the exit code is compared as `status`), `grpc` and
`message` return `not_implemented` results with the reason.
"""
from __future__ import annotations

from typing import Any

from ..models import CharacterizationCase, Expectation
from . import wire
from .compare import compare, filter_headers
from .models import CaseResult, Observed, RunnerConfig
from .wire import CaseInvalid, HostNotAllowed, build_url, parse_base_url

__all__ = ["CaseInvalid", "HostNotAllowed", "build_url", "parse_base_url", "http_exchange", "observe",
           "result_from_raw", "wire_config", "is_recorded", "run_case", "run_cases"]


def wire_config(cfg: RunnerConfig) -> dict:
    """The plain-dict config understood by wire.py (and the in-network runner)."""
    return {"base_url": cfg.base_url, "timeout_s": cfg.timeout_s, "job_endpoint": cfg.job_endpoint,
            "follow_redirects": cfg.follow_redirects, "max_redirects": cfg.max_redirects,
            "max_body_bytes": cfg.max_body_bytes, "extra_headers": dict(cfg.extra_headers)}


def http_exchange(cfg: RunnerConfig, method: str, path: str, *, headers: dict[str, str] | None = None,
                  query: dict | None = None, body: Any = None) -> Observed:
    """One HTTP request to the base origin. Raises HostNotAllowed / CaseInvalid / OSError-family errors."""
    d = wire.exchange(wire_config(cfg), method, path, headers=headers, query=query, body=body)
    return Observed(status=d["status"], headers=filter_headers(d["headers"], cfg.header_allowlist), body=d["body"],
                    body_is_json=d["body_is_json"], duration_s=d["duration_s"])


def result_from_raw(case: CharacterizationCase, raw: Any, cfg: RunnerConfig, *, compare_expect: bool = False) \
        -> CaseResult:
    """CaseResult from a plain observation dict (wire.observe_raw, possibly produced inside a sandbox container:
    treated as untrusted data). Headers are allowlisted here; `compare_expect` compares with case.expect like
    run_case. Malformed observations become `error` results (error_class protocol)."""
    base = dict(case_id=case.id, kind=case.kind, expectation_source=case.expect.source, masks=list(case.normalize))
    if not isinstance(raw, dict):
        return CaseResult(**base, outcome="error", error="runner returned a malformed result",
                          error_class="protocol")
    outcome = raw.get("outcome")
    if outcome in ("error", "not_implemented"):
        cls = raw.get("error_class") if outcome == "error" else ""
        if cls not in ("", "transport", "host_refused", "config", "invalid_case", "timeout", "protocol"):
            cls = "protocol"
        return CaseResult(**base, outcome=outcome, error=str(raw.get("error") or "")[:2000], error_class=cls)
    o = raw.get("observed")
    try:
        if outcome != "unrecorded" or not isinstance(o, dict):
            raise ValueError("no observation")
        status = o.get("status")
        if status is not None and (not isinstance(status, int) or isinstance(status, bool)):
            raise ValueError("status is not an integer")
        hdrs = o.get("headers") or {}
        if not isinstance(hdrs, dict):
            raise ValueError("headers is not an object")
        obs = Observed(status=status, headers=filter_headers({str(k): str(v) for k, v in hdrs.items()},
                                                             cfg.header_allowlist),
                       body=o.get("body"), body_is_json=bool(o.get("body_is_json")),
                       duration_s=float(o.get("duration_s") or 0.0))
    except (ValueError, TypeError) as e:
        return CaseResult(**base, outcome="error", error=f"runner returned a malformed result: {e}"[:300],
                          error_class="protocol")
    res = CaseResult(**base, outcome="unrecorded", observed=obs)
    if not compare_expect or not is_recorded(case.expect):
        return res
    diffs = compare(case.expect, obs, case.normalize, cfg.header_allowlist)
    return res.model_copy(update={"outcome": "fail" if diffs else "pass", "diffs": diffs})


def observe(case: CharacterizationCase, cfg: RunnerConfig) -> CaseResult:
    """Execute a case and return what was observed (outcome `unrecorded`), or an error/not_implemented result.
    No comparison is made (the recorder uses this)."""
    return result_from_raw(case, wire.observe_raw(case.model_dump(mode="json"), wire_config(cfg)), cfg)


def is_recorded(expect: Expectation) -> bool:
    return expect.status is not None


def run_case(case: CharacterizationCase, cfg: RunnerConfig) -> CaseResult:
    """Execute and compare against `case.expect` (recorded or specified). Unrecorded cases return
    `unrecorded` with the observation attached."""
    res = observe(case, cfg)
    if res.outcome != "unrecorded" or res.observed is None:
        return res
    if not is_recorded(case.expect):
        return res
    diffs = compare(case.expect, res.observed, case.normalize, cfg.header_allowlist)
    return res.model_copy(update={"outcome": "fail" if diffs else "pass", "diffs": diffs})


def run_cases(cases: list[CharacterizationCase], cfg: RunnerConfig) -> list[CaseResult]:
    parse_base_url(cfg.base_url)          # fail fast on an unusable base URL
    return [run_case(c, cfg) for c in cases]
