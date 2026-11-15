"""migrate/harness.py: legacy_src import (hash-verified), jail, compose generation, build/up/down via DockerBackend
with a scripted docker CLI, and the CLI commands."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import exec as p2exec
from migration_agent.migrate import harness as H
from migration_agent.project import Project
from migration_agent.sandbox.tools import ReadOnlyTools, SandboxViolation

from p2_exec_helpers import FakeDocker, flag_values
from phase2_helpers import approved_project

REPOS = Path(__file__).parent.parent / "eval" / "repos"
EXPRESS = REPOS / "express-crud"


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    return approved_project(tmp_path, tmp_path_factory)


def _fake(monkeypatch, **kw) -> FakeDocker:
    fd = FakeDocker(**kw)
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fd.backend(project))
    return fd


def _cli(*args):
    return CliRunner().invoke(main, [str(a) for a in args])


# ------------------------------------------------------------------ import-source
def test_import_from_original_is_verified_and_unredacted(proj):
    m = H.import_source(proj, from_original=True)
    contract = json.loads((proj.docs / "migration_contract.json").read_text())
    assert m["verified"] and m["source_hash"] == contract["source_hash"] and contract["source_hash"]
    ls = proj.root / "legacy_src"
    assert (ls / "src" / "server.js").read_bytes() == (EXPRESS / "src" / "server.js").read_bytes()
    # ingest redacted the template token in code/; legacy_src keeps the original bytes for the build
    assert "changeme" not in (proj.code / ".env.example").read_text()
    assert (ls / ".env.example").read_text() == (EXPRESS / ".env.example").read_text()
    assert (ls / "harness" / "harness.yaml.in").exists()
    assert json.loads((proj.root / "harness" / "legacy_src.json").read_text())["verified"] is True
    audit = (proj.root / "audit" / "audit.jsonl").read_text()
    assert '"import_source"' in audit
    assert json.loads((proj.root / "project.json").read_text())["schema_version"] == 2


def test_import_refuses_a_different_source(proj, tmp_path):
    other = tmp_path / "tampered"
    shutil.copytree(EXPRESS, other)
    (other / "src" / "server.js").write_text("require('child_process').exec('curl evil')\n")
    with pytest.raises(H.HarnessError, match="hash mismatch"):
        H.import_source(proj, other)
    assert not (proj.root / "legacy_src").exists()
    assert '"import_source_refused"' in (proj.root / "audit" / "audit.jsonl").read_text()


def test_import_withholds_credential_files_and_needs_force(proj, tmp_path):
    src = tmp_path / "with-creds"
    shutil.copytree(EXPRESS, src)
    (src / ".env").write_text("API_TOKEN=real-production-token-123\n")   # withheld by ingest: hash unchanged
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "node_modules" / "x" / "index.js").write_text("1")
    m = H.import_source(proj, src)
    assert m["verified"] and ".env" in m["withheld"]
    assert (proj.root / "legacy_src" / ".env").read_bytes() == b""          # placeholder, never the value
    assert not (proj.root / "legacy_src" / "node_modules").exists()
    with pytest.raises(H.HarnessError, match="--force"):
        H.import_source(proj, src)
    assert H.import_source(proj, src, force=True)["verified"]


def test_import_without_source_hash_needs_accept_unpinned(proj):
    from phase2_helpers import edit_contract
    edit_contract(proj, source_hash="")
    with pytest.raises(H.HarnessError, match="accept-unpinned"):
        H.import_source(proj, from_original=True)
    assert H.import_source(proj, from_original=True, accept_unpinned=True)["verified"] is False


def test_import_rejects_git_url_and_both_options(proj):
    meta = json.loads(proj.meta_path.read_text())
    meta["source"] = "https://github.com/acme/legacy.git"
    proj.meta_path.write_text(json.dumps(meta))
    with pytest.raises(H.HarnessError, match="git URL"):
        H.import_source(proj, from_original=True)
    with pytest.raises(H.HarnessError, match="exactly one"):
        H.import_source(proj, EXPRESS, from_original=True)


def test_legacy_src_is_unreachable_through_readonly_tools(proj, tmp_path):
    H.import_source(proj, from_original=True)
    target = proj.root / "legacy_src" / ".env.example"
    assert "changeme" in target.read_text()
    t = ReadOnlyTools(proj.code)
    for path in ("../legacy_src/.env.example", str(target), "legacy_src/.env.example",
                 "src/../../legacy_src/.env.example"):
        with pytest.raises(SandboxViolation):
            t.read_file(path)
    for fn in (t.list_dir, t.file_stat):
        with pytest.raises(SandboxViolation):
            fn("../legacy_src")
    # a symlink planted inside code/ that points into legacy_src is not followed out of the jail
    link = proj.code / "peek"
    link.symlink_to(proj.root / "legacy_src")
    with pytest.raises(SandboxViolation):
        t.read_file("peek/.env.example")
    hits = t.grep("changeme", ".")
    assert all(not m.path.startswith("peek") for m in hits.matches)
    assert not any("changeme" in m.text for m in hits.matches)


# ------------------------------------------------------------------ spec + compose
@pytest.mark.parametrize("repo", ["express-crud", "django-lite", "spring-lite"])
def test_fixture_specs_parse_and_render_locked_down_compose(repo):
    spec = H.HarnessSpec.model_validate(yaml.safe_load((REPOS / repo / "harness" / "harness.yaml.in").read_text()))
    assert (REPOS / repo / "harness" / spec.legacy.dockerfile.split("/", 1)[1]).exists()
    df = (REPOS / repo / "harness" / "Dockerfile").read_text()
    assert "TODO" in df and "@sha256" in df and "USER 10001:10001" in df
    assert not any(line.startswith("RUN ") for line in df.splitlines())        # builds with --network none
    doc = H.render_compose(None, spec, "legacy", "runs/RUN-0001/work/app")
    assert doc["networks"] == {"internal": {"internal": True}}
    for name, svc in doc["services"].items():
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert svc["security_opt"] == ["no-new-privileges:true"] and svc["pull_policy"] == "never"
        assert svc["user"] != "0" and "ports" not in svc and "privileged" not in svc
        assert all("docker.sock" not in v for v in svc.get("volumes", []))
        assert svc["networks"] == ["internal"]
    legacy = doc["services"]["legacy"]
    assert legacy["volumes"] == [f"../runs/RUN-0001/work/app:{spec.legacy.workdir}"]
    assert legacy["entrypoint"] + legacy["command"] == spec.legacy.start
    for k in spec.legacy.synthetic_secrets:
        assert legacy["environment"][k].startswith("${" + k + ":?")
    if spec.legacy.healthcheck.command:
        assert "{port}" not in json.dumps(legacy["healthcheck"])


def test_compose_with_postgres_db_and_stubs():
    spec = H.HarnessSpec.model_validate({
        "name": "svc", "legacy": {"image": "x:1", "build": {"command": ["make"]}, "start": ["./run"], "port": 80,
                                  "synthetic_secrets": ["DB_PASSWORD"]},
        "db": {"engine": "postgres", "image": "postgres:16.4-alpine", "command": ["docker-entrypoint.sh", "postgres"],
               "env": {"PGDATA": "/tmp/pg"}, "synthetic_secrets": ["POSTGRES_PASSWORD"],
               "tmpfs": ["/var/run/postgresql"]},
        "stubs": [{"name": "payments", "image": "stub:1", "command": ["stub", "--port", "9000"], "port": 9000}]})
    doc = H.render_compose(None, spec, "legacy", "runs/RUN-0002/work/app")
    assert set(doc["services"]) == {"db", "payments", "legacy"}
    assert doc["services"]["legacy"]["depends_on"] == ["db", "payments"]
    assert doc["services"]["db"]["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert "/var/run/postgresql:rw,nosuid,nodev" in doc["services"]["db"]["tmpfs"]
    assert doc["x-synthetic-secrets"] == ["DB_PASSWORD", "POSTGRES_PASSWORD"]


def test_spec_rejects_escapes_and_composite_new_are_explained():
    base = {"name": "s", "legacy": {"image": "x:1", "build": {"command": ["make"]}, "start": ["run"], "port": 1}}
    for bad in ({"dockerfile": "../../etc/Dockerfile"}, {"context": "/"},
                {"build": {"command": ["make"], "cache": "../outside"}}, {"start": []}):
        with pytest.raises(Exception):
            H.HarnessSpec.model_validate({**base, "legacy": {**base["legacy"], **bad}})
    spec = H.HarnessSpec.model_validate(base)
    with pytest.raises(H.HarnessError, match="M6"):
        spec.service("composite")
    with pytest.raises(H.HarnessError, match="M4"):
        spec.service("new")


# ------------------------------------------------------------------ build / up / down with a scripted docker
def test_build_up_down_flow(proj, monkeypatch):
    fd = _fake(monkeypatch)
    H.import_source(proj, from_original=True)
    ent = H.build(proj, "legacy")
    assert ent["ok"] and ent["build_run"] == "RUN-0001" and ent["workspace"] == "runs/RUN-0001/work/app"
    # image built from the fixture Dockerfile with no network and no pull
    b = [c for c in fd.calls if c[0] == "build"][0]
    assert b[b.index("--network") + 1] == "none" and "--pull=false" in b and "ma-fixture/express-crud:legacy" in b
    assert b[b.index("-f") + 1].endswith("legacy_src/harness/Dockerfile")
    # the build job: phase build, workspace copy rw, npm cache mounted, offline (no mirror hosts configured)
    a = fd.run_argv()
    assert flag_values(a, "--network") == ["none"] and "ma.phase=build" in flag_values(a, "--label")
    mounts = flag_values(a, "--mount")
    assert any(m.endswith("runs/RUN-0001/work/app,target=/work/app") for m in mounts)
    assert any(m.endswith("harness/cache/npm,target=/cache") for m in mounts)
    assert not any("legacy_src" in m for m in mounts)                   # the build works on a copy
    assert (proj.root / "runs/RUN-0001/work/app/src/server.js").exists()
    assert (proj.root / "harness" / "harness.yaml").read_text().startswith("# copied from legacy_src/harness/")
    compose = yaml.safe_load((proj.root / "harness" / "compose.legacy.yml").read_text())
    assert compose["services"]["legacy"]["volumes"] == ["../runs/RUN-0001/work/app:/work/app"]
    st = json.loads((proj.root / "harness" / "state.json").read_text())
    assert st["legacy"]["ok"] and st["legacy"]["image_digest"].startswith("sha256:")

    n_before = len(fd.calls)
    u = H.up(proj, "legacy", sleep=lambda s: None)["up"]
    calls = fd.calls[n_before:]
    net = [c for c in calls if c[:2] == ["network", "create"]][0]
    assert "--internal" in net and net[-1] == u["network"]
    svc = [c for c in calls if c[0] == "run" and "--detach" in c][-1]
    assert flag_values(svc, "--network") == [u["network"]] and "legacy" in flag_values(svc, "--network-alias")
    assert "API_TOKEN" in flag_values(svc, "-e") and "--read-only" in svc and "--rm" in svc
    health = [c for c in calls if c[0] == "exec"][0]
    assert health[1] == u["service"] and health[2] == "node" and "http://127.0.0.1:3000/api/products" in health[4]
    secrets = proj.root / "harness" / "synthetic_secrets.json"
    assert oct(secrets.stat().st_mode & 0o777) == "0o600"
    assert json.loads(secrets.read_text())["legacy"]["values"]["API_TOKEN"]
    with pytest.raises(H.HarnessError, match="already up"):
        H.up(proj, "legacy", sleep=lambda s: None)

    r = H.down(proj, "legacy")
    assert u["service"] in r["removed"] and ["network", "rm", u["network"]] in fd.calls
    assert "up" not in json.loads((proj.root / "harness" / "state.json").read_text())["legacy"]
    assert not secrets.exists()


def test_up_runs_migrations_and_seeds_on_the_internal_network(proj, monkeypatch, tmp_path):
    fd = _fake(monkeypatch)
    (proj.root / "legacy_src").mkdir()
    shutil.copytree(REPOS / "django-lite", proj.root / "legacy_src", dirs_exist_ok=True)
    assert H.build(proj, "legacy")["ok"]
    n = len(fd.calls)
    H.up(proj, "legacy", sleep=lambda s: None)
    jobs = [c for c, k in zip(fd.calls[n:], fd.kwargs[n:]) if c[0] == "run" and "stdout" in k]
    assert len(jobs) == 2                                              # migrate + one seed
    for j in jobs:
        assert flag_values(j, "--network")[0].startswith("ma-int-") and "ma.phase=test" in flag_values(j, "--label")
        assert "DJANGO_SECRET_KEY" in flag_values(j, "-e")
    assert jobs[0][-4:] == ["manage.py", "migrate", "--run-syncdb", "--noinput"]
    assert jobs[1][-2:] == ["loaddata", "harness/seed/workshops.json"]


def test_failed_health_check_tears_everything_down(proj, monkeypatch):
    fd = _fake(monkeypatch, exec_rc=1)
    H.import_source(proj, from_original=True)
    H.build(proj, "legacy")
    t = iter(range(0, 10_000, 50))
    with pytest.raises(H.HarnessError, match="health check failed"):
        H.up(proj, "legacy", sleep=lambda s: None, clock=lambda: next(t))
    assert any(c[0] == "rm" for c in fd.calls) and any(c[:2] == ["network", "rm"] for c in fd.calls)
    assert "up" not in json.loads((proj.root / "harness" / "state.json").read_text())["legacy"]


def test_build_needs_legacy_src_and_available_sandbox(proj, monkeypatch):
    _fake(monkeypatch)
    with pytest.raises(H.HarnessError, match="spec|import-source"):
        H.build(proj, "legacy")
    H.import_source(proj, from_original=True)
    _fake(monkeypatch, daemon=False)
    with pytest.raises(H.HarnessError, match="daemon not reachable"):
        H.build(proj, "legacy")


def test_up_requires_successful_build(proj, monkeypatch):
    _fake(monkeypatch, rc=1, stderr=b"npm ERR! network\n")
    H.import_source(proj, from_original=True)
    assert H.build(proj, "legacy")["ok"] is False
    with pytest.raises(H.HarnessError, match="no successful legacy build"):
        H.up(proj, "legacy")


# ------------------------------------------------------------------ CLI
def test_cli_commands_registered_and_flow(proj, monkeypatch):
    r = _cli("harness", "--help")
    for c in ("import-source", "build", "up", "down", "status"):
        assert c in r.output
    r = _cli("harness", "import-source", proj.root, "--from-original")
    assert r.exit_code == 0 and "verified" in r.output, r.output
    r = _cli("harness", "status", proj.root, "--format", "json")
    assert r.exit_code == 0
    s = json.loads(r.output)
    assert s["legacy_src"] and s["spec"] and s["build"] is None
    fd = _fake(monkeypatch)
    r = _cli("harness", "build", proj.root, "--legacy")
    assert r.exit_code == 0 and "OK" in r.output, r.output
    r = _cli("harness", "up", proj.root)
    assert r.exit_code == 0 and "ma-int-" in r.output, r.output
    fd.ps = [{"Names": "x", "State": "running", "Status": "Up"}]
    r = _cli("harness", "status", proj.root)
    assert r.exit_code == 0 and "ma-int-" in r.output and "x: running" in r.output
    assert _cli("harness", "down", proj.root).exit_code == 0
    r = _cli("harness", "build", proj.root, "--composite")
    assert r.exit_code != 0 and "M6" in r.output
    r = _cli("harness", "build", proj.root, "--new")
    assert r.exit_code != 0 and "M4" in r.output


def test_cli_status_without_docker_reports_unavailable(proj, monkeypatch):
    _fake(monkeypatch, daemon=False)
    r = _cli("harness", "status", proj.root)
    assert r.exit_code == 0 and "daemon not reachable" in r.output


def test_cli_write_commands_are_contract_gated(tmp_path):
    p = Project.init(tmp_path / "bare", source=str(EXPRESS))
    for cmd in (["import-source", p.root, "--from-original"], ["build", p.root], ["up", p.root]):
        r = _cli("harness", *cmd)
        assert r.exit_code != 0 and "contract gate" in r.output, (cmd, r.output)
