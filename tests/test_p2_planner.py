"""M4 planner (migrate/planner.py): docs/phase2-plan.md section 3."""
import json
import sys
import types

import pytest
from click.testing import CliRunner

import migration_agent.migrate as migrate_pkg
from migration_agent.cli import main
from migration_agent.extract.treesitter import AstFacts, FileFacts, FunctionFact
from migration_agent.graph import load_latest, save_snapshot
from migration_agent.graph.models import Edge, Evidence, Graph, Node
from migration_agent.llm.base import FakeLLM
from migration_agent.migrate.contract import ContractError
from migration_agent.migrate.models import MigrationPlan, SliceState
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.planner import (
    PlanInvariantError, PlannerReport, Sizer, build_plan, check_invariant, plan_project, resource_key,
    strongly_connected, topo_order, validate_naming,
)
from migration_agent.review.approval import approve
from migration_agent.review.docrender import write_latest
from migration_agent.review.resolutions import add_resolution

from phase1_helpers import make_project, write_findings
from phase2_helpers import approved_project, contract_file


# ------------------------------------------------------------------ synthetic graphs
def N(id_, type_, name=None, file=None, line=None, module=""):
    ev = [Evidence(file=file, line_start=line, line_end=line, quote="x" if line else None)] if file else []
    return Node(id=id_, type=type_, name=name or id_, module=module, evidence=ev)


def G(nodes, edges):
    return Graph(version=1, nodes=nodes,
                 edges=[Edge(id=f"EDGE-{i:04d}", source=s, target=t, type=k) for i, (s, k, t) in enumerate(edges, 1)])


def synthetic():
    nodes = [
        N("ENT-0001", "data_entity", "Order"),
        N("COMP-0001", "component", "OrderRepository", module="repo"),
        N("CFG-0001", "config", "DB_URL"),
        N("COMP-0002", "component", "OrderService", module="services"),
        N("COMP-0003", "component", "PricingA", module="pricing"),
        N("COMP-0004", "component", "PricingB", module="pricing"),
        N("COMP-0005", "component", "OrdersController", module="web"),
        N("EP-0001", "endpoint", "GET /api/orders"),
        N("EP-0002", "endpoint", "POST /api/orders/:id/cancel"),
        N("EP-0003", "endpoint", "GET /api/customers"),
        N("COMP-0006", "component", "CustomersController", module="web"),
        N("COMP-0007", "component", "Formatter", module="text"),
        N("JOB-0001", "job", "nightly-report"),
        N("DEAD-0001", "dead_code", "oldHelper"),
        N("RULE-0001", "business_rule", "cancel only within 14 days"),
    ]
    edges = [
        ("COMP-0001", "reads", "ENT-0001"), ("COMP-0001", "writes", "ENT-0001"),
        ("CFG-0001", "configures", "COMP-0001"),
        ("COMP-0002", "calls", "COMP-0001"), ("COMP-0002", "calls", "COMP-0003"),
        ("COMP-0003", "calls", "COMP-0004"), ("COMP-0004", "calls", "COMP-0003"),   # cycle
        ("COMP-0005", "implements", "EP-0001"), ("COMP-0005", "implements", "EP-0002"),
        ("COMP-0005", "calls", "COMP-0002"), ("COMP-0005", "calls", "COMP-0007"),
        ("COMP-0006", "implements", "EP-0003"), ("COMP-0006", "calls", "COMP-0007"),  # Formatter shared
        ("JOB-0001", "calls", "COMP-0002"),
        ("COMP-0002", "implements", "RULE-0001"),
        ("DEAD-0001", "calls", "COMP-0007"),
    ]
    return G(nodes, edges)


def _slice_of(draft, node_id):
    return next(i for i, d in enumerate(draft.slices) if node_id in d.nodes)


# ------------------------------------------------------------------ algorithm
def test_tarjan_condenses_cycles_iteratively():
    adj = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": [], "e": ["d", "e"]}
    sccs = strongly_connected(adj.keys(), adj)
    assert sorted(map(tuple, sccs)) == [("a", "b", "c"), ("d",), ("e",)]
    assert sccs.index(["d"]) < sccs.index(["a", "b", "c"])        # dependencies first
    chain = {str(i): [str(i + 1)] for i in range(5000)}            # deep: no recursion limit
    chain["5000"] = ["0"]
    assert len(strongly_connected(list(chain) + ["5000"], chain)) == 1


def test_scc_lands_in_one_slice_and_order_is_topological():
    draft = build_plan(synthetic())
    assert _slice_of(draft, "COMP-0003") == _slice_of(draft, "COMP-0004")
    for i, d in enumerate(draft.slices):
        assert all(j < i for j in d.depends_on)
    kinds = [d.kind for d in draft.slices]
    assert kinds.index("entry") > max(i for i, k in enumerate(kinds) if k == "foundation" and
                                      draft.slices[i].category != "shared")
    cats = [d.category for d in draft.slices]
    assert cats.index("config") < cats.index("data")


def test_foundation_promotion_entry_groups_and_shared():
    draft = build_plan(synthetic())
    data = draft.slices[_slice_of(draft, "ENT-0001")]
    assert data.kind == "foundation" and {"ENT-0001", "COMP-0001"} <= data.nodes
    assert draft.slices[_slice_of(draft, "CFG-0001")].category == "config"
    orders = draft.slices[_slice_of(draft, "EP-0001")]
    assert orders.kind == "entry" and orders.group == "entry:/api/orders"
    assert {"EP-0002", "COMP-0005"} <= orders.nodes                     # one slice per resource prefix
    # OrderService is reached by the orders endpoints and the job -> shared foundation slice
    svc = draft.slices[_slice_of(draft, "COMP-0002")]
    assert svc.kind == "foundation" and svc.category == "shared"
    assert "RULE-0001" in svc.nodes                                     # implements binds the rule
    assert draft.slices[_slice_of(draft, "COMP-0007")].category == "shared"
    job = draft.slices[_slice_of(draft, "JOB-0001")]
    assert job.kind == "entry" and job.group == "job:JOB-0001"
    assert _slice_of(draft, "COMP-0002") < _slice_of(draft, "EP-0001")


def test_resource_key():
    assert resource_key(N("EP-0001", "endpoint", "GET /api/v1/orders/:id/items")) == "/api/v1/orders"
    assert resource_key(N("EP-0002", "endpoint", "DELETE /products/{id}")) == "/products"
    assert resource_key(N("EP-0003", "endpoint", "healthcheck")) == "endpoint:healthcheck"


def test_ties_broken_by_risk_then_confidence():
    nodes = [N("EP-0001", "endpoint", "GET /a"), N("EP-0002", "endpoint", "GET /b"),
             N("EP-0003", "endpoint", "GET /c", file="c.js", line=1)]
    g = G(nodes, [])
    d = build_plan(g, node_risks={"EP-0001": ["RISK-0001"]})
    assert [s.group for s in d.slices] == ["entry:/c", "entry:/b", "entry:/a"]


def test_topo_order_rejects_cycles():
    with pytest.raises(Exception, match="cycle"):
        topo_order(["a", "b"], {"a": ["b"], "b": ["a"]}, lambda x: x)


def test_exactly_once_invariant():
    g = synthetic()
    draft = build_plan(g, include_dead_code=True)
    allnodes = [n for d in draft.slices for n in d.nodes]
    assert sorted(allnodes) == sorted(n.id for n in g.nodes)
    check_invariant(["a", "b"], [["a"], ["b"]])
    with pytest.raises(PlanInvariantError, match="missing"):
        check_invariant(["a", "b"], [["a"]])
    with pytest.raises(PlanInvariantError, match="several"):
        check_invariant(["a"], [["a"], ["a"]])
    with pytest.raises(PlanInvariantError, match="not in scope"):
        check_invariant(["a"], [["a", "z"]])


def test_dead_code_excluded_by_default_included_on_request():
    g = synthetic()
    d = build_plan(g)
    assert "DEAD-0001" in d.excluded and "dead_code" in d.excluded["DEAD-0001"]
    assert all("DEAD-0001" not in s.nodes for s in d.slices)
    inc = build_plan(g, include_dead_code=True)
    assert inc.excluded == {} and any("DEAD-0001" in s.nodes for s in inc.slices)


def test_oversized_slice_splits_on_symbol_boundaries(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    lines = [f"def f{i}(): return {i} # padding padding padding" for i in range(200)]
    (code / "big.py").write_text("\n".join(lines) + "\n")
    ast = AstFacts(files=[FileFacts(path="big.py", language="python", parse_status="ok", line_count=200,
                                    functions=[FunctionFact(name="a", qualified_name="a", start_line=1, end_line=100),
                                               FunctionFact(name="b", qualified_name="b", start_line=101, end_line=200)])])
    nodes = [N("EP-0001", "endpoint", "GET /x"),
             N("COMP-0001", "component", "A", file="big.py", line=5),
             N("COMP-0002", "component", "B", file="big.py", line=150)]
    g = G(nodes, [("COMP-0001", "implements", "EP-0001"), ("COMP-0002", "implements", "EP-0001"),
                  ("COMP-0002", "calls", "COMP-0001")])
    sizer = Sizer(code, ast)
    whole = build_plan(g, sizer=Sizer(code, ast))
    assert len(whole.slices) == 1 and whole.slices[0].est_tokens > 2000
    split = build_plan(g, sizer=sizer, max_slice_tokens=whole.slices[0].est_tokens // 2 + 50)
    assert len(split.slices) == 2
    first, second = split.slices
    assert "COMP-0001" in first.nodes and "COMP-0002" in second.nodes and "EP-0001" in first.nodes
    assert first.part == 1 and second.part == 2 and "split" in first.flags and "(part 1/2)" in first.name
    assert second.depends_on == [0]
    assert "split_back_edges" in first.flags        # implements binds both ways: back edge dropped, flagged
    check_invariant({n.id for n in g.nodes}, (s.nodes for s in split.slices))


def test_naming_validation():
    slices = {"SLICE-0001": ["COMP-0001", "EP-0001"], "SLICE-0002": ["ENT-0001"]}
    known = {"COMP-0001", "EP-0001", "ENT-0001"}
    names, pk, merges, w = validate_naming(
        {"slices": [{"id": "SLICE-0001", "name": "Orders API", "package": "app.orders"},
                    {"id": "SLICE-0099", "name": "ghost"}],
         "merge_suggestions": [{"slice_ids": ["SLICE-0001", "SLICE-0002"], "reason": "tiny"},
                               {"slice_ids": ["SLICE-0001", "SLICE-0042"]}]}, slices, known)
    assert names == {"SLICE-0001": "Orders API"} and pk == {"SLICE-0001": "app.orders"}
    assert [m.slice_ids for m in merges] == [["SLICE-0001", "SLICE-0002"]] and len(w) == 2
    # dropping a node rejects the whole reply
    names, _, merges, w = validate_naming(
        {"slices": [{"id": "SLICE-0001", "name": "X", "node_ids": ["COMP-0001"]}]}, slices, known)
    assert names == {} and "rejected" in w[0]
    # inventing a node id rejects the whole reply
    names, _, _, w = validate_naming({"slices": [{"id": "SLICE-0001", "name": "X and COMP-0777"}]}, slices, known)
    assert names == {} and "COMP-0777" in w[0]
    assert validate_naming(["nope"], slices, known)[3]


# ------------------------------------------------------------------ project level (express-crud, approved)
@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return approved_project(tmp_path, tmp_path_factory)


def _approved_with_extra(tmp_path, tmp_path_factory, extra_nodes):
    p = make_project(tmp_path, tmp_path_factory)
    g = load_latest(p.graph_dir)
    save_snapshot(p.graph_dir, g.model_copy(update={"nodes": g.nodes + extra_nodes}))
    write_latest(p)
    approve(p, "alice")
    return p


def test_plan_writes_outputs_and_states(proj):
    res = plan_project(proj)
    lay = Phase2Layout(proj)
    plan = MigrationPlan.model_validate_json(lay.plan_json.read_text())
    assert plan.version == 1 and lay.plan_version(1).read_text() == lay.plan_json.read_text()
    contract = json.loads(contract_file(proj).read_text())
    assert plan.contract_graph_sha256 == contract["graph_sha256"] and plan.target_sha256 == ""
    g = load_latest(proj.graph_dir)
    assert sorted(n for s in plan.slices for n in s.node_ids) == sorted(n.id for n in g.nodes)
    kinds = [s.kind for s in plan.slices]
    assert kinds == sorted(kinds, key=lambda k: k != "foundation")         # foundation first
    orders = {s.id: s.order for s in plan.slices}
    assert all(orders[d] < s.order for s in plan.slices for d in s.depends_on)
    assert any(s.name == "Endpoints /api/orders" for s in plan.slices)
    assert all(s.estimate_usd > 0 for s in plan.slices)
    for s in plan.slices:
        st = SliceState.model_validate_json(lay.slice_state(s.id).read_text())
        assert st.status == "planned" and s.status == "planned"
    md = lay.plan_md.read_text()
    assert "## Order" in md and "| 1 | " in md and "## Not migrated" in md
    rep = PlannerReport.model_validate_json((lay.plan_dir / "planner_report.json").read_text())
    assert rep.plan_version == 1 and len(rep.details) == len(plan.slices)
    assert res.written and lay.plan_json in res.written


def test_stable_ids_across_replans(proj):
    lay = Phase2Layout(proj)
    first = plan_project(proj).plan
    v1 = lay.plan_version(1).read_text()
    second = plan_project(proj).plan
    assert second.version == 2 and lay.plan_version(1).read_text() == v1 and lay.plan_version(2).exists()
    assert [(s.id, s.node_ids) for s in first.slices] == [(s.id, s.node_ids) for s in second.slices]
    third = plan_project(proj, max_slice_tokens=300)                       # splits some slices
    unchanged = {tuple(s.node_ids): s.id for s in first.slices}
    for s in third.plan.slices:
        if tuple(s.node_ids) in unchanged:
            assert s.id == unchanged[tuple(s.node_ids)]
    assert third.report.retired_slice_ids
    assert set(third.report.retired_slice_ids) <= {s.id for s in first.slices}
    assert any("split" in d.flags for d in third.report.details)
    back = plan_project(proj).plan
    assert [(s.id, s.node_ids) for s in back.slices] == [(s.id, s.node_ids) for s in first.slices]


def test_dry_run_writes_nothing(proj):
    lay = Phase2Layout(proj)
    meta_before = proj.meta_path.read_text()
    res = plan_project(proj, dry_run=True, name_with_model=True, llm=FakeLLM(lambda *a: "{}"))
    assert res.plan.slices and res.written == []
    for d in (lay.plan_dir, lay.slices_dir, lay.migration):
        assert not d.exists()
    assert proj.meta_path.read_text() == meta_before
    assert any("dry-run" in w for w in res.report.warnings)
    r = CliRunner().invoke(main, ["plan", str(proj.root), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "dry run, nothing written" in r.output and not lay.plan_dir.exists()


def test_cli_plan(proj):
    r = CliRunner().invoke(main, ["plan", str(proj.root), "--offline", "--name-with-model"])
    assert r.exit_code == 0, r.output
    assert "SLICE-0001" in r.output and Phase2Layout(proj).plan_json.exists()


def test_dead_code_in_project_listed_not_migrated(tmp_path, tmp_path_factory):
    dead = Node(id="DEAD-0001", type="dead_code", name="legacyExport", module="src/old.js",
                evidence=[Evidence(file="src/app.js")])
    p = _approved_with_extra(tmp_path, tmp_path_factory, [dead])
    plan = plan_project(p).plan
    assert plan.excluded_nodes.keys() == {"DEAD-0001"}
    assert all("DEAD-0001" not in s.node_ids for s in plan.slices)
    md = Phase2Layout(p).plan_md.read_text()
    assert "`DEAD-0001` legacyExport: dead_code: not migrated" in md
    inc = plan_project(p, include_dead_code=True).plan
    assert inc.excluded_nodes == {} and any("DEAD-0001" in s.node_ids for s in inc.slices)


def test_deferred_finding_becomes_slice_blocker(tmp_path, tmp_path_factory):
    p = make_project(tmp_path, tmp_path_factory)
    write_findings(p, p.bug_id)
    add_resolution(p, "F-0001", "deferred", "post-migration")
    add_resolution(p, "F-0002", "dismissed")
    approve(p, "alice")
    plan = plan_project(p).plan
    blocked = [s for s in plan.slices if s.blockers]
    assert len(blocked) == 1 and blocked[0].blockers == ["F-0001"] and p.bug_id in blocked[0].node_ids
    md = Phase2Layout(p).plan_md.read_text()
    assert "F-0001" in md.split("## Blockers", 1)[1]


def test_fit_risks_and_blockers(proj, monkeypatch):
    lay = Phase2Layout(proj)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text("kind: framework_move\nsource: {language: javascript}\ntarget: {language: typescript}\n")
    g = load_latest(proj.graph_dir)
    ep = next(n.id for n in g.nodes if n.type == "endpoint")
    lay.fit_report_json.write_text(json.dumps({"risks": [
        {"id": "RISK-0001", "kind": "unknown", "severity": "high", "title": "t", "item_ids": [ep]},
        {"id": "RISK-0002", "kind": "unknown", "severity": "low", "title": "u", "item_ids": [ep]}]}))
    fake = types.ModuleType("migration_agent.migrate.fit")
    fake.fit_blockers = lambda project: [{"id": "RISK-0001", "item_ids": [ep]}]
    monkeypatch.setitem(sys.modules, "migration_agent.migrate.fit", fake)
    monkeypatch.setattr(migrate_pkg, "fit", fake, raising=False)
    plan = plan_project(proj).plan
    s = next(s for s in plan.slices if ep in s.node_ids)
    assert s.risks == ["RISK-0001", "RISK-0002"] and s.blockers == ["RISK-0001"]
    assert len(plan.target_sha256) == 64


def test_fit_module_missing_is_tolerated(proj, monkeypatch):
    monkeypatch.setitem(sys.modules, "migration_agent.migrate.fit", None)   # import raises ImportError
    monkeypatch.delattr(migrate_pkg, "fit", raising=False)
    res = plan_project(proj, dry_run=True)
    assert any("target-fit module not available" in w for w in res.report.warnings)


def test_model_naming_pass_cannot_drop_nodes(proj):
    base = plan_project(proj).plan
    victim = base.slices[0]
    bad = FakeLLM(lambda task, s, prompt: json.dumps(
        {"slices": [{"id": victim.id, "name": "Renamed", "node_ids": victim.node_ids[1:]}]}))
    res = plan_project(proj, name_with_model=True, llm=bad)
    assert bad.calls and bad.calls[0]["task"] == "plan" and bad.calls[0]["prompt_version"] == "plan-v1"
    assert [(s.id, s.name, s.node_ids) for s in res.plan.slices] == [(s.id, s.name, s.node_ids) for s in base.slices]
    assert any("rejected" in w for w in res.report.warnings) and not res.report.named_by_model
    good = FakeLLM(lambda task, s, prompt: json.dumps(
        {"slices": [{"id": victim.id, "name": "Build setup", "package": "build"}],
         "merge_suggestions": [{"slice_ids": [base.slices[0].id, base.slices[1].id], "reason": "small"}]}))
    res = plan_project(proj, name_with_model=True, llm=good)
    named = next(s for s in res.plan.slices if s.id == victim.id)
    assert named.name == "Build setup" and named.node_ids == victim.node_ids and res.report.named_by_model
    assert res.report.merge_suggestions[0].reason == "small"
    md = Phase2Layout(proj).plan_md.read_text()
    assert "Merge suggestions" in md and "Build setup" in md


def test_plan_uses_approved_snapshot_not_latest(proj):
    contract = json.loads(contract_file(proj).read_text())
    g = load_latest(proj.graph_dir)
    newer = g.model_copy(update={"nodes": g.nodes + [Node(id="COMP-0999", type="component", name="added later")]})
    save_snapshot(proj.graph_dir, newer)
    with pytest.raises(ContractError, match="re-analysed after approval"):
        plan_project(proj)
    plan = plan_project(proj, pin=True).plan
    nodes = {n for s in plan.slices for n in s.node_ids}
    assert "COMP-0999" not in nodes and nodes == {n.id for n in g.nodes}
    assert plan.contract_graph_sha256 == contract["graph_sha256"]
    r = CliRunner().invoke(main, ["plan", str(proj.root)])
    assert r.exit_code == 1 and "pin-contract" in r.output
