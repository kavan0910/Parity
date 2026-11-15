"""M6 schema compatibility (migrate/schemacompat.py): sandbox schema check with scripted exec results, static
diff, declared expand/contract changes, data mapping checks on synthetic fixtures, blind-spot blockers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.graph.models import Edge, Evidence, Graph, Node
from migration_agent.migrate import exec as p2exec
from migration_agent.migrate import schemacompat as S
from migration_agent.migrate.exec.fake import FakeExecBackend, FakeOutcome
from migration_agent.migrate.models import MigrationPlan, Slice, SourceStack, TargetSpec, TargetStack
from migration_agent.migrate.router import schema_probe
from migration_agent.migrate.target import write_target

from phase2_helpers import approved_project

DJANGO_MODELS = '''from django.db import models


class Workshop(models.Model):
    title = models.CharField(max_length=120)
    seats = models.PositiveIntegerField(default=10)
    starts_at = models.DateTimeField()


class Registration(models.Model):
    workshop = models.ForeignKey(Workshop, related_name="registrations", on_delete=models.CASCADE)
    email = models.EmailField()
'''
PROBE_OK = {"engine": "sqlite", "ok": True, "applied": [], "error": "", "triggers": [], "views": [], "tables": {
    "workshops_workshop": {"columns": {"id": {"type": "integer", "nullable": False, "pk": True},
                                       "title": {"type": "varchar(120)", "nullable": False, "pk": False},
                                       "seats": {"type": "integer unsigned", "nullable": False, "pk": False},
                                       "starts_at": {"type": "datetime", "nullable": False, "pk": False}}},
    "workshops_registration": {"columns": {"id": {"type": "integer", "nullable": False, "pk": True},
                                           "workshop_id": {"type": "bigint", "nullable": False, "pk": False},
                                           "email": {"type": "varchar(254)", "nullable": False, "pk": False},
                                           "waitlisted": {"type": "bool", "nullable": False, "pk": False}}},
    "django_migrations": {"columns": {"id": {"type": "integer"}}}}}
HARNESS = {"schema_version": 1, "name": "django-lite",
           "legacy": {"image": "ma-fixture/django-lite:legacy", "build": {"command": ["pip", "install"]},
                      "start": ["python", "manage.py", "runserver"], "port": 8000,
                      "synthetic_secrets": ["DJANGO_SECRET_KEY"]},
           "new": {"image": "ma-fixture/django-lite:new", "build": {"command": ["pip", "install"]},
                   "start": ["python", "manage.py", "runserver"], "port": 8000,
                   "synthetic_secrets": ["DJANGO_SECRET_KEY"]},
           "db": {"engine": "sqlite", "migrate": ["python", "manage.py", "migrate", "--noinput"]}}


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    p = approved_project(tmp_path, tmp_path_factory)
    write_target(p, TargetSpec(kind="version_upgrade",
                               source=SourceStack(language="python", framework="django@3.2"),
                               target=TargetStack(language="python", framework="django@5.0", build="pip")))
    return p


def _harness(p, **over) -> None:
    h = p.root / "harness"
    h.mkdir(parents=True, exist_ok=True)
    (h / "harness.yaml").write_text(yaml.safe_dump({**HARNESS, **over}))
    state = {}
    for role, run in (("legacy", "RUN-0001"), ("new", "RUN-0002")):
        ws = p.root / "runs" / run / "work" / "app"
        ws.mkdir(parents=True)
        (ws / "manage.py").write_text("# workspace copy\n")
        state[role] = {"ok": True, "workspace": f"runs/{run}/work/app", "image": HARNESS[role]["image"]}
    (h / "state.json").write_text(json.dumps(state))
    ids = p.root / "migration" / "ids.json"
    ids.parent.mkdir(parents=True, exist_ok=True)
    ids.write_text(json.dumps({"counters": {"RUN": 2}, "entries": {}}))


def _target_models(p, text=DJANGO_MODELS) -> None:
    f = p.root / "target_code" / "workshops" / "models.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def _fake(monkeypatch, p, *, probe=PROBE_OK, orm_rc=0, migrate_rc=0) -> FakeExecBackend:
    def script(job):
        stage = job.labels.get("stage")
        if stage == "legacy_migrate":
            return FakeOutcome(exit_code=migrate_rc, stderr="" if migrate_rc == 0 else "django.db.utils.Error")
        if stage == "introspect":
            return FakeOutcome(exit_code=0, stdout=json.dumps(probe), reports={"schema.json": json.dumps(probe)})
        if stage == "orm_check":
            return FakeOutcome(exit_code=orm_rc, stdout="No changes detected" if orm_rc == 0 else
                               "Migrations for 'workshops':\n  - Add field waitlisted")
        return FakeOutcome(exit_code=0)
    fb = FakeExecBackend(p, script=script)
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fb)
    return fb


# ------------------------------------------------------------------ sandbox check (scripted exec results)
def test_sqlite_sandbox_check_compatible(proj, monkeypatch):
    _harness(proj)
    _target_models(proj, DJANGO_MODELS.replace("    email = models.EmailField()\n",
                                               "    email = models.EmailField()\n"
                                               "    waitlisted = models.BooleanField(default=False)\n"))
    fb = _fake(monkeypatch, proj)
    r = S.check(proj)
    assert [j.labels["stage"] for j in fb.jobs] == ["legacy_migrate", "introspect", "orm_check"]
    for j in fb.jobs:
        assert j.phase == "test" and j.network == "none"
    mig, probe, orm = fb.jobs
    assert mig.image == "ma-fixture/django-lite:legacy" and mig.command == HARNESS["db"]["migrate"]
    assert "DJANGO_SECRET_KEY" in mig.synthetic_secrets and not mig.env.get("DJANGO_SECRET_KEY")
    assert probe.image == "ma-router:1" and probe.command[:4] == ["python", "/app/schema_probe.py", "--sqlite",
                                                                 "/work/app/db.sqlite3"]
    assert probe.mounts[0].source == mig.mounts[0].source and not probe.mounts[0].read_only
    assert orm.image == "ma-fixture/django-lite:new"
    assert orm.command == ["python", "manage.py", "makemigrations", "--check", "--dry-run"]
    assert r.status == "compatible", r.diffs
    assert r.legacy_source == "sandbox:sqlite" and r.orm_check.ok and r.orm_check.tool == "django-makemigrations-check"
    assert {d.kind for d in r.diffs} <= {"extra_table", "type_mismatch", "extra_column"}
    saved = json.loads((proj.root / "data" / "schema_compat.json").read_text())
    assert saved["status"] == "compatible" and saved["runs"] == r.runs
    assert (proj.root / "data" / "schema_compat.md").read_text().startswith("# Schema compatibility")


def test_missing_column_and_failed_orm_check_are_incompatible(proj, monkeypatch):
    _harness(proj)
    _target_models(proj, DJANGO_MODELS.replace("    email = models.EmailField()\n",
                                               "    email = models.EmailField()\n"
                                               "    phone = models.CharField(max_length=20)\n"))
    _fake(monkeypatch, proj, orm_rc=1)
    r = S.check(proj)
    assert r.status == "incompatible" and r.orm_check.ok is False
    miss = [d for d in r.blockers if d.kind == "missing_column"]
    assert [(d.table, d.column) for d in miss] == [("workshops_registration", "phone")]
    warn = [d for d in r.diffs if d.kind == "unmapped_not_null"]
    assert [(d.table, d.column) for d in warn] == [("workshops_registration", "waitlisted")]
    r2 = CliRunner().invoke(main, ["config", "schema-check", str(proj.root)])
    assert r2.exit_code == 1 and "incompatible" in r2.output


def test_failed_legacy_migration_is_an_error(proj, monkeypatch):
    _harness(proj)
    _fake(monkeypatch, proj, migrate_rc=1)
    r = S.check(proj)
    assert r.status == "error" and any("legacy migrations failed" in n for n in r.notes)


def test_declared_changes_apply_expand_sql_in_the_probe(proj, monkeypatch):
    _harness(proj)
    _target_models(proj)
    data = proj.root / "data"
    data.mkdir()
    (data / "schema_changes.yaml").write_text(yaml.safe_dump({"changes": [
        {"id": "SC-001", "kind": "add_column", "table": "workshops_workshop", "column": "room", "type": "VARCHAR(40)",
         "reason": "rooms"}]}))
    # static facts know no tables here, so declare them through the probe instead: validation uses facts only
    monkeypatch.setattr(S, "legacy_static_schema", lambda project, graph=None: S._tables_from_probe(PROBE_OK))
    probe = json.loads(json.dumps(PROBE_OK))
    probe["tables"]["workshops_workshop"]["columns"]["room"] = {"type": "VARCHAR(40)", "nullable": True}
    fb = _fake(monkeypatch, proj, probe=probe)
    r = S.check(proj)
    probe_job = [j for j in fb.jobs if j.labels["stage"] == "introspect"][0]
    assert probe_job.command[probe_job.command.index("--apply") + 1] == "/work/sql/expand.sql"
    sql = (proj.root / "runs" / probe_job.run_id / "work" / "sql" / "expand.sql").read_text()
    assert 'ALTER TABLE "workshops_workshop" ADD COLUMN "room" VARCHAR(40);' in sql
    assert r.changes[0].expand == ['ALTER TABLE "workshops_workshop" ADD COLUMN "room" VARCHAR(40);']
    assert r.legacy_tables["workshops_workshop"].columns["room"].type == "VARCHAR(40)"


def test_sql_migrations_without_migrate_command_and_app_created_schema(proj, monkeypatch):
    _harness(proj, db={"engine": "sqlite"}, schema_check={"sql_files": ["db/V1__init.sql"]})
    (proj.root / "legacy_src" / "db").mkdir(parents=True)
    (proj.root / "legacy_src" / "db" / "V1__init.sql").write_text("CREATE TABLE t (id INTEGER PRIMARY KEY);")
    fb = _fake(monkeypatch, proj)
    S.check(proj)
    probe = [j for j in fb.jobs if j.labels["stage"] == "introspect"][0]
    assert "/work/legacy/db/V1__init.sql" in probe.command
    assert any(m.source == "legacy_src" and m.read_only for m in probe.mounts)
    assert not any(j.labels["stage"] == "legacy_migrate" for j in fb.jobs)
    # neither migrate nor SQL: schema made by the app at start-up -> static view, honest note
    _harness_reset(proj, db={"engine": "sqlite"})
    fb = _fake(monkeypatch, proj)
    r = S.check(proj)
    assert [j.labels["stage"] for j in fb.jobs] == ["orm_check"] or fb.jobs == []
    assert r.status in ("unverified", "incompatible") and any("start-up" in n for n in r.notes)


def _harness_reset(p, **over):
    (p.root / "harness" / "harness.yaml").write_text(yaml.safe_dump({**HARNESS, **over}))


def test_schema_probe_script_on_a_real_sqlite_file(tmp_path):
    sql = tmp_path / "V1__init.sql"
    sql.write_text("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL, isbn VARCHAR(13));\n"
                   "CREATE TRIGGER trg AFTER INSERT ON book BEGIN SELECT 1; END;\n")
    bad = tmp_path / "V2__bad.sql"
    bad.write_text("ALTER TABLE nope ADD COLUMN x INT;")
    rc, out = schema_probe.run(str(tmp_path / "db.sqlite3"), [str(sql)])
    assert rc == 0 and out["ok"] and out["triggers"] == [{"name": "trg", "table": "book"}]
    t = S._tables_from_probe(out)["book"]
    assert t.columns["title"].nullable is False and t.columns["id"].pk and t.columns["isbn"].type == "VARCHAR(13)"
    rc, out = schema_probe.run(str(tmp_path / "db2.sqlite3"), [str(sql), str(bad)])
    assert rc == 2 and not out["ok"] and "nope" in out["error"] and "book" in out["tables"]


def test_postgres_path_uses_an_ephemeral_db_service_and_tears_down(proj, monkeypatch):
    _harness(proj, db={"engine": "postgres", "image": "postgres:16.4-alpine", "command": ["postgres"],
                       "synthetic_secrets": ["POSTGRES_PASSWORD"], "migrate": ["python", "manage.py", "migrate"]})
    _target_models(proj)

    class PgFake(FakeExecBackend):
        def __init__(self, project):
            super().__init__(project, script=lambda job: FakeOutcome(exit_code=0))
            self.started, self.execs, self.removed, self.kw = [], [], [], []

        def ensure_internal_network(self, suffix):
            return f"ma-int-test-{suffix}"

        def start_service(self, job, *, network, aliases=(), secret_values=None, extra_tmpfs=()):
            self.started.append((job, network, list(aliases), dict(secret_values or {})))
            return f"ma-test-{job.run_id.lower()}-db"

        def exec_in(self, container, argv, timeout=30):
            self.execs.append(argv)
            if "information_schema" in argv[-1]:
                return 0, ("workshops_workshop\tid\tinteger\tNO\nworkshops_workshop\ttitle\tcharacter varying\tNO\n"
                           "workshops_workshop\tseats\tinteger\tNO\nworkshops_workshop\tstarts_at\ttimestamp\tNO\n"
                           "workshops_registration\tid\tinteger\tNO\nworkshops_registration\tworkshop_id\tbigint\tNO\n"
                           "workshops_registration\temail\tcharacter varying\tNO\n")
            return 0, ""

        def run(self, job, **kw):
            self.kw.append(kw)
            return super().run(job)

        def remove_containers(self, names):
            self.removed += list(names)

        def remove_network(self, name):
            self.removed.append(name)

    fb = PgFake(proj)
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fb)
    r = S.check(proj)
    job, net, aliases, secrets = fb.started[0]
    assert job.image == "postgres:16.4-alpine" and aliases == ["db"] and set(secrets) == {"POSTGRES_PASSWORD"}
    assert job.phase == "test" and job.network == "none"
    assert fb.execs[-1][0] == "psql" and "-At" in fb.execs[-1]
    assert all(k.get("internal_network") == net for k in fb.kw)          # migrate + ORM check on the db network
    assert [j.labels.get("stage") for j in fb.jobs] == ["migrate", "orm_check"]
    assert net in fb.removed and fb.started and any(c.endswith("-db") for c in fb.removed)
    assert r.legacy_source == "sandbox:postgres" and r.status == "compatible", r.diffs


def test_static_mode_and_unavailable_sandbox(proj, monkeypatch):
    r = S.check(proj, static=True)
    assert r.mode == "static" and r.status == "skipped" and r.orm_check.skipped == ""
    with pytest.raises(S.SchemaCompatError, match="--static"):
        S.check(proj)
    _harness(proj)
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: FakeExecBackend(proj, available=(False, "no daemon")))
    from migration_agent.migrate.harness import HarnessError
    with pytest.raises(HarnessError, match="no daemon"):
        S.check(proj)
    _target_models(proj)
    r = S.check(proj, static=True)
    assert r.status == "incompatible" and {d.kind for d in r.blockers} == {"missing_table"}
    res = CliRunner().invoke(main, ["config", "schema-check", str(proj.root), "--static", "--format", "json"])
    assert res.exit_code == 1 and json.loads(res.output)["status"] == "incompatible"


# ------------------------------------------------------------------ declared changes and mappings
def test_schema_changes_spec_validation(proj):
    data = proj.root / "data"
    data.mkdir()
    f = data / "schema_changes.yaml"
    bad = [
        {"id": "SC-001", "kind": "add_column", "table": "users; DROP TABLE x", "column": "a", "type": "INT",
         "reason": "r"},
        {"id": "SC-001", "kind": "add_column", "table": "users", "type": "INT", "reason": "r"},
        {"id": "SC-001", "kind": "add_column", "table": "users", "column": "a", "type": "INT); DROP", "reason": "r"},
        {"id": "SC-001", "kind": "add_column", "table": "users", "column": "a", "type": "INT",
         "default": "1; DROP TABLE users", "reason": "r"},
        {"id": "SC-001", "kind": "rename_column", "table": "users", "column": "a", "type": "INT", "reason": "r"},
        {"id": "SC-001", "kind": "add_table", "table": "t", "reason": "r"},
        {"id": "X-1", "kind": "drop_table", "table": "t", "reason": "r"},
        {"id": "SC-001", "kind": "drop_table", "table": "t", "reason": ""},
    ]
    for b in bad:
        f.write_text(yaml.safe_dump({"changes": [b]}))
        with pytest.raises(S.SchemaCompatError, match="invalid"):
            S.load_schema_changes(proj)
    f.write_text(yaml.safe_dump({"changes": [{"id": "SC-001", "kind": "drop_table", "table": "t", "reason": "r"}] * 2}))
    with pytest.raises(S.SchemaCompatError, match="duplicate"):
        S.load_schema_changes(proj)
    f.write_text(yaml.safe_dump({"schema_export": ["../../etc/passwd"]}))
    with pytest.raises(S.SchemaCompatError, match="under data/"):
        S.load_schema_changes(proj)

    known = {"members": S.TableInfo(columns={"mail": S.ColumnInfo(type="text"), "id": S.ColumnInfo(type="int")})}
    ch = [S.SchemaChange(id="SC-001", kind="rename_column", table="members", column="mail", new_name="email",
                         type="VARCHAR(200)", reason="naming"),
          S.SchemaChange(id="SC-002", kind="add_column", table="members", column="mail", type="TEXT", reason="dup"),
          S.SchemaChange(id="SC-003", kind="drop_column", table="ghost", column="x", reason="r"),
          S.SchemaChange(id="SC-004", kind="add_table", table="members", columns={"id": "INT"}, reason="r")]
    errs = S.validate_changes(ch, known)
    assert errs == ["SC-002: members.mail already exists", "SC-003: unknown table ghost",
                    "SC-004: table members already exists"]
    m = S.DataMapping.model_validate({"id": "m1", "table": "members", "from": "mail", "to": "email",
                                      "transform": "lower", "change": "SC-001"})
    specs = S.change_specs(ch[:1], {"SLICE-0002": {"members"}, "SLICE-0003": {"loans"}}, [m])
    s = specs[0]
    assert s.expand == ['ALTER TABLE "members" ADD COLUMN "email" VARCHAR(200);']
    assert s.backfill == ['UPDATE "members" SET "email" = LOWER("mail") WHERE "email" IS NULL;']
    assert s.contract == ['ALTER TABLE "members" DROP COLUMN "mail";'] and s.contract_after == ["SLICE-0002"]
    assert "dual-write" in s.cutover_step
    after = S.apply_expand(known, ch[:1])
    assert "email" in after["members"].columns and "mail" in after["members"].columns   # expand keeps both


def test_mapping_checks_on_synthetic_fixtures(proj):
    with pytest.raises(Exception):
        S.MappingFile.model_validate({"mappings": []})                  # synthetic confirmation required
    mf = S.MappingFile.model_validate({"fixtures_are_synthetic": True, "mappings": [
        {"id": "email-lower", "table": "members", "from": "mail", "to": "email", "transform": "lower",
         "fixtures": [{"in": {"mail": "A@X.TEST"}, "out": "a@x.test"}, {"in": {"mail": None}, "out": None}]},
        {"id": "price", "table": "books", "from": "price_cents", "to": "price", "transform": "cents_to_units",
         "fixtures": [{"in": {"price_cents": 1999}, "out": 19.99}, {"in": {"price_cents": 5}, "out": 0.5}]},
        {"id": "name", "table": "members", "from": ["first", "last"], "to": "full_name", "transform": "concat",
         "params": {"sep": " "}, "fixtures": [{"in": {"first": "Ada", "last": "L"}, "out": "Ada L"}]},
        {"id": "tok", "table": "members", "from": "t", "to": "t2",
         "fixtures": [{"in": {"t": "ghp_" + "a" * 36}, "out": "x"}]},
        {"id": "none", "table": "members", "from": "a", "to": "b", "transform": "to_int"}]})
    res = {m.id: m for m in S.run_mapping_checks(mf)}
    assert res["email-lower"].status == "passed" and res["email-lower"].sql == 'LOWER("mail")'
    assert res["price"].status == "failed" and res["price"].passed == 1 and "expected 0.5" in res["price"].failures[0]
    assert res["name"].status == "passed" and res["name"].sql == "\"first\" || ' ' || \"last\""
    assert res["tok"].status == "failed" and "synthetic" in res["tok"].failures[0]
    assert res["none"].status == "untested"
    with pytest.raises(Exception, match="exactly one"):
        S.DataMapping.model_validate({"id": "x", "table": "t", "from": ["a", "b"], "to": "c", "transform": "lower"})
    with pytest.raises(Exception, match="sep"):
        S.DataMapping.model_validate({"id": "x", "table": "t", "from": ["a", "b"], "to": "c", "transform": "concat",
                                      "params": {"sep": "'; DROP"}})


def test_failed_mapping_makes_the_report_incompatible(proj):
    data = proj.root / "data"
    data.mkdir()
    (data / "mapping.yaml").write_text(yaml.safe_dump({"fixtures_are_synthetic": True, "mappings": [
        {"id": "m", "table": "members", "from": "a", "to": "b", "transform": "upper",
         "fixtures": [{"in": {"a": "x"}, "out": "x"}]}]}))
    r = S.check(proj, static=True)
    assert r.status == "invalid" and r.validation_errors == ["mapping m: unknown table members"]


# ------------------------------------------------------------------ static scanners and diff
def test_jpa_and_django_scanners_and_diff():
    jpa = '''@Entity
@Table(name = "loans")
public class Loan {
    @Id @GeneratedValue
    private Long id;
    @ManyToOne
    private Book book;
    @Column(name = "due_on", nullable = false)
    private LocalDate dueDate;
    @Transient
    private String cached;
    @OneToMany(mappedBy = "loan")
    private List<Fine> fines;
    private static final int MAX = 3;
    private BigDecimal feeAmount;
}
'''
    t = S.scan_jpa(jpa)["loans"]
    assert set(t.columns) == {"id", "book_id", "due_on", "fee_amount"} and t.columns["id"].pk
    d = S.scan_django_models("workshops/models.py", DJANGO_MODELS)
    assert set(d) == {"workshops_workshop", "workshops_registration"}
    assert set(d["workshops_registration"].columns) == {"id", "workshop_id", "email"}
    legacy = {"loans": S.TableInfo(columns={"id": S.ColumnInfo(type="bigint", pk=True),
                                            "book_id": S.ColumnInfo(type="bigint"),
                                            "due_on": S.ColumnInfo(type="varchar(10)"),
                                            "fee_amount": S.ColumnInfo(type="numeric(8,2)"),
                                            "branch": S.ColumnInfo(type="int", nullable=False)}),
              "flyway_schema_history": S.TableInfo()}
    diffs = S.diff_schemas(legacy, {"loans": t, "fines": S.TableInfo(columns={"id": S.ColumnInfo()})})
    kinds = {(x.kind, x.table, x.column, x.severity) for x in diffs}
    assert ("missing_table", "fines", "", "blocker") in kinds
    assert ("type_mismatch", "loans", "due_on", "warning") in kinds
    assert ("unmapped_not_null", "loans", "branch", "warning") in kinds
    assert not any(x.table == "flyway_schema_history" for x in diffs)


# ------------------------------------------------------------------ blind spots -> slice blockers
def _blind_spot_world(monkeypatch):
    g = Graph(version=1, nodes=[
        Node(id="COMP-0001", type="component", name="repo", module="src/repo.js",
             evidence=[Evidence(file="src/repo.js", line_start=1)]),
        Node(id="COMP-0002", type="component", name="ledger", module="src/ledger.js"),
        Node(id="COMP-0003", type="component", name="other", module="src/other.js"),
        Node(id="ENT-0001", type="data_entity", name="public.Ledger", module="db/schema.sql")],
        edges=[Edge(id="EDGE-0001", source="COMP-0002", target="ENT-0001", type="writes")])
    plan = MigrationPlan(version=1, slices=[
        Slice(id="SLICE-0001", name="repo", kind="foundation", node_ids=["COMP-0001"], order=0),
        Slice(id="SLICE-0002", name="ledger", kind="entry", node_ids=["COMP-0002", "ENT-0001"], order=1),
        Slice(id="SLICE-0003", name="other", kind="entry", node_ids=["COMP-0003"], order=2)])
    monkeypatch.setattr(S, "_plan_graph", lambda project: (plan, g))


def test_schema_blockers(proj, monkeypatch):
    _blind_spot_world(monkeypatch)
    src = proj.code / "src"
    src.mkdir(exist_ok=True)
    (src / "repo.js").write_text("module.exports = db => db.query('CALL refresh_totals(?)', [1]);\n"
                                 "const ok = db.query('CALL local_proc()');\n")
    (proj.code / "db").mkdir()
    (proj.code / "db" / "procs.sql").write_text("CREATE OR REPLACE PROCEDURE local_proc() AS $$ $$;\n"
                                                "ALTER TABLE ledger DISABLE TRIGGER trg_ledger_audit;\n")
    b = S.schema_blockers(proj)
    assert set(b) == {"SLICE-0001", "SLICE-0002"}
    assert "refresh_totals" in b["SLICE-0001"][0] and "src/repo.js:1" in b["SLICE-0001"][0]
    assert not any("local_proc" in r for r in b["SLICE-0001"])          # defined in the repo
    assert any("trg_ledger_audit" in r for r in b["SLICE-0002"])         # table touched through a writes edge
    spots = S.find_blind_spots(proj)
    assert [p["name"] for p in spots["procedures"]] == ["refresh_totals"]

    data = proj.root / "data"
    data.mkdir()
    (data / "prod_schema.sql").write_text("CREATE PROCEDURE refresh_totals() BEGIN END;\n"
                                          "CREATE TRIGGER trg_ledger_audit AFTER INSERT ON ledger FOR EACH ROW BEGIN END;\n")
    (data / "schema_changes.yaml").write_text(yaml.safe_dump({"schema_export": ["data/prod_schema.sql"]}))
    assert S.schema_blockers(proj) == {}
    (data / "schema_changes.yaml").write_text(yaml.safe_dump({
        "blind_spots": [{"table": "ledger", "reason": "partitioning job in prod only"}],
        "accepted": [{"name": "refresh_totals", "reason": "no-op in the sandbox", "by": "alice"},
                     {"name": "trg_ledger_audit", "reason": "audit only"}]}))
    b = S.schema_blockers(proj)
    assert b == {"SLICE-0002": ["declared blind spot on table ledger: partitioning job in prod only"]}
    rep = S.SchemaCompatReport(status="incompatible", diffs=[S.SchemaDiff(kind="missing_column", table="ledger",
                                                                          column="x", severity="blocker")])
    (data / "schema_changes.yaml").write_text(yaml.safe_dump({"schema_export": ["data/prod_schema.sql"]}))
    (data / "schema_compat.json").write_text(rep.model_dump_json())
    assert S.schema_blockers(proj) == {"SLICE-0002": ["schema-compat missing_column ledger.x"]}


def test_schema_blockers_without_plan_and_cli(proj):
    assert S.schema_blockers(proj) == {}
    r = CliRunner().invoke(main, ["config", "schema-blockers", str(proj.root)])
    assert r.exit_code == 0 and "no schema blockers" in r.output
    r = CliRunner().invoke(main, ["config", "--help"])
    assert "schema-check" in r.output and "schema-blockers" in r.output
