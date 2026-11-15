"""Characterization case storage and M3 side files (shared by testgen.py and recorder.py).

Files (under characterization/):
  cases/SLICE-NNNN/CT-NNNN.json   CharacterizationCase of a planned slice (layout.case_file)
  cases/UNPLANNED/CT-NNNN.json    case whose nodes are not in a plan yet (no plan/migration_plan.json, or the
                                  node is not in any slice); `slice` is null. Re-running `tests generate` after
                                  `plan` moves the file into its slice folder.
  hypotheses.json                 HypothesesFile: what a business_rule predicts for a case (status / status class /
                                  body text). Never an expected value: only used to detect contradictions with
                                  the legacy recording (runtime findings, plan 4.5)
  untestable.json                 UntestableFile: node id -> reason, marked by a human (`tests mark-untestable`)
  testgen_report.json             TestgenReport of the last `tests generate`
  coverage.json                   CoverageReport written by `tests coverage` (recorder.py)

Scope: endpoint, job and business_rule nodes plus every quirk / suspected_bug node of the APPROVED graph
snapshot named by the contract, minus nodes the plan excludes (`MigrationPlan.excluded_nodes`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..graph.models import Graph, Node
from .models import CharacterizationCase, MigrationPlan, is_id
from .paths import Phase2Layout
from .state import atomic_write_json, file_lock, now_iso, read_json

UNPLANNED = "UNPLANNED"
SCOPE_TYPES = ("endpoint", "job", "business_rule")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleHypothesis(_Strict):
    """What the covered business rule(s) predict for one case. Checked against the recording only."""
    case_id: str
    rule_ids: list[str] = Field(default_factory=list)
    status: int | None = Field(default=None, ge=100, le=599)
    status_class: Literal["2xx", "3xx", "4xx", "5xx"] | None = None
    body_contains: str | None = Field(default=None, max_length=200)
    source: Literal["model", "offline", "human"] = "model"


class HypothesesFile(_Strict):
    schema_version: Literal[1] = 1
    hypotheses: dict[str, RuleHypothesis] = Field(default_factory=dict)   # CT id -> hypothesis


class UntestableEntry(_Strict):
    reason: str = Field(min_length=1)
    by: str = Field(min_length=1)
    at: str = ""


class UntestableFile(_Strict):
    schema_version: Literal[1] = 1
    nodes: dict[str, UntestableEntry] = Field(default_factory=dict)


class TestgenReport(_Strict):
    __test__ = False                  # not a pytest class
    schema_version: Literal[1] = 1
    created: str = ""
    prompt_version: str = ""
    offline: bool = False
    nodes: list[str] = Field(default_factory=list)          # target nodes processed
    created_cases: list[str] = Field(default_factory=list)
    kept_cases: list[str] = Field(default_factory=list)     # already existed (never overwritten)
    dropped: list[str] = Field(default_factory=list)        # "<node>: reason" for rejected proposals
    ignored_expectations: int = 0                           # model tried to write expected values
    needs_approval: list[str] = Field(default_factory=list)  # specified drafts awaiting `tests approve-spec`
    provisional: list[str] = Field(default_factory=list)
    blockers: dict[str, list[str]] = Field(default_factory=dict)   # finding id -> covered case ids
    untestable_suggestions: dict[str, str] = Field(default_factory=dict)   # model suggestions (not applied)
    stubs_written: list[str] = Field(default_factory=list)
    stopped: str = ""                                       # budget message when the run stopped early


# ------------------------------------------------------------------ files
def char_file(project, name: str) -> Path:
    return Phase2Layout(project).characterization / name


def case_path(project, case: CharacterizationCase) -> Path:
    lay = Phase2Layout(project)
    if case.slice:
        return lay.case_file(case.slice, case.id)
    if not is_id(case.id, "CT"):
        raise ValueError(f"not a valid CT id: {case.id!r}")
    return lay.cases_dir / UNPLANNED / f"{case.id}.json"


def iter_case_files(project) -> list[Path]:
    d = Phase2Layout(project).cases_dir
    if not d.is_dir():
        return []
    out = []
    for sub in sorted(d.iterdir()):
        if sub.is_dir() and (sub.name == UNPLANNED or is_id(sub.name, "SLICE")):
            out += sorted(f for f in sub.glob("CT-*.json") if is_id(f.stem, "CT"))
    return out


def load_cases(project) -> list[CharacterizationCase]:
    """Every case, sorted by id. A corrupt case file raises (never silently skipped)."""
    out = []
    for f in iter_case_files(project):
        try:
            out.append(CharacterizationCase.model_validate_json(f.read_text()))
        except ValidationError as e:
            raise ValueError(f"invalid case file {f}: {e.errors()[0]['msg']}") from None
    return sorted(out, key=lambda c: c.id)


def find_case(project, case_id: str) -> CharacterizationCase | None:
    if not is_id(case_id, "CT"):
        raise ValueError(f"not a valid CT id: {case_id!r}")
    for f in iter_case_files(project):
        if f.stem == case_id:
            return CharacterizationCase.model_validate_json(f.read_text())
    return None


def save_case(project, case: CharacterizationCase) -> Path:
    """Write a case (atomic), removing a stale copy in another slice folder."""
    lay = Phase2Layout(project)
    lay.ensure(lay.cases_dir)
    target = case_path(project, case)
    for f in iter_case_files(project):
        if f.stem == case.id and f.resolve() != target.resolve():
            f.unlink()
    return atomic_write_json(target, case)


def load_hypotheses(project) -> HypothesesFile:
    return read_json(char_file(project, "hypotheses.json"), HypothesesFile, HypothesesFile())


def save_hypotheses(project, hf: HypothesesFile) -> None:
    lay = Phase2Layout(project)
    lay.ensure(lay.characterization)
    atomic_write_json(char_file(project, "hypotheses.json"), hf)


def load_untestable(project) -> UntestableFile:
    return read_json(char_file(project, "untestable.json"), UntestableFile, UntestableFile())


def mark_untestable(project, node_id: str, reason: str, by: str) -> UntestableEntry:
    """Record a human-approved `untestable` reason for an in-scope node (audited)."""
    from ..audit import AuditLog
    from .contract import load_contract
    reason, by = reason.strip(), by.strip()
    if not reason:
        raise ValueError("a reason is required")
    graph = contract_graph(project, load_contract(project))
    if node_id not in {n.id for n in scope_nodes(project, graph)}:
        raise ValueError(f"{node_id} is not an in-scope endpoint/job/business_rule/quirk node")
    path = char_file(project, "untestable.json")
    lay = Phase2Layout(project)
    lay.ensure(lay.characterization)
    with file_lock(path):
        uf = load_untestable(project)
        entry = UntestableEntry(reason=reason[:500], by=by, at=now_iso())
        uf.nodes[node_id] = entry
        atomic_write_json(path, uf)
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "recorder", "mark_untestable", node=node_id, by=by, reason=reason[:300])
    return entry


# ------------------------------------------------------------------ graph scope and slices
def contract_graph(project, contract) -> Graph:
    """The approved snapshot the contract names (not graph/latest.json)."""
    from ..graph.store import parse_graph
    p = (Path(project.root) / contract.graph_snapshot).resolve()
    if not p.is_relative_to(Path(project.root).resolve()):
        raise ValueError("contract graph_snapshot escapes the project folder")
    return parse_graph(p.read_text())


def load_plan(project) -> MigrationPlan | None:
    p = Phase2Layout(project).plan_json
    if not p.exists():
        return None
    try:
        return MigrationPlan.model_validate_json(p.read_text())
    except (ValidationError, ValueError):
        return None


def slice_of(project) -> dict[str, str]:
    """node id -> slice id from plan/migration_plan.json ({} before planning)."""
    plan = load_plan(project)
    return {n: s.id for s in plan.slices for n in s.node_ids} if plan else {}


def in_scope(node: Node) -> bool:
    return node.type in SCOPE_TYPES or node.quirk or node.suspected_bug


def scope_nodes(project, graph: Graph) -> list[Node]:
    plan = load_plan(project)
    excluded = set(plan.excluded_nodes) if plan else set()
    return sorted((n for n in graph.nodes if in_scope(n) and n.id not in excluded), key=lambda n: n.id)


# ------------------------------------------------------------------ endpoint names
_ROUTE = re.compile(r"^([A-Z]+)\s+(/\S*)")
_RPC = re.compile(r"^RPC\s+([\w.]+)/(\w+)")


def parse_endpoint(name: str) -> tuple[str, str, str] | None:
    """("http", METHOD, path) or ("grpc", service, rpc) from an endpoint node name; None if unparseable."""
    from ..extract.specs import route_identity
    n = route_identity(name.strip())
    m = _RPC.match(n)
    if m:
        return "grpc", m.group(1), m.group(2)
    m = _ROUTE.match(n)
    if m:
        return "http", m.group(1), m.group(2)
    return None


def route_regex(path: str) -> re.Pattern:
    """`/api/orders/:id` or `/api/orders/{id}` or `/api/<int:id>` -> regex matching one segment per param."""
    parts = []
    for seg in path.strip("/").split("/"):
        if re.fullmatch(r":\w+\??|\{[^}/]+\}|<[^>/]+>|\*", seg):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "/?$" if parts != [""] else r"^/?$")


def fill_params(path: str, value: str = "1") -> str:
    out = []
    for seg in path.strip("/").split("/"):
        out.append(value if re.fullmatch(r":\w+\??|\{[^}/]+\}|<[^>/]+>|\*", seg) else seg)
    return "/" + "/".join(out)


def dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str)
