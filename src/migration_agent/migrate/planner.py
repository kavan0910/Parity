"""Migration planner (docs/phase2-plan.md section 3, milestone M4): approved graph -> ordered slices.

    migration-agent plan PROJECT [--dry-run] [--offline] [--include-dead-code] [--max-slice-tokens N]
                                 [--name-with-model] [--pin-contract] [--accept-unmapped]

Deterministic algorithm (`build_plan`, pure apart from reading code/ for token estimates):
  1. Directed "needs" graph over the in-scope nodes of the APPROVED snapshot named in the contract (never latest):
     calls / depends_on / reads / writes: source needs target; configures: the non-config end needs the config end;
     implements: both directions (implementer and implemented rule/endpoint always share a slice). Endpoints, jobs
     and business rules with none of those edges are bound to the components defined in the same evidence file
     (reported as inferred links). publishes/consumes do not order slices. dead_code is excluded unless opted in.
  2. Strongly connected components are condensed (iterative Tarjan); cycles above LARGE_CYCLE nodes are flagged.
  3. Foundation units: data entities and data access, config, build/external dependencies, cross-cutting
     (auth/error/logging: the render.py _XCUT pattern on name/module/symbol), plus everything they need (promoted).
     Entry units: endpoints (grouped by resource path prefix), jobs, consumers (components with `consumes` edges).
     Each entry group takes the closure of components only it reaches; components reached by several groups become
     shared foundation slices (leaf-to-root); components reached by none form the "Application bootstrap" entry.
  4. Slice-level cycles are merged, oversized slices are split along file and top-level symbol boundaries
     (analyze/chunker token estimates over code/ read through sandbox ReadOnlyTools), then slices are ordered
     topologically: foundation (build, config, cross-cutting, data, shared) -> entry slices -> bootstrap. Ties: fewest
     fit risks, highest mean node confidence, then a stable key.
  5. Invariant: every in-scope node lands in exactly one slice (PlanInvariantError otherwise).

`plan_project` adds: contract gate, deferred-finding and fit-risk blockers, per-slice USD estimates (`codegen` tier),
the optional model naming pass (task `plan`, prompts/plan.md; names / merge suggestions only, validated), stable
SLICE ids keyed by the sorted node ids, and the outputs:
  plan/migration_plan.json, plan/versions/vNNN.json (immutable), plan/migration_plan.md,
  plan/planner_report.json (PlannerReport, this module), slices/SLICE-NNNN/state.json (planned, if missing).
"""
from __future__ import annotations

import hashlib
import heapq
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import click
from pydantic import BaseModel, ConfigDict, Field

from ..graph.models import Graph, Node

PROMPT_FILE = Path(__file__).parent / "prompts" / "plan.md"
PROMPT_VERSION = "plan-v1"
DEFAULT_MAX_SLICE_TOKENS = 60_000
LARGE_CYCLE = 10                     # SCCs with more nodes than this are flagged in the report
SPEC_OVERHEAD_TOKENS = 2_000         # slice spec, rules and decisions in the codegen context pack
SPEC_TOKENS_PER_NODE = 150
MIN_OUTPUT_TOKENS = 500

FORWARD_EDGES = frozenset({"calls", "depends_on", "reads", "writes"})
CONFIG_EDGES = frozenset({"configures"})
BIND_EDGES = frozenset({"implements"})
ORDER_EDGE_TYPES = FORWARD_EDGES | CONFIG_EDGES | BIND_EDGES

# from render.py _XCUT (same vocabulary), applied to name/module/symbol only (responsibility text is too noisy)
_XCUT = re.compile(r"\b(auth\w*|login|session|token|permission\w*|error\w*|exception\w*|logg\w*|middleware|"
                   r"validat\w*|retry|audit)\b", re.I)
_DATA = re.compile(r"\b(models?|repositor\w*|dao|daos|db|database|schemas?|entit\w*|persist\w*|migrations?|orm)\b",
                   re.I)
_CONFIG = re.compile(r"\b(config\w*|settings?|env|properties)\b", re.I)
_NODE_ID = re.compile(r"\b(COMP|EP|ENT|EXT|RULE|CFG|JOB|INT|DEAD)-\d{4,}\b")
_PATH_SKIP = re.compile(r"^(api|rest|v\d+(\.\d+)*|)$", re.I)

FOUNDATION_CATEGORIES = ("platform", "config", "xcut", "data", "shared")
CATEGORY_RANK = {"platform": 0, "config": 1, "xcut": 2, "data": 3, "shared": 4, "entry": 5, "bootstrap": 6}
CATEGORY_NAME = {
    "platform": "Foundation: build and external dependencies",
    "config": "Foundation: configuration",
    "xcut": "Foundation: cross-cutting (auth, errors, logging)",
    "data": "Foundation: data entities and data access",
}
_CONF_SCORE = {"none": 0, "low": 1, "medium": 2, "high": 3}


class PlanError(RuntimeError):
    pass


class PlanInvariantError(PlanError):
    """A node is missing from the plan or in more than one slice."""


# ------------------------------------------------------------------ extra models (plan/planner_report.json)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SliceDetail(_Strict):
    slice_id: str
    key: str                                  # stable identity: sha256 of the sorted node ids (ids.json key)
    group: str                                # e.g. entry:/api/orders, foundation:data, shared:src/utils
    category: str                             # platform|config|xcut|data|shared|entry|bootstrap
    est_tokens: int = 0                       # legacy code regions (chunker estimate)
    confidence: float = 0.0                   # mean node confidence score (0 none .. 3 high)
    split_part: int | None = None             # 1-based part number when an oversized slice was split
    split_total: int | None = None
    package: str = ""                         # model naming pass suggestion
    flags: list[str] = Field(default_factory=list)   # merged_cycle, large_cycle, split, oversized, split_back_edges


class MergeSuggestion(_Strict):
    slice_ids: list[str]
    reason: str = ""


class PlannerReport(_Strict):
    schema_version: int = 1
    plan_version: int = 0
    dry_run: bool = False
    graph_version: int = 0
    graph_snapshot: str = ""
    max_slice_tokens: int = DEFAULT_MAX_SLICE_TOKENS
    include_dead_code: bool = False
    details: list[SliceDetail] = Field(default_factory=list)
    inferred_links: list[list[str]] = Field(default_factory=list)   # [from, to] same-file bindings
    merge_suggestions: list[MergeSuggestion] = Field(default_factory=list)
    named_by_model: bool = False
    retired_slice_ids: list[str] = Field(default_factory=list)       # in the previous plan, not in this one
    plan_blockers: list[str] = Field(default_factory=list)           # deferred findings without items etc.
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ graph algorithms
def strongly_connected(nodes: Iterable[str], adj: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Iterative Tarjan. Returns SCCs (each sorted) in reverse topological order (dependencies first for
    "needs" edges). Deterministic: roots and neighbours are visited in sorted order."""
    universe = set(nodes)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0

    def succ(v: str):
        return iter(sorted(w for w in adj.get(v, ()) if w in universe))

    for root in sorted(universe):
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, succ(root))]
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, succ(w)))
                    pushed = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if pushed:
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def topo_order(items: Iterable[str], needs: Mapping[str, Iterable[str]], priority: Callable[[str], Any]) -> list[str]:
    """Kahn's algorithm on an acyclic "needs" relation (dependencies first); ready items by `priority`."""
    items = list(items)
    universe = set(items)
    pending = {i: {d for d in needs.get(i, ()) if d in universe and d != i} for i in items}
    users: dict[str, set[str]] = defaultdict(set)
    for i, ds in pending.items():
        for d in ds:
            users[d].add(i)
    heap = [(priority(i), i) for i in items if not pending[i]]
    heapq.heapify(heap)
    out: list[str] = []
    while heap:
        _, i = heapq.heappop(heap)
        out.append(i)
        for u in sorted(users[i]):
            pending[u].discard(i)
            if not pending[u]:
                heapq.heappush(heap, (priority(u), u))
    if len(out) != len(items):
        raise PlanError(f"cycle among {sorted(universe - set(out))[:10]} (internal: should have been condensed)")
    return out


def needs_graph(graph: Graph, in_scope: set[str]) -> tuple[dict[str, set[str]], list[list[str]]]:
    """node -> nodes it needs (migrated no later than itself). Also returns inferred same-file bindings."""
    by_id = {n.id: n for n in graph.nodes}
    needs: dict[str, set[str]] = {n: set() for n in in_scope}
    touched: set[str] = set()
    for e in sorted(graph.edges, key=lambda e: e.id):
        s, t = e.source, e.target
        if s not in in_scope or t not in in_scope or s == t or e.type not in ORDER_EDGE_TYPES:
            continue
        touched.update((s, t))
        if e.type in FORWARD_EDGES:
            needs[s].add(t)
        elif e.type in CONFIG_EDGES:
            if by_id[s].type == "config" and by_id[t].type != "config":
                needs[t].add(s)
            else:
                needs[s].add(t)
        else:  # implements: bind both ways
            needs[s].add(t)
            needs[t].add(s)
    inferred: list[list[str]] = []
    comps_by_file: dict[str, list[str]] = defaultdict(list)
    for n in sorted(graph.nodes, key=lambda n: n.id):
        if n.id in in_scope and n.type in ("component", "dead_code"):
            for f in sorted({ev.file for ev in n.evidence}):
                comps_by_file[f].append(n.id)
    for n in sorted(graph.nodes, key=lambda n: n.id):
        if n.id not in in_scope or n.id in touched or n.type not in ("endpoint", "job", "business_rule"):
            continue
        for f in sorted({ev.file for ev in n.evidence}):
            for c in comps_by_file.get(f, []):
                needs[n.id].add(c)
                needs[c].add(n.id)
                inferred.append([n.id, c])
    return needs, inferred


# ------------------------------------------------------------------ classification
def _text(n: Node) -> str:
    return f"{n.name} {n.module} {n.qualified_symbol}"


def node_category(n: Node, data_access: set[str]) -> str | None:
    """Foundation category of a node, 'entry' for entry points, None for plain components."""
    if n.type in ("endpoint", "job"):
        return "entry"
    if n.type == "data_entity":
        return "data"
    if n.type == "config":
        return "config"
    if n.type == "external_dependency":
        return "platform"
    if n.type in ("component", "integration", "business_rule"):
        if n.id in data_access or (n.type != "business_rule" and _DATA.search(_text(n))):
            return "data"
        if n.type != "business_rule" and _CONFIG.search(_text(n)):
            return "config"
        if _XCUT.search(_text(n)):
            return "xcut"
    return None


def resource_key(n: Node) -> str:
    """Endpoint group: the path up to its first resource segment, e.g. 'GET /api/orders/:id' -> '/api/orders'."""
    path = next((tok for tok in n.name.split() if tok.startswith("/")), "")
    if not path:
        return f"endpoint:{n.name}"
    path = path.split("?", 1)[0]
    kept: list[str] = []
    for seg in path.strip("/").split("/"):
        if seg.startswith((":", "{", "<", "*")):
            break
        kept.append(seg)
        if not _PATH_SKIP.match(seg):
            break
    return "/" + "/".join(kept)


def _dir(n: Node) -> str:
    if not n.evidence:
        return n.module or "(root)"
    f = n.evidence[0].file
    return f.rsplit("/", 1)[0] if "/" in f else "(root)"


# ------------------------------------------------------------------ token sizing
class Sizer:
    """Token estimates (analyze/chunker cost model) of evidence regions, read through ReadOnlyTools over code/."""

    def __init__(self, code_dir: Path | None = None, ast=None):
        from ..sandbox.tools import ReadOnlyTools
        self.tools = None
        if code_dir is not None and Path(code_dir).is_dir():
            self.tools = ReadOnlyTools(Path(code_dir))
        self.facts = {f.path: f for f in ast.files} if ast is not None else {}
        self._lines: dict[str, list[str] | None] = {}
        self.unreadable: set[str] = set()

    def lines(self, rel: str) -> list[str] | None:
        if rel in self._lines:
            return self._lines[rel]
        out: list[str] | None = None
        if self.tools is not None:
            from ..sandbox.tools import SandboxViolation
            try:
                out, start = [], 1
                while True:
                    r = self.tools.read_file(rel, start, start + self.tools.MAX_READ_LINES - 1)
                    if r.end >= r.start:
                        out.extend(r.content.split("\n"))
                    if r.end < r.start or r.end >= r.total_lines:
                        break
                    start = r.end + 1
            except (SandboxViolation, OSError, ValueError):
                out = None
        if out is None:
            self.unreadable.add(rel)
        self._lines[rel] = out
        return out

    @staticmethod
    def node_regions(n: Node) -> dict[str, list[tuple[int, int]] | None]:
        """file -> line ranges (None = whole file). Components claim their defining files whole."""
        regs: dict[str, list[tuple[int, int]] | None] = {}
        for ev in n.evidence:
            whole = n.type in ("component", "dead_code") or ev.line_start is None
            if whole:
                regs[ev.file] = None
            elif regs.get(ev.file, []) is not None:
                regs.setdefault(ev.file, []).append((ev.line_start, ev.line_end or ev.line_start))
        return regs

    @staticmethod
    def merge_regions(nodes: Iterable[Node]) -> dict[str, list[tuple[int, int]] | None]:
        out: dict[str, list[tuple[int, int]] | None] = {}
        for n in nodes:
            for f, rr in Sizer.node_regions(n).items():
                if rr is None or (f in out and out[f] is None):
                    out[f] = None
                else:
                    out.setdefault(f, []).extend(rr)
        return out

    def cost(self, regions: Mapping[str, list[tuple[int, int]] | None]) -> int:
        from ..analyze.chunker import _line_costs, _seg_cost
        total = 0
        for f in sorted(regions):
            lines = self.lines(f)
            if not lines:
                continue
            n = len(lines)
            pre = _line_costs(lines)
            rr = regions[f]
            if rr is None:
                total += _seg_cost(f, 1, n, pre)
                continue
            merged: list[list[int]] = []
            for a, b in sorted((max(1, a), min(n, b)) for a, b in rr):
                if a > b:
                    continue
                if merged and a <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            total += sum(_seg_cost(f, a, b, pre) for a, b in merged)
        return total

    def split_file(self, rel: str, budget: int) -> list[tuple[int, int, int]]:
        """(start, end, tokens) segments along top-level symbol boundaries (chunker._split_file)."""
        from ..analyze.chunker import _line_costs, _split_file
        from ..extract.treesitter import FileFacts
        lines = self.lines(rel)
        if not lines:
            return []
        ff = self.facts.get(rel) or FileFacts(path=rel, language="", parse_status="fallback")
        return [(s.a, s.b, s.cost) for s in _split_file(ff, lines, _line_costs(lines), budget)]


# ------------------------------------------------------------------ drafts
@dataclass
class DraftSlice:
    group: str
    kind: str                 # foundation | entry
    category: str
    name: str
    nodes: set[str]
    flags: list[str] = field(default_factory=list)
    split_of: str | None = None
    part: int | None = None
    parts: int | None = None
    est_tokens: int = 0
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    depends_on: list[int] = field(default_factory=list)   # indices into PlanDraft.slices (ordered)

    @property
    def key(self) -> str:
        return slice_key(self.nodes)


def slice_key(node_ids: Iterable[str]) -> str:
    return "nodes:" + hashlib.sha256("\n".join(sorted(node_ids)).encode()).hexdigest()[:32]


@dataclass
class PlanDraft:
    slices: list[DraftSlice]                      # in migration order
    excluded: dict[str, str]
    in_scope: set[str]
    inferred_links: list[list[str]]
    warnings: list[str]


def check_invariant(in_scope: Iterable[str], slices: Iterable[Iterable[str]]) -> None:
    """Every in-scope node in exactly one slice, and nothing else (raises PlanInvariantError)."""
    scope = set(in_scope)
    seen: dict[str, int] = {}
    dup: set[str] = set()
    for i, nodes in enumerate(slices):
        for n in nodes:
            if n in seen:
                dup.add(n)
            seen[n] = i
    missing = scope - seen.keys()
    extra = seen.keys() - scope
    if dup or missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)[:10]}")
        if dup:
            parts.append(f"in several slices {sorted(dup)[:10]}")
        if extra:
            parts.append(f"not in scope {sorted(extra)[:10]}")
        raise PlanInvariantError("plan invariant violated: " + "; ".join(parts))


def build_plan(graph: Graph, *, include_dead_code: bool = False, max_slice_tokens: int = DEFAULT_MAX_SLICE_TOKENS,
               sizer: Sizer | None = None, node_risks: Mapping[str, Iterable[str]] | None = None) -> PlanDraft:
    """Deterministic slice construction and ordering (see module docstring). Raises PlanInvariantError."""
    if max_slice_tokens < 1:
        raise PlanError("max_slice_tokens must be >= 1")
    sizer = sizer or Sizer()
    node_risks = {k: set(v) for k, v in (node_risks or {}).items()}
    by_id = {n.id: n for n in graph.nodes}
    warnings: list[str] = []
    excluded = {n.id: "dead_code: not migrated (pass --include-dead-code to migrate it)"
                for n in graph.nodes if n.type == "dead_code" and not include_dead_code}
    in_scope = {n.id for n in graph.nodes} - excluded.keys()
    needs, inferred = needs_graph(graph, in_scope)

    # 1-2: condense
    sccs = strongly_connected(in_scope, needs)
    unit_of = {n: i for i, comp in enumerate(sccs) for n in comp}
    unit_needs: dict[int, set[int]] = defaultdict(set)
    for n, ds in needs.items():
        for d in ds:
            if unit_of[d] != unit_of[n]:
                unit_needs[unit_of[n]].add(unit_of[d])
    large = {i for i, comp in enumerate(sccs) if len(comp) > LARGE_CYCLE}
    for i in sorted(large):
        warnings.append(f"large dependency cycle of {len(sccs[i])} nodes condensed into one unit "
                        f"({', '.join(sccs[i][:5])}...)")

    # 3: classify units
    entities = {n.id for n in graph.nodes if n.type == "data_entity"}
    data_access = {e.source for e in graph.edges if e.type in ("reads", "writes") and e.target in entities}
    consumers = {e.source for e in graph.edges if e.type == "consumes" and e.source in in_scope
                 and by_id[e.source].type in ("component", "integration")}
    unit_cat: dict[int, str | None] = {}
    for i, comp in enumerate(sccs):
        cats = {node_category(by_id[n], data_access) for n in comp}
        if "entry" in cats or any(n in consumers for n in comp):
            unit_cat[i] = "entry"
        else:
            unit_cat[i] = next((c for c in ("data", "config", "xcut", "platform") if c in cats), None)
    # promote everything a foundation unit needs
    foundation = {i for i, c in unit_cat.items() if c not in (None, "entry")}
    stack = sorted(foundation)
    while stack:
        u = stack.pop()
        for d in sorted(unit_needs[u]):
            if d not in foundation:
                if unit_cat[d] == "entry":
                    warnings.append(f"entry point(s) {', '.join(sccs[d])} are needed by foundation code and are "
                                    "migrated as foundation")
                unit_cat[d] = unit_cat[d] if unit_cat[d] not in (None, "entry") else "shared"
                foundation.add(d)
                stack.append(d)

    # entry groups (union-find over keys inside one unit)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    unit_key: dict[int, str] = {}
    for i, comp in enumerate(sccs):
        if unit_cat[i] != "entry" or i in foundation:
            continue
        keys = []
        for n in comp:
            node = by_id[n]
            if node.type == "endpoint":
                keys.append("entry:" + resource_key(node))
            elif node.type == "job":
                keys.append(f"job:{node.id}")
            elif n in consumers:
                keys.append(f"consumer:{node.id}")
        keys = sorted(set(keys))
        for k in keys[1:]:
            parent[find(k)] = find(keys[0])
        unit_key[i] = keys[0]
    group_of_unit = {i: find(k) for i, k in unit_key.items()}

    # components reached by entry groups (traversal stops at entry and foundation units)
    reached: dict[int, set[str]] = defaultdict(set)
    for i, g in sorted(group_of_unit.items()):
        stack = [i]
        seen = {i}
        while stack:
            u = stack.pop()
            for d in unit_needs[u]:
                if d in seen or d in foundation or d in group_of_unit:
                    continue
                seen.add(d)
                reached[d].add(g)
                stack.append(d)
    unit_group: dict[int, tuple[str, str, str]] = {}   # unit -> (group, kind, category)
    for i, comp in enumerate(sccs):
        if i in foundation:
            cat = unit_cat[i] or "shared"
            grp = f"foundation:{cat}" if cat != "shared" else f"shared:{_dir(by_id[comp[0]])}"
            unit_group[i] = (grp, "foundation", cat)
        elif i in group_of_unit:
            unit_group[i] = (group_of_unit[i], "entry", "entry")
        elif len(reached[i]) == 1:
            unit_group[i] = (next(iter(reached[i])), "entry", "entry")
        elif len(reached[i]) > 1:
            unit_group[i] = (f"shared:{_dir(by_id[comp[0]])}", "foundation", "shared")
        else:
            unit_group[i] = ("bootstrap", "entry", "bootstrap")

    groups: dict[str, DraftSlice] = {}
    for i, comp in enumerate(sccs):
        grp, kind, cat = unit_group[i]
        d = groups.get(grp)
        if d is None:
            d = groups[grp] = DraftSlice(group=grp, kind=kind, category=cat, name=_group_name(grp, cat, by_id, comp),
                                         nodes=set())
        d.nodes.update(comp)
        if i in large and "large_cycle" not in d.flags:
            d.flags.append("large_cycle")

    # 4a: merge slice-level cycles
    drafts = _merge_cycles(list(groups.values()), needs)
    # 4b: split oversized slices
    final: list[DraftSlice] = []
    for d in drafts:
        final.extend(_split(d, by_id, needs, sizer, max_slice_tokens, warnings))
    for d in final:
        if d.split_of is None:
            d.est_tokens = sizer.cost(Sizer.merge_regions(by_id[n] for n in d.nodes))
        d.risks = sorted({r for n in d.nodes for r in node_risks.get(n, ())})
        d.confidence = round(sum(_CONF_SCORE[by_id[n].confidence] for n in d.nodes) / max(1, len(d.nodes)), 3)
    if sizer.unreadable:
        warnings.append(f"{len(sizer.unreadable)} evidence file(s) not readable under code/ (estimated as 0 tokens): "
                        + ", ".join(sorted(sizer.unreadable)[:5]))

    # 4c: order
    owner = {n: i for i, d in enumerate(final) for n in d.nodes}
    deps: dict[str, set[str]] = {str(i): set() for i in range(len(final))}
    back = set()
    for n, ds in needs.items():
        a = owner[n]
        for m in ds:
            b = owner[m]
            if a == b:
                continue
            da, db = final[a], final[b]
            if da.split_of is not None and da.split_of == db.split_of and db.part > da.part:
                back.add(a)
                continue
            deps[str(a)].add(str(b))
    for a in back:
        if "split_back_edges" not in final[a].flags:
            final[a].flags.append("split_back_edges")

    tie: dict[str, tuple[int, float]] = {}
    for grp in {d.split_of for d in final if d.split_of is not None}:
        members = set().union(*(d.nodes for d in final if d.split_of == grp))
        tie[grp] = (len({r for n in members for r in node_risks.get(n, ())}),
                    round(sum(_CONF_SCORE[by_id[n].confidence] for n in members) / max(1, len(members)), 3))

    def prio(i: str):
        d = final[int(i)]
        risks, conf = tie.get(d.split_of, (len(d.risks), d.confidence))   # parts of one slice share a tie-break
        return (CATEGORY_RANK[d.category], risks, -conf, d.split_of or d.group, d.part or 0, d.key)

    order = [int(i) for i in topo_order(deps.keys(), deps, prio)]
    pos = {old: new for new, old in enumerate(order)}
    ordered = [final[i] for i in order]
    for old, d in zip(order, ordered):
        d.depends_on = sorted(pos[int(x)] for x in deps[str(old)])
    check_invariant(in_scope, (d.nodes for d in ordered))
    return PlanDraft(slices=ordered, excluded=dict(sorted(excluded.items())), in_scope=in_scope,
                     inferred_links=inferred, warnings=warnings)


def _group_name(grp: str, cat: str, by_id: Mapping[str, Node], comp: list[str]) -> str:
    if cat in CATEGORY_NAME:
        return CATEGORY_NAME[cat]
    if cat == "shared":
        return f"Shared components: {grp.split(':', 1)[1]}"
    if cat == "bootstrap":
        return "Application bootstrap and wiring"
    if grp.startswith("entry:endpoint:"):
        return f"Endpoint: {grp.split(':', 2)[2]}"
    if grp.startswith("entry:"):
        return f"Endpoints {grp.split(':', 1)[1]}"
    if grp.startswith(("job:", "consumer:")):
        kind, nid = grp.split(":", 1)
        return f"{kind.capitalize()}: {by_id[nid].name}"
    return grp


def _merge_cycles(drafts: list[DraftSlice], needs: Mapping[str, set[str]]) -> list[DraftSlice]:
    owner = {n: i for i, d in enumerate(drafts) for n in d.nodes}
    adj: dict[str, set[str]] = defaultdict(set)
    for n, ds in needs.items():
        for m in ds:
            if owner[n] != owner[m]:
                adj[str(owner[n])].add(str(owner[m]))
    out: list[DraftSlice] = []
    for comp in strongly_connected((str(i) for i in range(len(drafts))), adj):
        members = sorted((drafts[int(i)] for i in comp), key=lambda d: (CATEGORY_RANK[d.category], d.group))
        if len(members) == 1:
            out.append(members[0])
            continue
        kind = "entry" if any(m.kind == "entry" for m in members) else "foundation"
        cat = next((m.category for m in members if m.kind == kind), members[0].category)
        flags = sorted({f for m in members for f in m.flags} | {"merged_cycle"})
        out.append(DraftSlice(group=" + ".join(m.group for m in members), kind=kind, category=cat,
                              name=" + ".join(m.name for m in members), nodes=set().union(*(m.nodes for m in members)),
                              flags=flags))
    return out


def _primary_file(n: Node) -> str:
    return n.evidence[0].file if n.evidence else ""


def _split(d: DraftSlice, by_id: Mapping[str, Node], needs: Mapping[str, set[str]], sizer: Sizer, budget: int,
           warnings: list[str]) -> list[DraftSlice]:
    nodes = [by_id[n] for n in sorted(d.nodes)]
    regions = Sizer.merge_regions(nodes)
    total = sizer.cost(regions)
    if total <= budget or len(nodes) == 1:
        if total > budget:
            d.flags.append("oversized")
            warnings.append(f"slice {d.name!r} ({total} tokens) exceeds --max-slice-tokens {budget} and cannot be "
                            "split (single node)")
        return [d]
    by_file: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        by_file[_primary_file(n)].append(n.id)
    file_of = {n: f for f, ns in by_file.items() for n in ns}
    fneeds: dict[str, set[str]] = defaultdict(set)
    for n in d.nodes:
        for m in needs.get(n, ()):
            if m in d.nodes and file_of[m] != file_of[n]:
                fneeds[file_of[n]].add(file_of[m])
    files_order: list[str] = []
    for comp in strongly_connected(by_file.keys(), fneeds):   # dependencies first
        files_order.extend(comp)
    pieces: list[tuple[int, list[str]]] = []   # (tokens, node ids)
    for f in files_order:
        fnodes = by_file[f]
        if not f:
            pieces.append((0, fnodes))
            continue
        fcost = sizer.cost({f: regions.get(f)})
        segs = sizer.split_file(f, budget) if fcost > budget and len(fnodes) > 1 else []
        if len(segs) < 2:
            pieces.append((fcost, fnodes))
            continue
        assigned: list[list[str]] = [[] for _ in segs]
        for nid in fnodes:
            starts = [ev.line_start for ev in by_id[nid].evidence if ev.file == f and ev.line_start]
            line = min(starts) if starts else 1
            idx = next((k for k, (a, b, _) in enumerate(segs) if a <= line <= b), 0)
            assigned[idx].append(nid)
        carry = 0
        local: list[list] = []
        for (a, b, c), ns in zip(segs, assigned):
            if ns:
                local.append([c + carry, ns])
                carry = 0
            elif local:
                local[-1][0] += c
            else:
                carry += c
        if carry and local:
            local[0][0] += carry
        pieces.extend((c, ns) for c, ns in local)
    bins: list[list] = []
    for c, ns in pieces:
        if bins and (bins[-1][0] + c <= budget or not bins[-1][1]):
            bins[-1][0] += c
            bins[-1][1].extend(ns)
        else:
            bins.append([c, list(ns)])
    if len(bins) < 2:
        d.flags.append("oversized")
        warnings.append(f"slice {d.name!r} ({total} tokens) exceeds --max-slice-tokens {budget}; no file or symbol "
                        "boundary splits it further")
        return [d]
    out = []
    for k, (c, ns) in enumerate(bins, 1):
        flags = sorted(set(d.flags) | {"split"} | ({"oversized"} if c > budget else set()))
        if c > budget:
            warnings.append(f"part {k}/{len(bins)} of slice {d.name!r} ({c} tokens) still exceeds {budget}")
        out.append(DraftSlice(group=d.group, kind=d.kind, category=d.category, name=f"{d.name} (part {k}/{len(bins)})",
                              nodes=set(ns), flags=flags, split_of=d.group, part=k, parts=len(bins), est_tokens=c))
    return out


# ------------------------------------------------------------------ model naming pass
def validate_naming(raw: Any, slices: Mapping[str, Iterable[str]], known_nodes: set[str]
                    ) -> tuple[dict[str, str], dict[str, str], list[MergeSuggestion], list[str]]:
    """Validate a `plan` model reply. Returns (names, packages, merge_suggestions, warnings).

    The model may only rename slices, suggest packages and suggest merges. Any attempt to change membership (echoed
    node_ids that differ from the slice) or any mention of an unknown node ID rejects the WHOLE reply."""
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {}, {}, [], ["model naming reply is not a JSON object: ignored"]
    members = {sid: set(ns) for sid, ns in slices.items()}
    blob = json.dumps(raw)
    invented = sorted(set(m.group(0) for m in _NODE_ID.finditer(blob)) - known_nodes)
    if invented:
        return {}, {}, [], [f"model naming reply mentions unknown node id(s) {invented[:10]}: reply rejected"]
    names: dict[str, str] = {}
    packages: dict[str, str] = {}
    entries = raw.get("slices", [])
    if not isinstance(entries, list):
        return {}, {}, [], ["model naming reply 'slices' is not a list: ignored"]
    for ent in entries:
        if not isinstance(ent, dict) or ent.get("id") not in members:
            warnings.append(f"model naming entry for unknown slice {ent.get('id') if isinstance(ent, dict) else ent!r} "
                            "dropped")
            continue
        sid = ent["id"]
        if "node_ids" in ent:
            got = ent["node_ids"]
            if not isinstance(got, list) or set(map(str, got)) != members[sid]:
                dropped = sorted(members[sid] - set(map(str, got if isinstance(got, list) else [])))
                return {}, {}, [], [f"model naming reply changes the nodes of {sid} (dropped {dropped[:10]}): "
                                    "reply rejected, deterministic names kept"]
        name = ent.get("name")
        if isinstance(name, str) and name.strip():
            names[sid] = " ".join(name.split())[:120]
        pkg = ent.get("package")
        if isinstance(pkg, str) and pkg.strip():
            packages[sid] = pkg.strip()[:200]
    merges: list[MergeSuggestion] = []
    for m in raw.get("merge_suggestions", []) or []:
        ids = m.get("slice_ids") if isinstance(m, dict) else None
        if not isinstance(ids, list) or len(set(ids)) < 2 or not set(ids) <= members.keys():
            warnings.append(f"invalid merge suggestion dropped: {str(m)[:120]}")
            continue
        merges.append(MergeSuggestion(slice_ids=sorted(set(ids)), reason=str(m.get("reason", ""))[:300]))
    return names, packages, merges, warnings


def naming_prompt(plan_slices, by_id: Mapping[str, Node]) -> str:
    rows = []
    for s in plan_slices:
        rows.append({"id": s.id, "kind": s.kind, "current_name": s.name,
                     "node_ids": s.node_ids,
                     "nodes": [{"id": n, "type": by_id[n].type, "name": by_id[n].name, "module": by_id[n].module}
                               for n in s.node_ids[:60]]})
    return json.dumps({"slices": rows}, indent=1)


def run_naming_pass(llm, plan_slices, by_id: Mapping[str, Node]):
    from ..llm.base import BudgetExceeded, LLMError, parse_json
    system = PROMPT_FILE.read_text()
    try:
        resp = llm.complete(task="plan", system=system, prompt=naming_prompt(plan_slices, by_id), json_mode=True,
                            prompt_version=PROMPT_VERSION)
        raw = parse_json(resp.text)
    except (BudgetExceeded, LLMError) as e:
        return {}, {}, [], [f"model naming pass skipped: {type(e).__name__}: {str(e)[:200]}"]
    except ValueError:
        return {}, {}, [], ["model naming reply is not valid JSON: ignored"]
    return validate_naming(raw, {s.id: s.node_ids for s in plan_slices}, set(by_id))


# ------------------------------------------------------------------ project-level planning
@dataclass
class PlanResult:
    plan: Any                     # MigrationPlan
    report: PlannerReport
    written: list[Path] = field(default_factory=list)


def _load_risks(project) -> list:
    from .models import FitReport
    from .paths import Phase2Layout
    p = Phase2Layout(project).fit_report_json
    if not p.exists():
        return []
    try:
        return list(FitReport.model_validate_json(p.read_text()).risks)
    except ValueError:
        return []


def _fit_blockers(project, risks_by_id: Mapping[str, Any], warnings: list[str]) -> list[tuple[str, set[str]]]:
    try:
        from . import fit as _fit
        fn = getattr(_fit, "fit_blockers")
    except (ImportError, AttributeError):
        warnings.append("target-fit module not available: no fit-risk blockers attached")
        return []
    out = []
    from .paths import Phase2Layout
    if not Phase2Layout(project).target_yaml.exists():
        warnings.append("no target/target.yaml: planning target-agnostically, no fit-risk blockers attached")
        return []
    try:
        got = list(fn(project) or [])
    except getattr(_fit, "FitError", RuntimeError) as e:
        # a missing or stale fit report must never read as "no blockers" (plan section 2)
        raise PlanError(f"target fit is required before planning: {e}") from e
    for r in got:
        rid = r if isinstance(r, str) else (r.get("id") if isinstance(r, dict) else getattr(r, "id", None))
        items = (r.get("item_ids") if isinstance(r, dict) else getattr(r, "item_ids", None)) if not isinstance(r, str) \
            else None
        if items is None and rid in risks_by_id:
            items = risks_by_id[rid].item_ids
        if rid:
            out.append((str(rid), set(items or [])))
    return out


def _llm_config(config_path: Path | None):
    from ..llm.litellm_client import LLMConfig
    if config_path is not None:
        return LLMConfig.load(config_path)
    try:
        from ..config_env import load_llm_config_from_env
        return load_llm_config_from_env()
    except Exception:  # noqa: BLE001 - estimates fall back to default pricing
        return LLMConfig()


def estimate_slice_usd(est_tokens: int, n_nodes: int, config=None) -> float:
    from ..llm.litellm_client import estimate_cost
    tin = est_tokens + SPEC_OVERHEAD_TOKENS + SPEC_TOKENS_PER_NODE * n_nodes
    tout = max(MIN_OUTPUT_TOKENS, est_tokens)
    return round(estimate_cost([("codegen", tin, tout)], config), 4)


def _next_version(versions_dir: Path) -> int:
    if not versions_dir.is_dir():
        return 1
    vs = [int(m.group(1)) for p in versions_dir.iterdir() if (m := re.fullmatch(r"v(\d+)\.json", p.name))]
    return max(vs, default=0) + 1


def plan_project(project, *, dry_run: bool = False, offline: bool = False, include_dead_code: bool = False,
                 max_slice_tokens: int = DEFAULT_MAX_SLICE_TOKENS, name_with_model: bool = False, pin: bool = False,
                 accept_unmapped: bool = False, llm=None, config_path: Path | None = None,
                 max_usd: float | None = None) -> PlanResult:
    """Plan the migration of `project` (see module docstring). `llm` overrides the naming-pass client (tests)."""
    from ..graph.store import parse_graph
    from .contract import require_contract
    from .ids import IdAllocator, allocator
    from .models import MigrationPlan, Slice, format_id
    from .paths import Phase2Layout
    from .state import atomic_write_json, atomic_write_text, file_lock, load_state, now_iso, save_state, sha256_file

    contract = require_contract(project, pin=pin, accept_unmapped=accept_unmapped)
    lay = Phase2Layout(project)
    snap = (Path(project.root) / contract.graph_snapshot).resolve()
    if not snap.is_relative_to(Path(project.root).resolve()) or not snap.exists():
        raise PlanError(f"approved graph snapshot {contract.graph_snapshot!r} not found in the project")
    graph = parse_graph(snap.read_text())
    by_id = {n.id: n for n in graph.nodes}
    warnings: list[str] = []

    risks = _load_risks(project)
    risks_by_id = {r.id: r for r in risks}
    node_risks: dict[str, set[str]] = defaultdict(set)
    for r in risks:
        for i in r.item_ids:
            node_risks[i].add(r.id)

    ast = None
    ast_path = Path(project.facts) / "ast.json"
    if ast_path.exists():
        from ..extract.treesitter import AstFacts
        try:
            ast = AstFacts.model_validate_json(ast_path.read_text())
        except ValueError:
            warnings.append("facts/ast.json unreadable: symbol boundaries unavailable, splitting by lines")
    sizer = Sizer(Path(project.code), ast)
    draft = build_plan(graph, include_dead_code=include_dead_code, max_slice_tokens=max_slice_tokens, sizer=sizer,
                       node_risks=node_risks)
    warnings.extend(draft.warnings)

    # stable ids keyed by sorted node ids
    keys = [d.key for d in draft.slices]
    if dry_run:
        alloc = IdAllocator(lay.ids_file)      # read-only use: no folder creation, no writes
        nxt = alloc.last("SLICE") if lay.ids_file.exists() else 0
        ids = {}
        for k in sorted(set(keys)):
            got = alloc.lookup("SLICE", k) if lay.ids_file.exists() else None
            if got is None:
                nxt += 1
                got = format_id("SLICE", nxt)
            ids[k] = got
    else:
        ids = allocator(project).allocate_many("SLICE", keys)

    # blockers
    blockers: dict[int, list[str]] = defaultdict(list)
    plan_blockers: list[str] = []
    for f in contract.deferred_findings:
        hit = [i for i, d in enumerate(draft.slices) if d.nodes & set(f.item_ids)]
        if not f.item_ids or not hit:
            plan_blockers.append(f"{f.id} ({f.title}): deferred finding with no in-scope items blocks cut-over")
        for i in hit:
            blockers[i].append(f.id)
    for rid, items in _fit_blockers(project, risks_by_id, warnings):
        hit = [i for i, d in enumerate(draft.slices) if d.nodes & items]
        if not hit:
            plan_blockers.append(f"{rid}: open high-severity fit risk not tied to a slice")
        for i in hit:
            if rid not in blockers[i]:
                blockers[i].append(rid)

    cfg = _llm_config(config_path)
    slices = []
    for i, d in enumerate(draft.slices):
        sid = ids[d.key]
        status = load_state(project, sid).status
        slices.append(Slice(id=sid, name=d.name, kind=d.kind, node_ids=sorted(d.nodes),
                            depends_on=[ids[draft.slices[j].key] for j in d.depends_on], order=i + 1, status=status,
                            risks=d.risks, estimate_usd=estimate_slice_usd(d.est_tokens, len(d.nodes), cfg),
                            blockers=sorted(blockers[i])))

    report = PlannerReport(dry_run=dry_run, graph_version=contract.graph_version,
                           graph_snapshot=contract.graph_snapshot, max_slice_tokens=max_slice_tokens,
                           include_dead_code=include_dead_code, inferred_links=draft.inferred_links,
                           plan_blockers=plan_blockers)
    packages: dict[str, str] = {}
    if name_with_model:
        if dry_run:
            warnings.append("--dry-run: model naming pass skipped (no model spend in a dry run)")
        else:
            if llm is None:
                llm = _naming_llm(project, offline, cfg, max_usd)
            names, packages, merges, nwarn = run_naming_pass(llm, slices, by_id)
            warnings.extend(nwarn)
            report.merge_suggestions = merges
            report.named_by_model = bool(names)
            slices = [s.model_copy(update={"name": names[s.id]}) if s.id in names else s for s in slices]
    for s, d in zip(slices, draft.slices):
        report.details.append(SliceDetail(slice_id=s.id, key=d.key, group=d.group, category=d.category,
                                          est_tokens=d.est_tokens, confidence=d.confidence, split_part=d.part,
                                          split_total=d.parts, package=packages.get(s.id, ""), flags=d.flags))

    target_sha = sha256_file(lay.target_yaml) if lay.target_yaml.exists() else ""
    check_invariant(draft.in_scope, (s.node_ids for s in slices))
    written: list[Path] = []
    if dry_run:
        plan = MigrationPlan(version=_next_version(lay.plan_versions), contract_graph_sha256=contract.graph_sha256,
                             target_sha256=target_sha, created=now_iso(), slices=slices,
                             excluded_nodes=draft.excluded)
        report.plan_version = plan.version
        report.warnings = warnings
        return PlanResult(plan=plan, report=report)

    lay.ensure(lay.plan_dir)
    with file_lock(lay.plan_json):
        prev = None
        if lay.plan_json.exists():
            try:
                prev = MigrationPlan.model_validate_json(lay.plan_json.read_text())
            except ValueError:
                warnings.append("previous plan/migration_plan.json unreadable: retired slices not computed")
        version = _next_version(lay.plan_versions)
        plan = MigrationPlan(version=version, contract_graph_sha256=contract.graph_sha256, target_sha256=target_sha,
                             created=now_iso(), slices=slices, excluded_nodes=draft.excluded)
        if prev is not None:
            report.retired_slice_ids = sorted({s.id for s in prev.slices} - {s.id for s in slices})
        report.plan_version = version
        report.warnings = warnings
        vpath = lay.plan_version(version)
        if vpath.exists():
            raise PlanError(f"{vpath} already exists (plan versions are immutable)")
        written.append(atomic_write_json(vpath, plan))
        written.append(atomic_write_json(lay.plan_json, plan))
        written.append(atomic_write_text(lay.plan_md, render_markdown(plan, report, by_id)))
        written.append(atomic_write_json(lay.plan_dir / "planner_report.json", report))
    for s in slices:
        if not lay.slice_state(s.id).exists():
            from .models import SliceState
            written.append(save_state(project, SliceState(slice_id=s.id)))
    from ..audit import AuditLog
    AuditLog(Path(project.audit_dir) / "audit.jsonl").record(
        "planner", "plan_created", plan_version=version, slices=len(slices), graph_version=contract.graph_version,
        excluded=len(draft.excluded), retired=report.retired_slice_ids, named_by_model=report.named_by_model)
    return PlanResult(plan=plan, report=report, written=written)


def _naming_llm(project, offline: bool, cfg, max_usd: float | None):
    if offline:
        from ..llm.offline import OfflineLLM
        return OfflineLLM()
    import os
    if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")):
        raise PlanError("--name-with-model needs an LLM API key (or pass --offline)")
    from ..audit import AuditLog
    from ..llm.litellm_client import LiteLLMClient
    from .ledger import budget_for, project_budget_usd
    return LiteLLMClient(cfg, budget=budget_for(project, project_budget_usd(project), max_usd=max_usd),
                         cache_dir=Path(project.root) / "llm_cache", audit=AuditLog(Path(project.audit_dir) / "audit.jsonl"),
                         agent="planner")


# ------------------------------------------------------------------ markdown
def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(plan, report: PlannerReport, by_id: Mapping[str, Node]) -> str:
    det = {d.slice_id: d for d in report.details}
    L = [f"# Migration plan v{plan.version}", "",
         f"- Approved graph: `{report.graph_snapshot}` (v{report.graph_version}, sha256 "
         f"`{plan.contract_graph_sha256[:12]}`)",
         f"- Target spec sha256: `{plan.target_sha256[:12] or '(no target/target.yaml)'}`",
         f"- Slices: {len(plan.slices)}; nodes in scope: {sum(len(s.node_ids) for s in plan.slices)}; "
         f"excluded: {len(plan.excluded_nodes)}",
         f"- Estimated codegen cost: ${sum(s.estimate_usd for s in plan.slices):.4f} (one pass, `codegen` tier)",
         f"- Max slice tokens: {report.max_slice_tokens}", "",
         "## Order", "",
         "| # | Slice | Kind | Name | Nodes | Depends on | Est. tokens | Est. USD | Risks | Blockers |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for s in plan.slices:
        d = det.get(s.id)
        L.append(f"| {s.order} | {s.id} | {s.kind} | {_md(s.name)} | {len(s.node_ids)} | "
                 f"{', '.join(s.depends_on) or '-'} | {d.est_tokens if d else 0} | {s.estimate_usd:.4f} | "
                 f"{', '.join(s.risks) or '-'} | {', '.join(s.blockers) or '-'} |")
    L += ["", "## Slices", ""]
    for s in plan.slices:
        d = det.get(s.id)
        L.append(f"### {s.order}. {s.id}: {_md(s.name)}")
        L.append("")
        extra = []
        if d is not None:
            extra.append(f"group `{d.group}`, category {d.category}")
            if d.package:
                extra.append(f"suggested package `{d.package}`")
            if d.flags:
                extra.append("flags: " + ", ".join(d.flags))
        if extra:
            L += ["- " + "; ".join(extra)]
        for n in s.node_ids:
            node = by_id.get(n)
            L.append(f"- `{n}` {node.type if node else '?'}: {_md(node.name if node else '')}")
        L.append("")
    L += ["## Blockers", ""]
    any_b = False
    for s in plan.slices:
        if s.blockers:
            any_b = True
            L.append(f"- {s.id} ({_md(s.name)}): {', '.join(s.blockers)}")
    for b in report.plan_blockers:
        any_b = True
        L.append(f"- plan-level: {_md(b)}")
    if not any_b:
        L.append("None.")
    L += ["", "## Not migrated", ""]
    if plan.excluded_nodes:
        for n, why in plan.excluded_nodes.items():
            node = by_id.get(n)
            L.append(f"- `{n}` {_md(node.name if node else '')}: {_md(why)}")
    else:
        L.append("Nothing excluded.")
    if report.merge_suggestions:
        L += ["", "## Merge suggestions (model, not applied)", ""]
        L += [f"- {', '.join(m.slice_ids)}: {_md(m.reason)}" for m in report.merge_suggestions]
    if report.retired_slice_ids:
        L += ["", "## Retired slices (in the previous plan, not in this one)", "",
              ", ".join(report.retired_slice_ids)]
    if report.warnings:
        L += ["", "## Warnings", ""] + [f"- {_md(w)}" for w in report.warnings]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ CLI
def register(groups) -> None:
    @groups.main.command("plan")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--dry-run", is_flag=True, help="Compute and print the plan; write nothing.")
    @click.option("--offline", is_flag=True, help="No network: the naming pass (if requested) uses the offline LLM.")
    @click.option("--include-dead-code", is_flag=True, help="Migrate dead_code nodes too (default: listed, not migrated).")
    @click.option("--max-slice-tokens", type=click.IntRange(min=1), default=DEFAULT_MAX_SLICE_TOKENS, show_default=True,
                  help="Codegen context budget for a slice's legacy code; larger slices are split.")
    @click.option("--name-with-model", is_flag=True, help="Optional model pass that names slices and suggests merges.")
    @click.option("--pin-contract", "pin", is_flag=True,
                  help="Plan from the approved snapshot even if the graph was re-analysed later.")
    @click.option("--accept-unmapped", is_flag=True, help="Accept unmapped inventory items in the approved coverage.")
    @click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None,
                  help="LLM config.json (tiers and pricing for estimates and the naming pass).")
    @click.option("--max-usd", type=float, default=None, help="Budget cap for the naming pass.")
    def plan_cmd(project: Path, dry_run: bool, offline: bool, include_dead_code: bool, max_slice_tokens: int,
                 name_with_model: bool, pin: bool, accept_unmapped: bool, config_path: Path | None,
                 max_usd: float | None) -> None:
        """Build the migration plan (ordered slices) from the approved migration contract."""
        from ..llm.base import BudgetExceeded
        from ..project import Project
        from .contract import ContractError
        try:
            p = Project.load(project)
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))
        try:
            res = plan_project(p, dry_run=dry_run, offline=offline, include_dead_code=include_dead_code,
                               max_slice_tokens=max_slice_tokens, name_with_model=name_with_model, pin=pin,
                               accept_unmapped=accept_unmapped, config_path=config_path, max_usd=max_usd)
        except (ContractError, PlanError, BudgetExceeded) as e:
            raise click.ClickException(str(e))
        plan = res.plan
        click.echo(f"plan v{plan.version}{' (dry run, nothing written)' if dry_run else ''}: {len(plan.slices)} "
                   f"slice(s), {len(plan.excluded_nodes)} excluded node(s), est. "
                   f"${sum(s.estimate_usd for s in plan.slices):.4f}")
        for s in plan.slices:
            extra = f"  blockers: {', '.join(s.blockers)}" if s.blockers else ""
            click.echo(f"  {s.order:>3}. {s.id} [{s.kind}] {s.name} ({len(s.node_ids)} nodes){extra}")
        for w in res.report.warnings:
            click.echo(f"  warning: {w}")
        if not dry_run:
            click.echo(f"written: {Path(p.root) / 'plan' / 'migration_plan.json'}")
