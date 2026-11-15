"""Latency comparison and differential fuzzing (docs/phase2-plan.md section 7: beyond the recorded cases).

Both are advisory: they never block `approve-slice`, but they are written next to the equivalence report, shown in
`equivalence/report.md` and counted as warnings by `cutover`.

    latency_summary(rows)                       # pure: (case_id, legacy_s, new_s) rows -> LatencySummary
    run_fuzz(project, slice_id, legacy_url=None, new_url=None, *, max_variants=8) -> FuzzReport

Latency: every equivalence run records each side's response time per case (`CaseOutcome.legacy_s` / `new_s`) and
a `LatencySummary` (p50 / p95 per side, the new/legacy ratio, and the cases where the new service is slower by
more than `ratio_limit` AND `min_delta_s`). Timings in a disposable sandbox are noisy, so this is a smoke signal
(a missing index, an N+1 query), not a benchmark.

Fuzzing: for each recorded HTTP case of the slice a fixed, deterministic set of mutations is derived from the
recorded request (no model, no randomness): empty / long / numeric-edge / unicode query values, JSON body fields
set to null, a wrong type, empty or removed, an unknown extra field, another body shape when there is no object
body, an unknown query parameter. Legacy is asked twice per variant; fields
where legacy disagrees with itself are noise and ignored. Anything the new service answers differently from the
legacy service is a `FuzzDivergence` (severity `high` when one side answers 5xx and the other does not or the
status class differs, else `medium`). Paths and methods are never mutated, and the variants run in the same
sandbox networks as the recorded cases (the runner's origin jail applies).
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .state import atomic_write_json, now_iso, read_json

LATENCY_RATIO = 2.0
LATENCY_MIN_DELTA_S = 0.05
MAX_DIVERGENCES = 200
FUZZ_REPORT_DIR = "fuzz"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ latency
class SlowCase(_Strict):
    case_id: str
    legacy_s: float
    new_s: float
    ratio: float


class LatencySummary(_Strict):
    cases: int = 0
    legacy_p50_s: float | None = None
    legacy_p95_s: float | None = None
    new_p50_s: float | None = None
    new_p95_s: float | None = None
    ratio_p50: float | None = None
    ratio_p95: float | None = None
    ratio_limit: float = LATENCY_RATIO
    min_delta_s: float = LATENCY_MIN_DELTA_S
    slower: list[SlowCase] = Field(default_factory=list)


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no data."""
    if not values:
        return None
    xs = sorted(values)
    k = max(1, math.ceil(q / 100.0 * len(xs)))
    return xs[min(k, len(xs)) - 1]


def _ratio(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return round(new / old, 3)


def latency_summary(rows: list[tuple[str, float | None, float | None]], *, ratio_limit: float = LATENCY_RATIO,
                    min_delta_s: float = LATENCY_MIN_DELTA_S) -> LatencySummary | None:
    """rows: (case_id, legacy_s, new_s). Only cases timed on BOTH sides count. None when nothing was timed."""
    both = [(c, a, b) for c, a, b in rows if a is not None and b is not None and a >= 0 and b >= 0]
    if not both:
        return None
    lv, nv = [a for _c, a, _b in both], [b for _c, _a, b in both]
    lp50, lp95, np50, np95 = percentile(lv, 50), percentile(lv, 95), percentile(nv, 50), percentile(nv, 95)
    slower = []
    for c, a, b in both:
        if b > a * ratio_limit and (b - a) >= min_delta_s:
            slower.append(SlowCase(case_id=c, legacy_s=round(a, 4), new_s=round(b, 4),
                                   ratio=round(b / a, 2) if a > 0 else 9999.0))
    slower.sort(key=lambda s: (-s.ratio, s.case_id))
    r = lambda v: None if v is None else round(v, 4)  # noqa: E731
    return LatencySummary(cases=len(both), legacy_p50_s=r(lp50), legacy_p95_s=r(lp95), new_p50_s=r(np50),
                          new_p95_s=r(np95), ratio_p50=_ratio(np50, lp50), ratio_p95=_ratio(np95, lp95),
                          ratio_limit=ratio_limit, min_delta_s=min_delta_s, slower=slower[:50])


# ------------------------------------------------------------------ mutations
_LONG = "A" * 2000
_QUERY_VALUES = (("empty", ""), ("long", _LONG), ("negative", "-1"), ("zero", "0"), ("huge", "9999999999999"),
                 ("unicode", "café 中文"), ("quote", "' OR '1'='1"))
_BODY_VALUES = (("null", None), ("empty_string", ""), ("long_string", _LONG), ("negative", -1), ("zero", 0),
                ("huge", 10 ** 15))


def _wrong_type(v: Any) -> Any:
    if isinstance(v, bool):
        return "true"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return 12345
    if isinstance(v, list):
        return {"unexpected": v}
    if isinstance(v, dict):
        return [v]
    return "unexpected"


def mutations(request, *, max_variants: int = 8) -> list[tuple[str, Any]]:
    """Deterministic [(label, mutated HttpRequest)] derived from a recorded request (round-robin over the
    mutation families so a small cap still covers query, body and structure)."""
    families: list[list[tuple[str, Any]]] = []
    q_family = []
    for key in sorted(request.query):
        for label, val in _QUERY_VALUES:
            q = {**request.query, key: val}
            q_family.append((f"query.{key}={label}", request.model_copy(update={"query": q})))
    families.append(q_family)
    b_family, s_family = [], []
    body = request.body
    if isinstance(body, dict) and body:
        for key in sorted(body):
            for label, val in _BODY_VALUES:
                nb = copy.deepcopy(body)
                nb[key] = val
                b_family.append((f"body.{key}={label}", request.model_copy(update={"body": nb})))
            nb = copy.deepcopy(body)
            nb[key] = _wrong_type(body[key])
            b_family.append((f"body.{key}=wrong_type", request.model_copy(update={"body": nb})))
            nb = copy.deepcopy(body)
            del nb[key]
            s_family.append((f"body.{key}=missing", request.model_copy(update={"body": nb})))
        nb = copy.deepcopy(body)
        nb["__fuzz_unknown_field__"] = "x"
        s_family.append(("body.+unknown_field", request.model_copy(update={"body": nb})))
        s_family.append(("body={}", request.model_copy(update={"body": {}})))
    if request.method.upper() in ("POST", "PUT", "PATCH") and not (isinstance(body, dict) and body):
        # no object body to mutate (empty, absent, a list...): try other body shapes instead
        for label, val in (("array", []), ("object_with_field", {"__fuzz__": 1}), ("string", "x"),
                           ("number", 0), ("null", None)):
            if val != body:
                s_family.append((f"body.shape={label}", request.model_copy(update={"body": val})))
    if not request.query:
        s_family.append(("query.+unknown_param", request.model_copy(update={"query": {"__fuzz__": "1"}})))
    families += [b_family, s_family]
    out: list[tuple[str, Any]] = []
    idx = 0
    while len(out) < max_variants and any(idx < len(f) for f in families):
        for f in families:
            if idx < len(f) and len(out) < max_variants:
                out.append(f[idx])
        idx += 1
    return out


# ------------------------------------------------------------------ fuzz
class FuzzDivergence(_Strict):
    case_id: str
    mutation: str
    field: str
    legacy: Any = None
    new: Any = None
    severity: Literal["high", "medium"] = "medium"


class FuzzReport(_Strict):
    schema_version: Literal[1] = 1
    slice: str
    created: str = ""
    legacy_url: str = ""
    new_url: str = ""
    cases_considered: int = 0
    variants_run: int = 0
    variants_skipped_unstable: int = 0
    variants_not_answered: int = 0
    divergences: list[FuzzDivergence] = Field(default_factory=list)
    truncated: bool = False

    @property
    def high(self) -> list[FuzzDivergence]:
        return [d for d in self.divergences if d.severity == "high"]


def fuzz_report_path(project, slice_id: str) -> Path:
    from .paths import Phase2Layout
    return Phase2Layout(project).equivalence_dir / FUZZ_REPORT_DIR / f"{slice_id}.json"


def load_fuzz_report(project, slice_id: str) -> FuzzReport | None:
    return read_json(fuzz_report_path(project, slice_id), FuzzReport, None)


def load_fuzz_reports(project) -> list[FuzzReport]:
    from .paths import Phase2Layout
    d = Phase2Layout(project).equivalence_dir / FUZZ_REPORT_DIR
    out = []
    if d.is_dir():
        for p in sorted(d.glob("SLICE-*.json")):
            try:
                out.append(FuzzReport.model_validate_json(p.read_text()))
            except (ValueError, OSError):
                continue
    return out


def _severity(legacy_obs, new_obs) -> str:
    ls, ns = getattr(legacy_obs, "status", None), getattr(new_obs, "status", None)
    if not isinstance(ls, int) or not isinstance(ns, int):
        return "medium"
    if (ls >= 500) != (ns >= 500) or ls // 100 != ns // 100:
        return "high"
    return "medium"


def run_fuzz(project, slice_id: str, legacy_url: str | None = None, new_url: str | None = None, *,
             max_variants: int = 8, timeout_s: float = 10.0, header_allowlist: list[str] | None = None,
             job_endpoint: str | None = "/__harness/job", backend=None) -> FuzzReport:
    """Fuzz the slice's recorded HTTP cases against both services; write equivalence/fuzz/<slice>.json."""
    from . import equivalence as eq
    from .casestore import load_cases
    from .harness_runner import DEFAULT_HEADER_ALLOWLIST, compare
    from .models import HttpRequest
    eq._sid(slice_id)
    plan = eq._plan(project)
    if next((s for s in plan.slices if s.id == slice_id), None) is None:
        raise eq.EquivalenceError(f"{slice_id} is not in the plan")
    allow = list(header_allowlist or DEFAULT_HEADER_ALLOWLIST)
    common = dict(timeout_s=timeout_s, header_allowlist=allow, job_endpoint=job_endpoint, backend=backend)
    masks = eq.load_masks(project)
    cases = [c for c in load_cases(project) if c.slice == slice_id and c.kind in ("http", "replay")
             and isinstance(c.request, HttpRequest)]
    variants, labels = [], []
    for c in cases:
        for label, req in mutations(c.request, max_variants=max_variants):
            variants.append(c.model_copy(update={"request": req, "tags": [*c.tags, "fuzz"]}))
            labels.append(label)
    rep = FuzzReport(slice=slice_id, created=now_iso(), legacy_url=legacy_url or "", new_url=new_url or "",
                     cases_considered=len(cases))
    if variants:
        l1 = eq.observe_side(project, "legacy", legacy_url, variants, **common)
        l2 = eq.observe_side(project, "legacy", legacy_url, variants, **common)
        nn = eq.observe_side(project, "new", new_url, variants, **common)
        for case, label, a, b, n in zip(variants, labels, l1, l2, nn):
            if a.observed is None or n.observed is None:
                rep.variants_not_answered += 1
                continue
            rep.variants_run += 1
            legacy_d = eq._obs_dict(a.observed)
            noise = {d.field for d in compare(legacy_d, b.observed, [], allow)} if b.observed is not None else set()
            extra = masks.approved_for(case.id)
            diffs = [d for d in compare(legacy_d, n.observed, sorted(set(case.normalize) | set(extra)), allow)
                     if d.field not in noise]
            if b.observed is None:
                rep.variants_skipped_unstable += 1
                continue
            for d in diffs:
                if len(rep.divergences) >= MAX_DIVERGENCES:
                    rep.truncated = True
                    break
                rep.divergences.append(FuzzDivergence(case_id=case.id, mutation=label, field=str(d.field)[:200],
                                                      legacy=d.expected, new=d.actual,
                                                      severity=_severity(a.observed, n.observed)))
    p = fuzz_report_path(project, slice_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, rep)
    eq._audit(project, "equivalence_fuzz", slice=slice_id, variants=rep.variants_run,
              divergences=len(rep.divergences), high=len(rep.high))
    eq.write_report_md(project)
    return rep
