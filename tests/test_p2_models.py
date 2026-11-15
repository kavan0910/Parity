"""Phase 2 shared contracts (migrate/models.py), ID allocator (ids.py) and folder layout (paths.py)."""
import json
import textwrap

import pytest
from pydantic import ValidationError

from migration_agent.migrate import models as m
from migration_agent.migrate.ids import IdAllocator, allocate, allocator
from migration_agent.migrate.paths import PHASE2_DIRS, Phase2Layout
from migration_agent.project import Project, ProjectMeta

TARGET_YAML = textwrap.dedent("""\
    schema_version: 1
    kind: version_upgrade
    source: {language: java, runtime: "8", framework: spring-boot@2.7}
    target: {language: java, runtime: "21", framework: spring-boot@3.3, build: maven,
             base_image: "eclipse-temurin:21-jre@sha256:abc"}
    infra: {db: postgres@15, messaging: none, deploy: container, port: 8080}
    style: {formatter: google-java-format, linters: [checkstyle], package_root: com.acme.library,
            rules: ["constructor injection only", "no Lombok"]}
    libraries: {allow: [], deny: ["log4j:log4j"]}
    equivalence: {wire_compat: strict, perf_regression_max_pct: null}
    strategy: strangler
    budget_usd: 50
""")
H = "a" * 64


def _rt(obj):
    cls = type(obj)
    again = cls.model_validate_json(obj.model_dump_json())
    assert again == obj
    return again


def _case(**kw):
    d = {"id": "CT-0042", "slice": "SLICE-0007", "covers": ["EP-0003", "RULE-0011"], "kind": "http",
         "setup": {"db_fixture": "fixtures/loans_overdue.sql", "stubs": ["EXT-0002/payment_ok"]},
         "request": {"method": "POST", "path": "/api/loans", "headers": {"Content-Type": "application/json"},
                     "body": {"bookId": 7, "memberId": 3}},
         "expect": {"source": "recorded", "status": 409, "body": {"error": "book already on loan"},
                    "db_after": {"loans": "count(book_id=7)=1"}},
         "normalize": ["$.timestamp", "$.id"], "decision": "preserve",
         "provenance": {"generator": "test_gen", "prompt_version": "v1"}}
    d.update(kw)
    return d


def test_id_helpers():
    assert m.PREFIXES == ("SLICE", "CT", "RISK", "EQ", "RUN")
    assert m.format_id("SLICE", 7) == "SLICE-0007" and m.format_id("RUN", 12345) == "RUN-12345"
    assert m.is_id("CT-0001") and m.is_id("CT-0001", "CT") and not m.is_id("CT-0001", "EQ")
    assert not m.is_id("CT-1") and not m.is_id("../CT-0001") and not m.is_id("")
    with pytest.raises(ValueError):
        m.format_id("F", 1)


def test_target_spec_from_plan_yaml_round_trip():
    t = m.TargetSpec.from_yaml(TARGET_YAML)
    assert t.kind == "version_upgrade" and t.target.build == "maven" and t.infra.port == 8080
    assert t.libraries.deny == ["log4j:log4j"] and t.equivalence.wire_compat == "strict" and t.budget_usd == 50
    _rt(t)
    assert m.TargetSpec.from_yaml(t.to_yaml()) == t
    with pytest.raises(ValidationError):
        m.TargetSpec.from_yaml(TARGET_YAML + "surprise: 1\n")
    with pytest.raises(ValidationError):
        m.TargetSpec.from_yaml(TARGET_YAML.replace("version_upgrade", "rewrite"))
    with pytest.raises(ValueError):
        m.TargetSpec.from_yaml("- a list")


def test_fit_report_and_risk():
    r = m.Risk(id="RISK-0001", kind="library_replacement", title="javax -> jakarta", item_ids=["EXT-0001"],
               evidence=[{"file": "pom.xml", "line_start": 3, "quote": "javax"}], needs_decision=True)
    fr = _rt(m.FitReport(graph_sha256=H, target_sha256=H, risks=[r], dropped_unverified=2))
    assert fr.risks[0].evidence[0].file == "pom.xml"
    with pytest.raises(ValidationError):
        m.Risk(id="R-1", kind="unknown", title="x")
    with pytest.raises(ValidationError):
        m.FitReport(graph_sha256="nothex")


def test_plan_and_slices():
    s1 = m.Slice(id="SLICE-0001", name="entities", kind="foundation", node_ids=["DE-0001"], order=0)
    s2 = m.Slice(id="SLICE-0002", name="loans", kind="entry", node_ids=["EP-0001"], depends_on=["SLICE-0001"],
                 order=1, risks=["RISK-0001"], estimate_usd=0.4, blockers=["F-0001"])
    plan = _rt(m.MigrationPlan(version=1, contract_graph_sha256=H, target_sha256=H, slices=[s1, s2],
                               excluded_nodes={"DC-0001": "dead_code"}))
    assert plan.slices[1].status == "planned"
    with pytest.raises(ValidationError, match="both"):
        m.MigrationPlan(version=1, slices=[s1, s2.model_copy(update={"node_ids": ["DE-0001"]})])
    with pytest.raises(ValidationError, match="unknown slice"):
        m.MigrationPlan(version=1, slices=[s2])
    with pytest.raises(ValidationError, match="duplicate"):
        m.MigrationPlan(version=1, slices=[s1, s1])
    with pytest.raises(ValidationError):
        m.Slice(id="SLICE-0003", name="x", kind="entry", order=0, status="done")


def test_characterization_case_plan_example():
    c = _rt(m.CharacterizationCase.model_validate(_case()))
    assert isinstance(c.request, m.HttpRequest) and c.expect.status == 409
    assert c.setup.stubs == ["EXT-0002/payment_ok"]


@pytest.mark.parametrize("kind,req,cls", [
    ("grpc", {"service": "Loans", "rpc": "Create", "message": {"a": 1}}, m.GrpcRequest),
    ("job", {"job": "nightly_overdue", "args": ["--dry"]}, m.JobRequest),
    ("message", {"topic": "loans.created", "payload": {"id": 1}}, m.MessageRequest),
    ("replay", {"method": "GET", "path": "/x"}, m.HttpRequest),
])
def test_case_kinds(kind, req, cls):
    c = _rt(m.CharacterizationCase.model_validate(_case(kind=kind, request=req, tags=["traffic"])))
    assert isinstance(c.request, cls)


def test_case_validation():
    with pytest.raises(ValidationError):
        m.CharacterizationCase.model_validate(_case(kind="job"))          # http request for a job
    with pytest.raises(ValidationError, match="specified"):
        m.CharacterizationCase.model_validate(_case(expect={"source": "specified", "status": 200}))
    ok = m.CharacterizationCase.model_validate(_case(decision="fix", decision_ref="COMP-0002",
                                                     expect={"source": "specified", "status": 200,
                                                             "legacy_observed": {"status": 409}}))
    assert ok.expect.legacy_observed == {"status": 409}
    with pytest.raises(ValidationError):
        m.CharacterizationCase.model_validate(_case(unknown_field=1))


def _job(**kw):
    d = {"run_id": "RUN-0001", "image": "maven@sha256:" + H, "command": ["mvn", "-q", "test"], "phase": "test",
         "mounts": [{"source": "target_code", "target": "/work"}], "env": {"PORT": "8080"},
         "synthetic_secrets": ["DB_PASSWORD"], "labels": {"slice": "SLICE-0001"}}
    d.update(kw)
    return m.ExecJob.model_validate(d)


def test_exec_job_and_result():
    j = _rt(_job())
    assert j.network == "none" and j.limits.timeout_s == 900 and j.mounts[0].read_only
    _rt(_job(phase="build", network="mirror"))
    r = _rt(m.ExecResult(run_id="RUN-0001", exit_code=0, duration_s=1.5, stdout_ref="runs/RUN-0001/logs/stdout.txt",
                         reports=["runs/RUN-0001/reports/junit.xml"], image_digest="sha256:" + H))
    assert r.ok and not m.ExecResult(run_id="RUN-0001", exit_code=None, error="no docker").ok
    assert not m.ExecResult(run_id="RUN-0001", exit_code=0, timed_out=True).ok


@pytest.mark.parametrize("kw,msg", [
    ({"network": "mirror"}, "no network"),
    ({"env": {"PASSWORD": "hunter2hunter2"}}, "secret"),
    ({"env": {"bad-name": "x"}}, "invalid env name"),
    ({"env": {"DB_PASSWORD": "x"}}, "both"),
    ({"mounts": [{"source": "/etc", "target": "/work"}]}, "project-relative"),
    ({"mounts": [{"source": "a/../../b", "target": "/work"}]}, "project-relative"),
    ({"mounts": [{"source": "a", "target": "work"}]}, "absolute"),
    ({"command": []}, "command"),
    ({"limits": {"memory_mb": 1}}, "memory_mb"),
    ({"workdir": "rel"}, "workdir"),
])
def test_exec_job_rejects(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        _job(**kw)


def test_equivalence_models():
    d1 = m.EquivalenceDiff(id="EQ-0001", case_id="CT-0001", field="body.$.total", legacy=1, new=2,
                           classification="regression", triage="new_bug", triage_quote="total")
    d2 = m.EquivalenceDiff(id="EQ-0002", case_id="CT-0002", field="status", legacy=409, new=200,
                           classification="intended_divergence", decision_ref="COMP-0002")
    rep = _rt(m.EquivalenceReport(slice="SLICE-0001", legacy_run="RUN-0001", new_run="RUN-0002", cases_total=4,
                                  cases_equivalent=3, diffs=[d1, d2], untestable={"RULE-0009": "needs prod data"}))
    assert rep.equivalence_pct == 75.0 and [d.id for d in rep.unexplained] == ["EQ-0001"]
    assert m.EquivalenceReport().equivalence_pct == 100.0
    with pytest.raises(ValidationError):
        m.EquivalenceDiff(id="EQ-0003", case_id="CT-0001", field="x", classification="bad")
    with pytest.raises(ValidationError):
        m.EquivalenceDiff(id="EQ-0003", case_id="CT-0001", field="x", classification="unknown", triage="maybe")


def test_approvals_findings_state_round_trip():
    _rt(m.SliceApproval(slice_id="SLICE-0001", approved_by="alice", approved_at="t", commit_sha="abc",
                        report_hashes={"slices/SLICE-0001/equivalence.json": H}, accepted_blockers=["F-0001"]))
    _rt(m.CutoverApproval(approved_by="alice", approved_at="t", contract_sha256=H, plan_sha256=H, tree_commit="abc",
                          equivalence_report_sha256=H, slice_ids=["SLICE-0001"]))
    _rt(m.RuntimeFindingsFile(findings=[m.RuntimeFinding(id="EQ-0004", kind="contradicts_rule", title="x",
                                                         item_ids=["RULE-0001"], case_ids=["CT-0001"],
                                                         run_id="RUN-0001")]))
    _rt(m.SliceState(slice_id="SLICE-0001"))
    with pytest.raises(ValidationError):
        m.SliceApproval(slice_id="SLICE-0001", approved_by="", approved_at="t")
    with pytest.raises(ValidationError):
        m.CutoverApproval(approved_by="a", approved_at="t", contract_sha256="x", plan_sha256=H, tree_commit="c",
                          equivalence_report_sha256=H)


# ------------------------------------------------------------------ ids.py
def test_allocator_sequence_keys_and_persistence(tmp_path):
    a = IdAllocator(tmp_path / "ids.json")
    assert a.allocate("RUN") == "RUN-0001" and a.allocate("RUN") == "RUN-0002"
    assert a.allocate("SLICE", key="entry:loans") == "SLICE-0001"
    assert a.allocate("SLICE", key="entry:books") == "SLICE-0002"
    assert a.allocate("SLICE", key="entry:loans") == "SLICE-0001"
    b = IdAllocator(tmp_path / "ids.json")
    assert b.lookup("SLICE", "entry:books") == "SLICE-0002" and b.last("RUN") == 2 and b.last("CT") == 0
    assert b.allocate_many("CT", ["z", "a", "z"]) == {"z": "CT-0002", "a": "CT-0001"}
    with pytest.raises(ValueError):
        b.allocate("F")
    (tmp_path / "ids.json").write_text(json.dumps({"counters": {"CT": 2}, "entries": {"CT": {"a": "CT-0001",
                                                                                                "b": "CT-0001"}}}))
    with pytest.raises(ValueError, match="duplicate"):
        b.allocate("CT")


def test_allocate_in_project_is_lazy_and_bumps_schema(tmp_path):
    p = Project.init(tmp_path / "p", source="s")
    lay = Phase2Layout(p)
    assert not lay.migration.exists()
    assert allocate(p, "EQ") == "EQ-0001"
    assert lay.ids_file.exists() and allocator(p).last("EQ") == 1


# ------------------------------------------------------------------ paths.py / project schema
def test_init_creates_no_phase2_dirs(tmp_path):
    p = Project.init(tmp_path / "p", source="s")
    assert p.meta().schema_version == 2
    assert not any((p.root / d).exists() for d in PHASE2_DIRS)


def test_v1_project_loads_and_is_bumped_on_first_phase2_write(tmp_path):
    p = Project.init(tmp_path / "p", source="s")
    meta = json.loads(p.meta_path.read_text())
    meta["schema_version"] = 1
    p.meta_path.write_text(json.dumps(meta))
    p = Project.load(p.root)
    assert p.meta().schema_version == 1
    lay = Phase2Layout(p)
    _ = (lay.target_yaml, lay.plan_json, lay.slice_state("SLICE-0001"), lay.run_meta("RUN-0001"))
    assert p.meta().schema_version == 1 and not lay.target_dir.exists()   # properties never create anything
    lay.ensure(lay.target_dir)
    assert lay.target_dir.is_dir() and p.meta().schema_version == 2
    assert ProjectMeta.model_validate_json(p.meta_path.read_text()).name == meta["name"]


def test_newer_project_schema_is_refused(tmp_path):
    p = Project.init(tmp_path / "p", source="s")
    meta = json.loads(p.meta_path.read_text())
    meta["schema_version"] = 99
    p.meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="newer"):
        Project.load(p.root)


def test_layout_paths(tmp_path):
    p = Project.init(tmp_path / "p", source="s")
    lay = Phase2Layout(p)
    assert lay.rel(lay.case_file("SLICE-0001", "CT-0002")) == "characterization/cases/SLICE-0001/CT-0002.json"
    assert lay.rel(lay.slice_approval("SLICE-0003")) == "slices/SLICE-0003/approval.json"
    assert lay.rel(lay.run_logs("RUN-0004")) == "runs/RUN-0004/logs"
    assert lay.rel(lay.plan_version(3)) == "plan/versions/v003.json"
    assert lay.rel(lay.contract_file) == "docs/migration_contract.json"
    assert lay.rel(lay.inputs_traffic) == "inputs/traffic"
    for bad in ("../x", "SLICE-1", "RUN-0001"):
        with pytest.raises(ValueError):
            lay.slice_dir(bad)
    with pytest.raises(ValueError):
        lay.ensure(tmp_path / "outside")
