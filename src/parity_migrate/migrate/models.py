"""Phase 2 shared data contracts. Every Phase 2 module codes against these; change them only additively.

All models are strict (`extra="forbid"`) and JSON (de)serialisable with `model_dump_json` /
`model_validate_json`. File locations are in migrate/paths.py; the plan sections cited are docs/phase2-plan.md.

  target/target.yaml                 TargetSpec              (plan 2)
  target/fit_report.json             FitReport[Risk]         (plan 2)
  plan/migration_plan.json           MigrationPlan[Slice]    (plan 3)
  slices/SLICE-NNNN/state.json       SliceState              (plan 5.1, transitions in migrate/state.py)
  characterization/cases/SLICE-NNNN/CT-NNNN.json  CharacterizationCase (plan 4.2)
  runs/RUN-NNNN/meta.json            ExecJob + ExecResult    (plan 6, sandbox interface in migrate/exec/)
  slices/SLICE-NNNN/equivalence.json EquivalenceReport[EquivalenceDiff] (plan 7)
  slices/SLICE-NNNN/approval.json    SliceApproval           (plan 8)
  cutover/approval.json              CutoverApproval         (plan 8)
  migration/runtime_findings.json    RuntimeFindingsFile[RuntimeFinding] (plan 4.5)

ID prefixes (allocated by migrate/ids.py, format PREFIX-NNNN, never reused):
  SLICE  migration slice        CT   characterization test case
  RISK   target-fit risk        EQ   equivalence diff / runtime finding
  RUN    one sandbox exec run
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..graph.models import Evidence

# ------------------------------------------------------------------ IDs
PREFIXES: tuple[str, ...] = ("SLICE", "CT", "RISK", "EQ", "RUN")
ID_RE = re.compile(r"^(SLICE|CT|RISK|EQ|RUN)-(\d{4,})$")


def format_id(prefix: str, n: int) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown Phase 2 id prefix {prefix!r}")
    return f"{prefix}-{n:04d}"


def is_id(value: str, prefix: str | None = None) -> bool:
    m = ID_RE.match(value or "")
    return bool(m) and (prefix is None or m.group(1) == prefix)


SliceId = Annotated[str, Field(pattern=r"^SLICE-\d{4,}$")]
CaseId = Annotated[str, Field(pattern=r"^CT-\d{4,}$")]
RiskId = Annotated[str, Field(pattern=r"^RISK-\d{4,}$")]
EqId = Annotated[str, Field(pattern=r"^EQ-\d{4,}$")]
RunId = Annotated[str, Field(pattern=r"^RUN-\d{4,}$")]
Sha256 = Annotated[str, Field(pattern=r"^([0-9a-f]{64})?$")]   # "" allowed where a hash is not known yet


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------ target spec (plan 2)
MigrationKind = Literal["version_upgrade", "framework_move", "language_move"]
Strategy = Literal["strangler", "big_bang", "in_place"]
WireCompat = Literal["strict", "json-semantic", "declared-only"]


class SourceStack(_Strict):
    language: str = Field(min_length=1)
    runtime: str = ""
    framework: str = ""          # e.g. "spring-boot@2.7"


class TargetStack(_Strict):
    language: str = Field(min_length=1)
    runtime: str = ""
    framework: str = ""
    build: str = ""              # maven | gradle | pip | poetry | npm | go-mod ...
    base_image: str = ""         # should be pinned by digest (name@sha256:...)


class Infra(_Strict):
    db: str = ""                 # e.g. "postgres@15"; unchanged unless declared
    messaging: str = "none"
    deploy: str = "container"
    port: int | None = Field(default=None, ge=1, le=65535)


class Style(_Strict):
    formatter: str = ""
    linters: list[str] = Field(default_factory=list)
    package_root: str = ""
    rules: list[str] = Field(default_factory=list)


class Libraries(_Strict):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class EquivalencePolicy(_Strict):
    wire_compat: WireCompat = "strict"
    perf_regression_max_pct: float | None = Field(default=None, ge=0)


class TargetSpec(_Strict):
    """target/target.yaml. Hashed (migrate/state.sha256_file) into every later artifact."""
    schema_version: Literal[1] = 1
    kind: MigrationKind
    source: SourceStack
    target: TargetStack
    infra: Infra = Field(default_factory=Infra)
    style: Style = Field(default_factory=Style)
    libraries: Libraries = Field(default_factory=Libraries)
    equivalence: EquivalencePolicy = Field(default_factory=EquivalencePolicy)
    strategy: Strategy = "strangler"
    budget_usd: float | None = Field(default=None, ge=0)   # project-level cap across sessions (migrate/ledger.py)

    @classmethod
    def from_yaml(cls, text: str) -> "TargetSpec":
        import yaml
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("target.yaml must be a mapping")
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        import yaml
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)


# ---------------------------------------------------------- fit analysis (plan 2)
RiskKind = Literal["no_equivalent", "library_replacement", "unmapped_library", "semantic_shift", "infra_gap", "unknown"]
Severity = Literal["low", "medium", "high"]


class Risk(_Strict):
    id: RiskId
    kind: RiskKind
    severity: Severity = "medium"
    title: str = Field(min_length=1)
    detail: str = ""
    item_ids: list[str] = Field(default_factory=list)          # graph IDs
    evidence: list[Evidence] = Field(default_factory=list)     # verified against code/ (analyze/evidence.py)
    proposed_handling: str = ""
    needs_decision: bool = False
    source: Literal["deterministic", "model"] = "deterministic"


class FitReport(_Strict):
    schema_version: Literal[1] = 1
    graph_sha256: Sha256 = ""
    target_sha256: Sha256 = ""
    risks: list[Risk] = Field(default_factory=list)
    dropped_unverified: int = 0
    created: str = ""


# ---------------------------------------------------------- slices and plan (plan 3, 5.1)
SliceKind = Literal["foundation", "entry"]
SliceStatus = Literal["planned", "tests_recorded", "generating", "built", "tests_passing", "reviewed",
                      "awaiting_human", "approved", "rejected", "blocked"]


class Slice(_Strict):
    id: SliceId
    name: str = Field(min_length=1)
    kind: SliceKind
    node_ids: list[str] = Field(default_factory=list)      # graph IDs; every in-scope node lands in exactly one slice
    depends_on: list[SliceId] = Field(default_factory=list)
    order: int = Field(ge=0)
    status: SliceStatus = "planned"                       # mirror of slices/<id>/state.json (state.py is the truth)
    risks: list[RiskId] = Field(default_factory=list)
    estimate_usd: float = Field(default=0.0, ge=0)
    blockers: list[str] = Field(default_factory=list)     # e.g. deferred finding F-0003, RISK-0002


class MigrationPlan(_Strict):
    schema_version: Literal[1] = 1
    version: int = Field(ge=1)
    contract_graph_sha256: Sha256 = ""
    target_sha256: Sha256 = ""
    created: str = ""
    slices: list[Slice] = Field(default_factory=list)
    excluded_nodes: dict[str, str] = Field(default_factory=dict)   # node id -> reason (e.g. dead_code)

    @model_validator(mode="after")
    def _consistent(self):
        ids = [s.id for s in self.slices]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate slice id")
        seen: dict[str, str] = {}
        for s in self.slices:
            for d in s.depends_on:
                if d not in ids:
                    raise ValueError(f"{s.id} depends on unknown slice {d}")
            for n in s.node_ids:
                if n in seen:
                    raise ValueError(f"node {n} in both {seen[n]} and {s.id}")
                seen[n] = s.id
        return self


class StateTransition(_Strict):
    from_status: SliceStatus
    to_status: SliceStatus
    at: str
    actor: str = "agent"         # agent | human:<name> | system
    reason: str = ""


class SliceState(_Strict):
    """slices/SLICE-NNNN/state.json. Changed only through migrate/state.py (validated transitions)."""
    schema_version: Literal[1] = 1
    slice_id: SliceId
    status: SliceStatus = "planned"
    iteration: int = Field(default=0, ge=0)          # generation iterations so far (fix-loop counter)
    blocked_from: SliceStatus | None = None          # status before entering `blocked`
    reason: str = ""                                 # reason for the current status (required for blocked/rejected)
    history: list[StateTransition] = Field(default_factory=list)


# ------------------------------------------------- characterization tests (plan 4.2-4.4)
CaseKind = Literal["http", "grpc", "job", "message", "replay"]
KNOWN_CASE_TAGS = ("provisional", "quirk", "suspected_bug", "boundary", "error_path", "auth", "happy_path",
                   "regression", "traffic")


class CaseSetup(_Strict):
    db_fixture: str | None = None                 # characterization-relative, e.g. fixtures/loans_overdue.sql
    stubs: list[str] = Field(default_factory=list)  # "<EXT-id>/<stub name>"
    clock: str | None = None                      # fixed ISO time if the harness can pin it
    seed: int | None = None


class HttpRequest(_Strict):
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str | list[str]] = Field(default_factory=dict)
    body: Any = None


class GrpcRequest(_Strict):
    service: str = Field(min_length=1)
    rpc: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)
    message: Any = None


class JobRequest(_Strict):
    job: str = Field(min_length=1)                # job node name or harness entry point
    args: list[str] = Field(default_factory=list)


class MessageRequest(_Strict):
    topic: str = Field(min_length=1)
    key: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    payload: Any = None


_REQUEST_FOR_KIND = {"http": HttpRequest, "replay": HttpRequest, "grpc": GrpcRequest, "job": JobRequest,
                     "message": MessageRequest}


class Expectation(_Strict):
    """`recorded`: legacy output is truth (filled by `tests record`). `specified`: written for a `fix`
    decision and only valid once `approved_by` is set (plan 4.4)."""
    source: Literal["recorded", "specified"] = "recorded"
    status: int | None = None                      # http status / grpc code / job exit code
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    db_after: dict[str, str] = Field(default_factory=dict)      # table -> assertion
    stub_calls: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    legacy_observed: dict[str, Any] | None = None  # for `specified`: what legacy actually returned
    recorded_runs: list[RunId] = Field(default_factory=list)
    approved_by: str = ""                          # required for `specified` before it gates a slice


class Provenance(_Strict):
    generator: Literal["test_gen", "traffic", "human", "fit"] = "test_gen"
    prompt_version: str = ""
    model: str = ""
    created: str = ""


class CharacterizationCase(_Strict):
    id: CaseId
    slice: SliceId | None = None
    covers: list[str] = Field(default_factory=list)   # graph IDs (EP-, RULE-, quirk nodes ...)
    kind: CaseKind
    setup: CaseSetup = Field(default_factory=CaseSetup)
    request: HttpRequest | GrpcRequest | JobRequest | MessageRequest
    expect: Expectation = Field(default_factory=Expectation)
    normalize: list[str] = Field(default_factory=list)    # JSONPath-like masks, e.g. "$.timestamp"
    decision: Literal["preserve", "fix"] = "preserve"
    decision_ref: str = ""                                 # quirk item id or slice decision id behind a `fix`
    provenance: Provenance = Field(default_factory=Provenance)
    tags: list[str] = Field(default_factory=list)          # see KNOWN_CASE_TAGS (free strings allowed)

    @model_validator(mode="before")
    @classmethod
    def _request_by_kind(cls, data):
        if isinstance(data, dict) and isinstance(data.get("request"), dict):
            model = _REQUEST_FOR_KIND.get(data.get("kind"))
            if model is not None:
                data = {**data, "request": model.model_validate(data["request"])}
        return data

    @model_validator(mode="after")
    def _check(self):
        if not isinstance(self.request, _REQUEST_FOR_KIND[self.kind]):
            raise ValueError(f"request shape does not match kind {self.kind!r}")
        if self.expect.source == "specified" and self.decision != "fix":
            raise ValueError("expect.source 'specified' is only allowed for decision 'fix'")
        return self


# ------------------------------------------------------------ exec sandbox (plan 6)
ExecPhase = Literal["build", "test"]
NetworkMode = Literal["none", "mirror"]


class Mount(_Strict):
    source: str = Field(min_length=1)    # project-relative path (e.g. legacy_src, target_code, runs/RUN-0001/work)
    target: str = Field(min_length=1)    # absolute path inside the container
    read_only: bool = True

    @field_validator("source")
    @classmethod
    def _rel(cls, v: str) -> str:
        if v.startswith(("/", "\\")) or (len(v) > 1 and v[1] == ":") or ".." in v.replace("\\", "/").split("/"):
            raise ValueError("mount source must be project-relative without '..'")
        return v

    @field_validator("target")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not v.startswith("/") or ".." in v.split("/"):
            raise ValueError("mount target must be an absolute container path without '..'")
        return v


class Limits(_Strict):
    cpus: float = Field(default=2.0, gt=0)
    memory_mb: int = Field(default=2048, ge=64)
    pids: int = Field(default=512, ge=16)
    timeout_s: int = Field(default=900, ge=1)
    disk_mb: int = Field(default=4096, ge=16)


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecJob(_Strict):
    """One disposable container run. See migrate/exec/base.py for the backend contract."""
    run_id: RunId
    image: str = Field(min_length=1)               # pinned by digest where possible
    command: list[str] = Field(min_length=1)       # argv, never a shell string
    phase: ExecPhase
    network: NetworkMode = "none"
    workdir: str = "/work"
    mounts: list[Mount] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    env: dict[str, str] = Field(default_factory=dict)            # no secrets (validated)
    synthetic_secrets: list[str] = Field(default_factory=list)   # env names the backend fills with random values
    labels: dict[str, str] = Field(default_factory=dict)         # e.g. {"slice": "SLICE-0003", "stack": "legacy"}

    @field_validator("env")
    @classmethod
    def _no_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        from ..ingest.secrets import redact_text
        for k, val in v.items():
            if not _ENV_NAME.match(k):
                raise ValueError(f"invalid env name {k!r}")
            if redact_text("", f"{k}={val}")[1] or redact_text("", val)[1]:
                raise ValueError(f"env {k} looks like a secret; use synthetic_secrets or a mirror, never real values")
        return v

    @field_validator("synthetic_secrets")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        for k in v:
            if not _ENV_NAME.match(k):
                raise ValueError(f"invalid env name {k!r}")
        return v

    @model_validator(mode="after")
    def _network(self):
        if self.phase == "test" and self.network != "none":
            raise ValueError("test-phase jobs have no network (network must be 'none')")
        if not self.workdir.startswith("/"):
            raise ValueError("workdir must be an absolute container path")
        if set(self.env) & set(self.synthetic_secrets):
            raise ValueError("a name cannot be both in env and synthetic_secrets")
        return self


class ExecResult(_Strict):
    run_id: RunId
    exit_code: int | None = None                   # None when the job never started (see `error`)
    duration_s: float = Field(default=0.0, ge=0)
    timed_out: bool = False
    oom_killed: bool = False
    stdout_ref: str = ""                           # project-relative, runs/RUN-NNNN/logs/stdout.txt (capped, redacted)
    stderr_ref: str = ""
    stdout_tail: str = ""                          # last few KiB, redacted: safe to show/feed (delimited) to a model
    stderr_tail: str = ""
    truncated: bool = False
    reports: list[str] = Field(default_factory=list)   # project-relative report files (JUnit XML, JSON results)
    image_digest: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""                                # backend error (e.g. docker unavailable); exit_code None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error


# ------------------------------------------------------------ equivalence (plan 7)
DiffClass = Literal["match", "intended_divergence", "noise", "regression", "unknown"]
Triage = Literal["legacy_bug", "new_bug", "harness_issue", "spec_ambiguity"]


class EquivalenceDiff(_Strict):
    id: EqId
    case_id: CaseId
    slice: SliceId | None = None
    field: str = Field(min_length=1)             # "status", "headers.content-type", "body.$.total", "db_after.loans"
    legacy: Any = None
    new: Any = None
    classification: DiffClass
    triage: Triage | None = None
    triage_quote: str = ""                       # verbatim from the diff (verified, C-09 pattern)
    decision_ref: str = ""                       # for intended_divergence: the fix decision
    note: str = ""


class EquivalenceReport(_Strict):
    schema_version: Literal[1] = 1
    slice: SliceId | None = None
    legacy_run: RunId | None = None
    new_run: RunId | None = None
    cases_total: int = Field(default=0, ge=0)
    cases_equivalent: int = Field(default=0, ge=0)   # every field match / noise / approved intended divergence
    diffs: list[EquivalenceDiff] = Field(default_factory=list)
    untestable: dict[str, str] = Field(default_factory=dict)   # graph id -> approved reason
    created: str = ""

    @property
    def equivalence_pct(self) -> float:
        return 100.0 if self.cases_total == 0 else round(100.0 * self.cases_equivalent / self.cases_total, 2)

    @property
    def unexplained(self) -> list[EquivalenceDiff]:
        return [d for d in self.diffs if d.classification in ("regression", "unknown")]


# ------------------------------------------------------------ approvals (plan 8)
class SliceApproval(_Strict):
    slice_id: SliceId
    approved_by: str = Field(min_length=1)
    approved_at: str
    commit_sha: str = ""                         # target_code/ commit approved (tag slice/<id>/approved)
    report_hashes: dict[str, Sha256] = Field(default_factory=dict)   # project-relative file -> sha256
    accepted_blockers: list[str] = Field(default_factory=list)       # e.g. F-0003 "migrate legacy behaviour as-is"
    note: str = ""


class CutoverApproval(_Strict):
    approved_by: str = Field(min_length=1)
    approved_at: str
    contract_sha256: Sha256
    plan_sha256: Sha256
    tree_commit: str = Field(min_length=1)       # final target_code/ commit
    equivalence_report_sha256: Sha256
    slice_ids: list[SliceId] = Field(default_factory=list)
    note: str = ""


# ------------------------------------------------------------ runtime findings (plan 4.5)
class RuntimeFinding(_Strict):
    """Recorded legacy behaviour that contradicts the Phase 1 graph. Uses EQ- ids (the EQ allocator)."""
    id: EqId
    kind: Literal["contradicts_rule", "legacy_failure", "undocumented_behaviour"]
    title: str = Field(min_length=1)
    detail: str = ""
    item_ids: list[str] = Field(default_factory=list)
    case_ids: list[CaseId] = Field(default_factory=list)
    run_id: RunId | None = None
    observed: str = ""                           # capped, redacted excerpt of the legacy output
    status: Literal["open", "accepted_recorded", "reopened_phase1"] = "open"
    created: str = ""


class RuntimeFindingsFile(_Strict):
    schema_version: Literal[1] = 1
    findings: list[RuntimeFinding] = Field(default_factory=list)
