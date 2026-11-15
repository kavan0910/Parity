"""M3 characterization test generation (docs/phase2-plan.md 4.3, 4.4): `tests generate`, `tests approve-spec`.

For every in-scope node of the approved graph snapshot (endpoint, job, business_rule, quirk / suspected_bug;
see casestore.py) the model task `test_gen` (prompt migrate/prompts/test_gen.md, version PROMPT_VERSION)
proposes request INPUTS as strict JSON. The model never writes expected values for preserved behaviour:
cases are created with `expect.source="recorded"` and an empty expectation that `tests record` fills from the
legacy service. Proposal validation (same path for the model and the offline proposer):
  * request method + path must match a listed endpoint of the node (path params filled); no absolute URLs,
    `//`, `..` or control characters; header and body strings are redacted (ingest/secrets.py);
  * `covers` is cut to ids that were in the prompt; the target id is always added;
  * any `expect`/`expected` key is dropped and counted (`TestgenReport.ignored_expectations`);
  * `specified` (a corrected expectation) is kept only when the contract decision for the target is `fix`:
    the case gets `decision="fix"`, `expect.source="specified"`, tag `needs_approval`; it gates nothing until
    `tests approve-spec PROJECT CT-NNNN --by NAME` sets `expect.approved_by`;
  * `hypothesis` (what a business_rule predicts) goes to characterization/hypotheses.json, never into `expect`.
Cases covering an item of a deferred Verifier finding are tagged `provisional`; the report lists the
blockers (finding id -> case ids). Case identity key `<target>/<category>/<title-slug>` -> stable CT id;
existing case files are never overwritten (only tags / slice folder are updated).

`--offline` uses a deterministic proposer (no model): baseline, unknown-id, wrong-body and auth cases per
endpoint, one case per rule / quirk / job; rule hypotheses come from an HTTP status written in the rule text.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROMPT_FILE = Path(__file__).parent / "prompts" / "test_gen.md"
PROMPT_VERSION = "test_gen.v1"
MAX_CASES_PER_NODE = 12
MAX_CODE_CHARS = 30_000
MAX_ENDPOINTS = 30
CATEGORIES = ("happy_path", "validation", "boundary", "error_path", "auth", "quirk")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
AUTH_HINT = re.compile(r"requireAuth|authenticat|isAuthenticated|login_required|@PreAuthorize|@Secured|"
                       r"permission_classes|Authorization|jwt|passport", re.I)
_STATUS_IN_TEXT = re.compile(r"\b([1-5]\d\d)\b")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecDraft(_Strict):
    status: int | None = Field(default=None, ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class Hypothesis(_Strict):
    status: int | None = Field(default=None, ge=100, le=599)
    status_class: Literal["2xx", "3xx", "4xx", "5xx"] | None = None
    body_contains: str | None = Field(default=None, max_length=200)


class Proposal(_Strict):
    """One proposed case as emitted by the model (or the offline proposer)."""
    target: str = ""
    covers: list[str] = Field(default_factory=list)
    kind: Literal["http", "job", "grpc"] = "http"
    category: Literal["happy_path", "validation", "boundary", "error_path", "auth", "quirk"] = "happy_path"
    title: str = Field(min_length=1, max_length=120)
    request: dict[str, Any]
    hypothesis: Hypothesis | None = None
    specified: SpecDraft | None = None
    stubs: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ context
@dataclass
class NodeContext:
    node: Any                                  # graph Node
    decision: str = "preserve"
    endpoints: list[tuple[Any, tuple]] = field(default_factory=list)   # (Node, parse_endpoint result)
    rules: list[Any] = field(default_factory=list)                    # linked business_rule nodes
    allowed: set[str] = field(default_factory=set)                    # ids allowed in covers
    code: str = ""
    specs: str = ""
    schemas: str = ""


def load_prompt() -> tuple[str, str]:
    """(system, user template) from prompts/test_gen.md (split at `<!-- user -->`)."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    system, sep, user = text.partition("<!-- user -->")
    if not sep:
        raise RuntimeError("test_gen.md has no <!-- user --> section")
    return system.strip(), user.strip()


def _defang(text: str) -> str:
    from ..analyze.module_analyzer import neutralize
    return neutralize(str(text)).replace("<<<", "<<").replace(">>>", ">>")


def _neighbours(graph) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.source, set()).add(e.target)
        adj.setdefault(e.target, set()).add(e.source)
    return adj


def _candidate_endpoints(graph, node, adj, by_id) -> list:
    from .casestore import parse_endpoint
    eps = [n for n in graph.nodes if n.type == "endpoint" and parse_endpoint(n.name)]
    if node.type == "endpoint":
        return [node] if parse_endpoint(node.name) else []
    seen, frontier, found = {node.id}, {node.id}, []
    for _ in range(2):
        nxt = set()
        for nid in frontier:
            for m in adj.get(nid, ()):
                if m not in seen:
                    seen.add(m)
                    nxt.add(m)
        frontier = nxt
        found += [by_id[m] for m in sorted(nxt) if m in by_id and by_id[m] in eps]
        if found:
            break
    if not found:
        files = {e.file for e in node.evidence}
        found = [n for n in eps if files & {e.file for e in n.evidence}]
    if not found:
        found = eps
    return sorted({n.id: n for n in found}.values(), key=lambda n: n.id)[:MAX_ENDPOINTS]


def _inventory(project) -> list[dict]:
    p = Path(project.inventory_path)
    if not p.exists():
        return []
    try:
        return [i for i in json.loads(p.read_text()).get("items", []) if isinstance(i, dict)]
    except (ValueError, AttributeError):
        return []


def _specs_text(tools, endpoints, inventory) -> str:
    from ..extract.specs import route_identity
    from .casestore import route_regex
    blocks = []
    for ep, parsed in endpoints:
        for it in inventory:
            attrs = it.get("attrs") or {}
            if it.get("kind") != "route" or attrs.get("source") not in ("openapi", "proto"):
                continue
            name = route_identity(str(it.get("name", "")))
            if parsed[0] == "grpc":
                hit = name == f"RPC {parsed[1]}/{parsed[2]}"
            else:
                m = re.match(r"^([A-Z]+)\s+(/\S*)", name)
                hit = bool(m) and m.group(1) == parsed[1] and bool(route_regex(parsed[2]).match(
                    re.sub(r"\{[^}/]+\}", "x", m.group(2))))
            if not hit or not it.get("file"):
                continue
            line = int(it.get("line") or 1)
            try:
                r = tools.read_file(it["file"], line, line + 30)
            except Exception:  # noqa: BLE001 - missing / refused file: facts only
                blocks.append(f"{ep.id}: {name} ({attrs.get('source')}, {it['file']}:{line})")
                continue
            blocks.append(f"=== {ep.id}: {name} from {it['file']} (lines {r.start}-{r.end}) ===\n{r.content}")
    return "\n".join(blocks) or "(none)"


def _schemas_text(inventory) -> str:
    out = []
    for it in inventory:
        if it.get("kind") != "schema_entity":
            continue
        cols = (it.get("attrs") or {}).get("columns") or []
        names = [c.get("name") if isinstance(c, dict) else str(c) for c in cols][:20]
        out.append(f"- {it.get('name')}: {', '.join(str(n) for n in names if n)}")
        if len(out) >= 30:
            break
    return "\n".join(out) or "(none)"


def build_context(project, graph, node, decision: str, tools, inventory, adj=None) -> NodeContext:
    from ..review.reanalyze import _regions
    from .casestore import parse_endpoint
    by_id = {n.id: n for n in graph.nodes}
    adj = adj if adj is not None else _neighbours(graph)
    eps = _candidate_endpoints(graph, node, adj, by_id)
    endpoints = [(e, parse_endpoint(e.name)) for e in eps]
    rules = [by_id[m] for m in sorted(adj.get(node.id, ())) if m in by_id and by_id[m].type == "business_rule"]
    if node.type == "business_rule":
        rules = [node]
    allowed = {node.id} | {e.id for e in eps} | {r.id for r in rules}
    code = ""
    if tools is not None:
        parts = []
        for n in [node, *eps[:5], *rules[:5]]:
            if n.evidence:
                parts.append(_regions(n, tools, 15))
        code = "\n".join(p for p in dict.fromkeys(parts) if p)[:MAX_CODE_CHARS]
    return NodeContext(node=node, decision=decision, endpoints=endpoints, rules=rules, allowed=allowed, code=code,
                       specs=_specs_text(tools, endpoints, inventory) if tools is not None else "(none)",
                       schemas=_schemas_text(inventory))


def render_prompt(ctx: NodeContext) -> tuple[str, str]:
    from ..analyze.module_analyzer import render
    system, user = load_prompt()
    n = ctx.node
    target = {"id": n.id, "type": n.type, "name": n.name, "module": n.module, "symbol": n.qualified_symbol,
              "responsibility": n.responsibility, "quirk": n.quirk, "suspected_bug": n.suspected_bug,
              "linked_rules": [{"id": r.id, "name": r.name, "responsibility": r.responsibility}
                               for r in ctx.rules if r.id != n.id]}
    eps = "\n".join(f"- {e.id}: {e.name}" for e, _ in ctx.endpoints) or "(none)"
    prompt = render(user, TARGET=_defang(json.dumps(target, indent=1)), DECISION=ctx.decision,
                    ENDPOINTS=_defang(eps), CODE=_defang(ctx.code or "(no code region)"),
                    SPECS=_defang(ctx.specs), SCHEMAS=_defang(ctx.schemas), TARGET_ID=n.id)
    return system, prompt


# ------------------------------------------------------------------ offline proposer
def offline_proposals(ctx: NodeContext) -> dict:
    """Deterministic proposals (no model). Same JSON shape as the model output."""
    from .casestore import fill_params
    n = ctx.node
    cases: list[dict] = []

    def http_case(ep, parsed, category, title, **req):
        return {"target": n.id, "covers": [n.id, ep.id], "kind": "http", "category": category, "title": title,
                "request": {"method": parsed[1], "path": req.pop("path", fill_params(parsed[2])), **req}}

    if n.type == "job":
        cases.append({"target": n.id, "covers": [n.id], "kind": "job", "category": "happy_path",
                      "title": "trigger", "request": {"job": n.name}})
    elif n.type == "endpoint":
        for ep, parsed in ctx.endpoints:
            if parsed[0] == "grpc":
                cases.append({"target": n.id, "covers": [n.id], "kind": "grpc", "category": "happy_path",
                              "title": "baseline", "request": {"service": parsed[1], "rpc": parsed[2],
                                                               "message": {}}})
                continue
            meth, path = parsed[1], parsed[2]
            write = meth in WRITE_METHODS
            cases.append(http_case(ep, parsed, "happy_path", "baseline", **({"body": {}} if write else {})))
            if fill_params(path) != path:
                cases.append(http_case(ep, parsed, "error_path", "unknown id", path=fill_params(path, "999999"),
                                       **({"body": {}} if write else {})))
            if write:
                cases.append(http_case(ep, parsed, "validation", "wrong body type", body=[]))
            if AUTH_HINT.search(ctx.code or ""):
                cases.append(http_case(ep, parsed, "auth", "invalid credential",
                                       headers={"Authorization": "Bearer invalid"},
                                       **({"body": {}} if write else {})))
    else:
        http_eps = [(e, p) for e, p in ctx.endpoints if p[0] == "http"]
        if http_eps:
            ep, parsed = http_eps[0]
            category = "boundary" if n.type == "business_rule" else "quirk"
            c = http_case(ep, parsed, category, f"exercise {n.id}",
                          **({"body": {}} if parsed[1] in WRITE_METHODS else {}))
            if n.type == "business_rule":
                m = _STATUS_IN_TEXT.search(f"{n.responsibility} {n.name}")
                if m:
                    c["hypothesis"] = {"status": int(m.group(1))}
            cases.append(c)
    return {"cases": cases[:MAX_CASES_PER_NODE], "untestable": []}


# ------------------------------------------------------------------ validation
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "case"


def _clean_path(path: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "://" in path:
        return None
    if _CTRL.search(path) or "\\" in path or " " in path:
        return None
    if any(s in ("..", ".") for s in path.split("?", 1)[0].split("/")):
        return None
    return path


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop headers with control characters; redact values that look like secrets (alone or by header name)."""
    from ..ingest.secrets import redact_string
    out = {}
    for k, v in headers.items():
        k, v = str(k), str(v)
        if _CTRL.search(k + v):
            continue
        red = redact_string(v)
        if red == v and redact_string(f"{k}={v}") != f"{k}={v}":
            red = "[REDACTED:header]"
        out[k] = red
    return out


def validate_proposal(raw: Any, ctx: NodeContext, known_stubs: set[str]) -> tuple[dict | None, str, int]:
    """(validated case parts, drop reason, ignored expectation count)."""
    from ..ingest.secrets import redact_obj
    from .casestore import route_regex
    from .models import GrpcRequest, HttpRequest, JobRequest
    if not isinstance(raw, dict):
        return None, "proposal is not an object", 0
    raw = dict(raw)
    ignored = sum(1 for k in ("expect", "expected", "expectation") if raw.pop(k, None) is not None)
    try:
        p = Proposal.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        return None, f"invalid proposal ({'.'.join(map(str, err['loc']))}: {err['msg']})", ignored
    n = ctx.node
    covers = [c for c in dict.fromkeys([n.id, *p.covers]) if c in ctx.allowed]
    try:
        if p.kind == "http":
            req = HttpRequest.model_validate(p.request)
            method = req.method.upper()
            path = _clean_path(req.path)
            if path is None:
                return None, f"unsafe or non-relative path {req.path!r}", ignored
            hit = next((e for e, parsed in ctx.endpoints if parsed[0] == "http" and parsed[1] == method
                        and route_regex(parsed[2]).match(path.split("?", 1)[0])), None)
            if hit is None:
                return None, f"{method} {path} does not match a listed endpoint", ignored
            covers = list(dict.fromkeys([*covers, hit.id]))
            req = HttpRequest(method=method, path=path, headers=_safe_headers(req.headers),
                              query=redact_obj(req.query), body=redact_obj(req.body))
        elif p.kind == "job":
            req = JobRequest.model_validate(p.request)
            if n.type != "job" or req.job != n.name:
                return None, "job requests are only allowed for job targets (with the job's name)", ignored
            req = JobRequest(job=req.job, args=[str(a) for a in redact_obj(req.args)])
        else:
            req = GrpcRequest.model_validate(p.request)
            hit = next((e for e, parsed in ctx.endpoints if parsed[0] == "grpc" and parsed[1] == req.service
                        and parsed[2] == req.rpc), None)
            if hit is None:
                return None, f"rpc {req.service}/{req.rpc} does not match a listed endpoint", ignored
            covers = list(dict.fromkeys([*covers, hit.id]))
            req = GrpcRequest(service=req.service, rpc=req.rpc, metadata=redact_obj(req.metadata),
                              message=redact_obj(req.message))
    except ValidationError as e:
        return None, f"invalid request ({e.errors()[0]['msg']})", ignored
    rule_ids = [c for c in covers if any(r.id == c for r in ctx.rules)]
    hypothesis = p.hypothesis if (p.hypothesis and rule_ids and (p.hypothesis.status or p.hypothesis.status_class
                                                                 or p.hypothesis.body_contains)) else None
    specified = p.specified
    if specified is not None and ctx.decision != "fix":
        ignored += 1
        specified = None
    return {"category": p.category, "title": p.title, "kind": p.kind, "request": req, "covers": covers,
            "hypothesis": hypothesis, "rule_ids": rule_ids, "specified": specified,
            "stubs": [s for s in p.stubs if s in known_stubs]}, "", ignored


# ------------------------------------------------------------------ generation
def generate_cases(project, llm=None, *, offline: bool = False, node_ids: list[str] | None = None,
                   pin: bool = False, log: Callable[[str], None] | None = None):
    """Create characterization cases for the in-scope nodes. Returns a casestore.TestgenReport."""
    from ..audit import AuditLog
    from ..llm.base import BudgetExceeded, parse_json
    from ..sandbox.tools import ReadOnlyTools
    from .casestore import (HypothesesFile, RuleHypothesis, TestgenReport, char_file, contract_graph, load_cases,
                            load_hypotheses, save_case, save_hypotheses, scope_nodes, slice_of)
    from .contract import require_contract
    from .harness_runner.stubserver import load_stubs
    from .ids import allocator
    from .models import CharacterizationCase, Expectation, Provenance
    from .paths import Phase2Layout
    from .state import atomic_write_json, now_iso

    log = log or (lambda _m: None)
    if not offline and llm is None:
        raise ValueError("an LLM client is required unless offline=True")
    contract = require_contract(project, pin=pin)
    graph = contract_graph(project, contract)
    scope = scope_nodes(project, graph)
    if node_ids:
        known = {n.id for n in scope}
        bad = [i for i in node_ids if i not in known]
        if bad:
            raise ValueError(f"not in-scope endpoint/job/business_rule/quirk node(s): {', '.join(bad)}")
        scope = [n for n in scope if n.id in set(node_ids)]
    decisions = {d.item_id: d.decision for d in contract.decisions}
    deferred = [(f.id, set(f.item_ids)) for f in contract.deferred_findings]
    slices = slice_of(project)
    lay = Phase2Layout(project)
    lay.ensure(lay.characterization)
    known_stubs = {s.ref for s in load_stubs(lay.stubs_dir)}
    try:
        tools = ReadOnlyTools(project.code) if Path(project.code).is_dir() else None
    except (OSError, ValueError):
        tools = None
    inventory = _inventory(project)
    adj = _neighbours(graph)
    system = load_prompt()[0]
    model_name = "offline" if offline else ""

    report = TestgenReport(created=now_iso(), prompt_version=PROMPT_VERSION, offline=offline,
                           nodes=[n.id for n in scope])
    proposals: list[tuple[Any, NodeContext, dict]] = []
    for node in scope:
        ctx = build_context(project, graph, node, decisions.get(node.id, "preserve"), tools, inventory, adj)
        if offline:
            data = offline_proposals(ctx)
        else:
            system, prompt = render_prompt(ctx)
            try:
                resp = llm.complete(task="test_gen", system=system, prompt=prompt, json_mode=True,
                                    prompt_version=PROMPT_VERSION)
            except BudgetExceeded as e:
                report.stopped = str(e)
                log(f"stopped: {e}")
                break
            model_name = resp.model or model_name
            try:
                data = parse_json(resp.text)
            except (ValueError, json.JSONDecodeError):
                report.dropped.append(f"{node.id}: model output is not JSON")
                continue
        if not isinstance(data, dict):
            report.dropped.append(f"{node.id}: model output is not an object")
            continue
        for u in (data.get("untestable") or [])[:20]:
            if isinstance(u, dict) and u.get("node_id") in ctx.allowed and isinstance(u.get("reason"), str):
                report.untestable_suggestions[u["node_id"]] = _defang(u["reason"])[:300]
        raw_cases = data.get("cases") if isinstance(data.get("cases"), list) else []
        if len(raw_cases) > MAX_CASES_PER_NODE:
            report.dropped.append(f"{node.id}: {len(raw_cases) - MAX_CASES_PER_NODE} proposal(s) over the limit")
        seen = set()
        for raw in raw_cases[:MAX_CASES_PER_NODE]:
            parts, why, ign = validate_proposal(raw, ctx, known_stubs)
            report.ignored_expectations += ign
            if parts is None:
                report.dropped.append(f"{node.id}: {why}")
                continue
            key = f"{node.id}/{parts['category']}/{_slug(parts['title'])}"
            if key in seen:
                continue
            seen.add(key)
            proposals.append((node, ctx, {**parts, "key": key}))

    ids = allocator(project).allocate_many("CT", [p[2]["key"] for p in proposals]) if proposals else {}
    existing = {c.id: c for c in load_cases(project)}
    hyp = load_hypotheses(project)
    hyp_changed = False
    for node, ctx, parts in proposals:
        cid = ids[parts["key"]]
        covers = parts["covers"]
        sl = slices.get(node.id) or next((slices[c] for c in covers if c in slices), None)
        provisional = any(items & set(covers) for _, items in deferred)
        if cid in existing:
            case = existing[cid]
            tags = list(case.tags)
            if provisional and "provisional" not in tags:
                tags.append("provisional")
            upd = case.model_copy(update={"tags": tags, "slice": sl or case.slice})
            if upd != case:
                save_case(project, CharacterizationCase.model_validate(upd.model_dump()))
            report.kept_cases.append(cid)
            continue
        tags = [parts["category"]]
        if node.quirk:
            tags.append("quirk")
        if node.suspected_bug:
            tags.append("suspected_bug")
        if provisional:
            tags.append("provisional")
        fix = ctx.decision == "fix"
        if fix:
            d = parts["specified"] or SpecDraft()
            expect = Expectation(source="specified", status=d.status, headers=d.headers, body=d.body)
            tags.append("needs_approval")
        else:
            expect = Expectation()
        case = CharacterizationCase(
            id=cid, slice=sl, covers=covers, kind=parts["kind"], request=parts["request"], expect=expect,
            decision="fix" if fix else "preserve", decision_ref=node.id if fix else "",
            setup={"stubs": parts["stubs"]},
            provenance=Provenance(generator="test_gen", prompt_version=PROMPT_VERSION, model=model_name,
                                  created=now_iso()),
            tags=list(dict.fromkeys(tags)))
        save_case(project, case)
        existing[cid] = case
        report.created_cases.append(cid)
        if parts["hypothesis"] is not None and cid not in hyp.hypotheses:
            h = parts["hypothesis"]
            hyp.hypotheses[cid] = RuleHypothesis(case_id=cid, rule_ids=parts["rule_ids"], status=h.status,
                                                 status_class=h.status_class, body_contains=h.body_contains,
                                                 source="offline" if offline else "model")
            hyp_changed = True
    if hyp_changed:
        save_hypotheses(project, HypothesesFile.model_validate(hyp.model_dump()))

    for c in existing.values():
        if c.expect.source == "specified" and not c.expect.approved_by:
            report.needs_approval.append(c.id)
        for fid, items in deferred:
            if items & set(c.covers):
                report.blockers.setdefault(fid, []).append(c.id)
                if c.id not in report.provisional:
                    report.provisional.append(c.id)
    report.needs_approval.sort()
    report.provisional.sort()
    atomic_write_json(char_file(project, "testgen_report.json"), report)
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "testgen", "tests_generate", offline=offline, prompt_version=PROMPT_VERSION, nodes=len(report.nodes),
        created=len(report.created_cases), kept=len(report.kept_cases), dropped=len(report.dropped),
        ignored_expectations=report.ignored_expectations, stopped=report.stopped[:200])
    return report


_UNSET = object()


def approve_spec(project, case_id: str, by: str, *, status: int | None = None, body: Any = _UNSET,
                 headers: dict[str, str] | None = None, pin: bool = False):
    """Approve (optionally after editing) the `specified` expectation of a fix-decision case."""
    from ..audit import AuditLog
    from .casestore import find_case, save_case
    from .contract import require_contract
    from .models import CharacterizationCase
    by = (by or "").strip()
    if not by:
        raise ValueError("--by is required")
    require_contract(project, pin=pin)
    case = find_case(project, case_id)
    if case is None:
        raise ValueError(f"no case {case_id}")
    if case.decision != "fix" or case.expect.source != "specified":
        raise ValueError(f"{case_id} has no specified expectation (only fix-decision cases do)")
    upd: dict[str, Any] = {"approved_by": by}
    if status is not None:
        upd["status"] = status
    if body is not _UNSET:
        upd["body"] = body
    if headers:
        upd["headers"] = {**case.expect.headers, **headers}
    expect = case.expect.model_copy(update=upd)
    if expect.status is None:
        raise ValueError(f"{case_id}: the specified expectation is empty; pass --status (and --body-json)")
    tags = [t for t in case.tags if t != "needs_approval"]
    new = CharacterizationCase.model_validate({**case.model_dump(), "expect": expect.model_dump(), "tags": tags})
    save_case(project, new)
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "testgen", "approve_spec", case=case_id, by=by, decision_ref=case.decision_ref, status=expect.status)
    return new


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _make_llm(project, config_path, max_usd):
    import os
    from ..audit import AuditLog
    from ..cli import _budget, _load_cfg
    from ..llm.litellm_client import LiteLLMClient
    from .ledger import budget_for, project_budget_usd
    if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")):
        raise click.ClickException("No LLM API key found. Add it to the .env file (see .env.example), or use --offline.")
    budget = budget_for(project, project_budget_usd(project), max_usd=_budget(max_usd))
    return LiteLLMClient(_load_cfg(config_path), budget=budget, cache_dir=Path(project.root) / "llm_cache",
                         audit=AuditLog(Path(project.root) / "audit" / "audit.jsonl"), agent="testgen")


def register(groups) -> None:
    @groups.tests.command("generate")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--node", "nodes", multiple=True, help="Only these graph node ids (repeatable).")
    @click.option("--max-usd", type=float, default=None, help="Cap for this run (also capped by the project budget).")
    @click.option("--offline", is_flag=True, help="Deterministic proposer, no model calls.")
    @click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None,
                  help="LLM config.json")
    @click.option("--pin-contract", "pin", is_flag=True, help="Use the approved snapshot even if re-analysed later.")
    def generate_cmd(project: Path, nodes: tuple[str, ...], max_usd: float | None, offline: bool,
                     config_path: Path | None, pin: bool) -> None:
        """Propose characterization test inputs per endpoint/job/rule/quirk (expected outputs are recorded later)."""
        from ..llm.base import BudgetExceeded
        from .contract import ContractError
        p = _project(project)
        try:
            llm = None if offline else _make_llm(p, config_path, max_usd)
            rep = generate_cases(p, llm, offline=offline, node_ids=list(nodes) or None, pin=pin, log=click.echo)
        except (ContractError, ValueError, BudgetExceeded) as e:
            raise click.ClickException(str(e))
        click.echo(f"cases: {len(rep.created_cases)} new, {len(rep.kept_cases)} kept, {len(rep.dropped)} dropped "
                   f"({len(rep.nodes)} node(s))")
        for d in rep.dropped[:20]:
            click.echo(f"  dropped: {d}")
        if rep.ignored_expectations:
            click.echo(f"  ignored {rep.ignored_expectations} model-written expectation(s) (outputs are recorded)")
        for cid in rep.needs_approval:
            click.echo(f"  needs approval: {cid} (fix decision; `tests approve-spec {project} {cid} --by NAME`)")
        for fid, cids in sorted(rep.blockers.items()):
            click.echo(f"  blocker {fid}: provisional case(s) {', '.join(cids)}")
        if rep.stopped:
            click.echo(f"  stopped early: {rep.stopped}")

    @groups.tests.command("approve-spec")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("case_id")
    @click.option("--by", required=True, help="Approver name.")
    @click.option("--status", type=int, default=None, help="Set the specified status before approving.")
    @click.option("--body-json", default=None, help="Set the specified body (JSON) before approving.")
    @click.option("--header", "headers", multiple=True, help="NAME=VALUE, set a specified header (repeatable).")
    @click.option("--pin-contract", "pin", is_flag=True)
    def approve_spec_cmd(project: Path, case_id: str, by: str, status: int | None, body_json: str | None,
                         headers: tuple[str, ...], pin: bool) -> None:
        """Approve the specified expectation of a fix-decision case."""
        from .contract import ContractError
        body: Any = _UNSET
        if body_json is not None:
            try:
                body = json.loads(body_json)
            except ValueError as e:
                raise click.ClickException(f"--body-json is not valid JSON: {e}")
        hdrs = {}
        for h in headers:
            k, sep, v = h.partition("=")
            if not sep or not k.strip():
                raise click.ClickException(f"--header must be NAME=VALUE, got {h!r}")
            hdrs[k.strip()] = v
        try:
            case = approve_spec(_project(project), case_id, by, status=status, body=body, headers=hdrs, pin=pin)
        except (ContractError, ValueError) as e:
            raise click.ClickException(str(e))
        click.echo(f"approved: {case.id} specified status {case.expect.status} by {case.expect.approved_by}")
