"""Phase 2 CLI (migrate/cli.py): contract check, doctor, group registry; exec interface (migrate/exec/)."""
import json
import sys
import types

import click
import pytest
from click.testing import CliRunner

from migration_agent.audit import AuditLog, read_audit
from migration_agent.cli import main
from migration_agent.migrate import cli as p2cli
from migration_agent.migrate import exec as p2exec
from migration_agent.migrate.exec.base import ExecBackend, UnavailableBackend, load_run
from migration_agent.migrate.exec.fake import FakeExecBackend, FakeOutcome
from migration_agent.migrate.models import ExecJob
from migration_agent.migrate.paths import Phase2Layout

from phase2_helpers import approved_project, edit_contract


@pytest.fixture()
def proj(tmp_path, tmp_path_factory):
    return approved_project(tmp_path, tmp_path_factory)


def _run(*args):
    return CliRunner().invoke(main, [str(a) for a in args])


def test_groups_are_registered_on_main():
    for g in p2cli.GROUP_NAMES:
        assert isinstance(main.commands[g], click.Group)
    assert "doctor" in main.commands and {"check", "schema"} <= set(main.commands["contract"].commands)


def test_contract_check_ok_and_failing(proj):
    r = _run("contract", "check", proj.root)
    assert r.exit_code == 0 and "contract: OK" in r.output
    edit_contract(proj, coverage={"mapped": 1, "unmapped": 3, "excluded": 0, "complete": False})
    r = _run("contract", "check", proj.root)
    assert r.exit_code == 1 and "error: coverage incomplete" in r.output
    r = _run("contract", "check", proj.root, "--accept-unmapped", "--format", "json")
    assert r.exit_code == 0 and json.loads(r.output)["ok"] is True


def test_contract_check_pin(proj):
    from migration_agent.graph import load_latest, save_snapshot
    save_snapshot(proj.graph_dir, load_latest(proj.graph_dir))
    assert _run("contract", "check", proj.root).exit_code == 1
    r = _run("contract", "check", proj.root, "--pin-contract")
    assert r.exit_code == 0 and "warning: graph re-analysed" in r.output


def test_contract_schema_command(tmp_path):
    r = _run("contract", "schema")
    assert r.exit_code == 0 and "source_hash" in json.loads(r.output)["properties"]
    out = tmp_path / "schema.json"
    assert _run("contract", "schema", "--out", out).exit_code == 0 and out.exists()


def test_doctor_all_ok_with_fake_backend(proj, monkeypatch):
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: FakeExecBackend(project))
    monkeypatch.setattr(p2cli, "_git_version", lambda: (True, "git version 2.x"))
    lay = Phase2Layout(proj)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text("kind: version_upgrade\nsource: {language: java}\ntarget: {language: java}\n"
                               "budget_usd: 5\n")
    AuditLog(proj.audit_dir / "audit.jsonl").record("analyst", "llm_call", cost_usd=1.5)
    r = _run("doctor", proj.root)
    assert r.exit_code == 0, r.output
    assert "OK    contract: OK" in r.output and "exec sandbox: fake: available" in r.output
    assert "$3.5000 remaining of $5.0000" in r.output
    js = json.loads(_run("doctor", proj.root, "--format", "json").output)
    assert {c["name"]: c["status"] for c in js} == {"contract": "ok", "exec sandbox": "ok", "git": "ok",
                                                   "budget": "ok"}


def test_doctor_reports_failures(tmp_path, monkeypatch):
    from migration_agent.project import Project
    p = Project.init(tmp_path / "p", source="s")
    monkeypatch.setattr(p2cli, "_git_version", lambda: (False, "git not found on PATH"))
    monkeypatch.setattr(p2exec, "get_backend", lambda *a, **k: UnavailableBackend(None, "docker daemon not reachable"))
    r = _run("doctor", p.root)
    assert r.exit_code == 1
    assert "FAIL  contract" in r.output and "FAIL  exec sandbox" in r.output
    assert "FAIL  git" in r.output and "WARN  budget: no project budget" in r.output


def test_default_backend_reports_docker_reason_when_daemon_is_down(tmp_path, monkeypatch):
    from migration_agent.migrate.exec import docker as p2docker
    from migration_agent.project import Project
    p = Project.init(tmp_path / "p", source="s")
    b = p2exec.get_backend(p)
    assert isinstance(b, (p2docker.DockerBackend, UnavailableBackend))
    monkeypatch.setattr(b, "available", lambda: (False, "docker daemon not reachable"), raising=False)
    ok, reason = b.available()
    assert not ok and "docker" in reason.lower()


def test_submodule_registry(monkeypatch):
    calls = []
    mod = types.ModuleType("migration_agent.migrate.zz_demo")

    def register(groups):
        calls.append(groups)

        @groups.target.command("demo")
        def demo():
            click.echo("demo ran")

        @groups.main.command("demo-verb")
        def verb():
            click.echo("verb ran")

    mod.register = register
    monkeypatch.setitem(sys.modules, "migration_agent.migrate.zz_demo", mod)

    @click.group()
    def fresh():
        pass

    groups = p2cli.register(fresh, submodules=("zz_demo", "zz_does_not_exist"))
    assert groups.registered == ["zz_demo"] and calls == [groups] and groups["tests"] is groups.tests
    r = CliRunner().invoke(fresh, ["target", "demo"])
    assert r.exit_code == 0 and "demo ran" in r.output
    assert "verb ran" in CliRunner().invoke(fresh, ["demo-verb"]).output


def test_registry_does_not_swallow_inner_import_errors(monkeypatch):
    def boom(name):
        raise ModuleNotFoundError("No module named 'lxml'", name="lxml")

    monkeypatch.setattr(p2cli.importlib, "import_module", boom)
    with pytest.raises(ModuleNotFoundError, match="lxml"):
        p2cli.register(click.Group("x"), submodules=("fit",))


def test_registry_refuses_name_clash():
    g = click.Group("x")
    g.add_command(click.Command("target", callback=lambda: None))
    with pytest.raises(RuntimeError, match="already exists"):
        p2cli.register(g, submodules=())


# ------------------------------------------------------------------ exec interface
def _job(run_id="RUN-0001", **kw):
    d = dict(run_id=run_id, image="img@sha256:" + "a" * 64, command=["mvn", "test"], phase="test",
             labels={"slice": "SLICE-0001"})
    d.update(kw)
    return ExecJob(**d)


def test_fake_backend_writes_run_files(tmp_path):
    from migration_agent.project import Project
    p = Project.init(tmp_path / "p", source="s")
    secret = "ghp_" + "b" * 36
    fake = FakeExecBackend(p, script=[FakeOutcome(exit_code=0, stdout="BUILD OK token=" + secret),
                                      FakeOutcome(exit_code=1, stderr="x" * 2_000_000,
                                                  reports={"junit.xml": "<testsuite/>"})])
    assert isinstance(fake, ExecBackend) and fake.available() == (True, "")
    r1 = fake.run(_job())
    assert r1.ok and r1.stdout_ref == "runs/RUN-0001/logs/stdout.txt"
    out = (p.root / r1.stdout_ref).read_text()
    assert "BUILD OK" in out and secret not in out and secret not in r1.stdout_tail
    r2 = fake.run(_job("RUN-0002"))
    assert r2.exit_code == 1 and r2.truncated and r2.reports == ["runs/RUN-0002/reports/junit.xml"]
    assert len(r2.stderr_tail.encode()) <= 4096
    r3 = fake.run(_job("RUN-0003"))            # last scripted outcome repeats
    assert r3.exit_code == 1
    job, res = load_run(p, "RUN-0002")
    assert job == fake.jobs[1] and res == r2
    recs = [e for e in read_audit(p.audit_dir / "audit.jsonl") if e["action"] == "exec_run"]
    assert [e["run_id"] for e in recs] == ["RUN-0001", "RUN-0002", "RUN-0003"] and recs[0]["argv"] == ["mvn", "test"]


def test_fake_backend_unavailable_and_callable_script():
    fake = FakeExecBackend(available=(False, "no docker"))
    r = fake.run(_job())
    assert r.exit_code is None and r.error == "no docker" and not r.ok
    fake = FakeExecBackend(script=lambda job: FakeOutcome(exit_code=0 if job.phase == "build" else 3))
    assert fake.run(_job(phase="build", network="mirror")).exit_code == 0
    assert fake.run(_job()).exit_code == 3
