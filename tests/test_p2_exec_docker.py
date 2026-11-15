"""DockerBackend (migrate/exec/docker.py) with a scripted docker CLI: argv, limits, refusals, results, digests.
Live tests at the bottom run only when a docker daemon answers `docker info`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from migration_agent.migrate import exec as p2exec
from migration_agent.migrate.exec.base import load_run
from migration_agent.migrate.exec.docker import (DockerBackend, ExecConfig, load_exec_config,
                                                 render_squid_conf)
from migration_agent.migrate.models import ExecJob, Limits, Mount
from migration_agent.project import Project

from p2_exec_helpers import IMG_ID, PROXY_ID, REPO_DIGEST, FakeDocker, flag_values


@pytest.fixture
def proj(tmp_path):
    p = Project.init(tmp_path / "p.migration", source="s")
    (p.root / "runs" / "RUN-0001" / "work" / "app").mkdir(parents=True)
    (p.root / "legacy_src").mkdir()
    return p


def job(**kw):
    base = dict(run_id="RUN-0001", image="img:1", command=["npm", "test", "--", "-p", "1"], phase="test",
                mounts=[Mount(source="legacy_src", target="/src"),
                        Mount(source="runs/RUN-0001/work/app", target="/work", read_only=False)],
                limits=Limits(cpus=1.5, memory_mb=768, pids=128, timeout_s=60, disk_mb=256),
                env={"NODE_ENV": "test"}, synthetic_secrets=["API_TOKEN"], labels={"slice": "SLICE-0001"})
    base.update(kw)
    return ExecJob(**base)


# ------------------------------------------------------------------ argv construction
def test_test_phase_argv_is_fully_locked_down(proj):
    fd = FakeDocker()
    res = fd.backend(proj).run(job())
    assert res.exit_code == 0 and not res.error, res.error
    a = fd.run_argv()
    assert a[0] == "run" and flag_values(a, "--network") == ["none"]
    for flag in ("--read-only", "--init"):
        assert flag in a
    assert flag_values(a, "--pull") == ["never"]
    assert flag_values(a, "--cap-drop") == ["ALL"]
    assert flag_values(a, "--security-opt") == ["no-new-privileges:true"]
    user = flag_values(a, "--user")[0]
    assert user.split(":")[0] != "0"
    assert flag_values(a, "--pids-limit") == ["128"]
    assert flag_values(a, "--memory") == ["768m"] and flag_values(a, "--memory-swap") == ["768m"]
    assert flag_values(a, "--cpus") == ["1.5"]
    assert "/tmp:rw,nosuid,nodev,size=256m" in flag_values(a, "--tmpfs")
    assert "--privileged" not in a and "--cap-add" not in a
    mounts = flag_values(a, "--mount")
    root = str(proj.root)
    assert f"type=bind,source={root}/legacy_src,target=/src,readonly" in mounts
    assert f"type=bind,source={root}/runs/RUN-0001/work/app,target=/work" in mounts
    assert all("docker.sock" not in m for m in mounts)
    envs = flag_values(a, "-e")
    assert "NODE_ENV=test" in envs and "API_TOKEN" in envs           # synthetic: name only on argv
    assert not any(e.startswith("API_TOKEN=") for e in envs)
    assert fd.popen_env["API_TOKEN"] and len(fd.popen_env["API_TOKEN"]) >= 24
    # argv = ... --entrypoint npm <image id> test -- -p 1 (run by immutable id, command is data after the image)
    i = a.index(IMG_ID)
    assert a[i - 2:i] == ["--entrypoint", "npm"] and a[i + 1:] == ["test", "--", "-p", "1"]
    assert all(k.get("shell") is False for k in fd.kwargs)
    assert not any(c[0] == "pull" or "--pull=always" in c for c in fd.calls)


def test_workdir_without_mount_gets_tmpfs(proj):
    fd = FakeDocker()
    fd.backend(proj).run(job(mounts=[], workdir="/app"))
    assert "/app:rw,nosuid,nodev,size=256m" in flag_values(fd.run_argv(), "--tmpfs")


def test_build_phase_without_mirror_hosts_is_offline(proj):
    fd = FakeDocker()
    res = fd.backend(proj, config=ExecConfig()).run(job(phase="build", network="mirror"))
    assert res.ok
    a = fd.run_argv()
    assert flag_values(a, "--network") == ["none"]
    assert not any(c[:2] == ["network", "create"] for c in fd.calls)
    assert not any(e.startswith("HTTPS_PROXY") for e in flag_values(a, "-e"))


def test_build_phase_with_mirror_uses_internal_network_and_allowlist_proxy(proj):
    fd = FakeDocker()
    cfg = ExecConfig(hosts=["registry.example", ".mirror.corp.example"], ports=[443, 8081])
    res = fd.backend(proj, config=cfg).run(job(phase="build", network="mirror"))
    assert res.ok, res.error
    creates = [c for c in fd.calls if c[:2] == ["network", "create"]]
    assert len(creates) == 1 and "--internal" in creates[0]
    net = creates[0][-1]
    proxy_run = [c for c in fd.calls if c[0] == "run" and "ma.role=egress-proxy" in c][0]
    assert PROXY_ID in proxy_run and flag_values(proxy_run, "--network") == ["bridge"]
    assert flag_values(proxy_run, "--cap-drop") == ["ALL"] and "--read-only" in proxy_run
    assert ["network", "connect", "--alias", "egress-proxy", net] == [c for c in fd.calls
                                                                       if c[:2] == ["network", "connect"]][0][:5]
    a = fd.run_argv()
    assert flag_values(a, "--network") == [net]
    assert "HTTPS_PROXY=http://egress-proxy:3128" in flag_values(a, "-e")
    conf = (proj.root / "runs" / "RUN-0001" / "work" / "proxy" / "squid.conf").read_text()
    assert "acl mirror_hosts dstdomain .mirror.corp.example registry.example" in conf
    assert "acl mirror_ports port 443 8081" in conf and conf.rstrip().count("http_access deny all") == 1
    # cleanup: proxy removed, network removed, egress log kept as a report
    assert any(c[:2] == ["rm", "--force"] and c[-1].endswith("proxy-run-0001") for c in fd.calls)
    assert ["network", "rm", net] in fd.calls
    assert "runs/RUN-0001/reports/egress.log" in res.reports
    assert "CONNECT registry.example:443" in (proj.root / "runs/RUN-0001/reports/egress.log").read_text()


def test_runtime_runsc_used_when_installed_or_required(proj):
    fd = FakeDocker(runtimes=("runc", "runsc"))
    fd.backend(proj).run(job())
    assert flag_values(fd.run_argv(), "--runtime") == ["runsc"]
    fd2 = FakeDocker(runtimes=("runc", "runsc"))
    fd2.backend(proj, config=ExecConfig(runtime="runc")).run(job())
    assert "--runtime" not in fd2.run_argv()
    fd3 = FakeDocker(runtimes=("runc",))
    b = fd3.backend(proj, config=ExecConfig(runtime="runsc"))
    ok, reason = b.available()
    assert not ok and "runsc" in reason


# ------------------------------------------------------------------ limits / results
def test_timeout_kills_container_and_is_reported(proj):
    fd = FakeDocker(hang=True, stdout=b"still running\n")
    res = fd.backend(proj).run(job())
    assert res.timed_out and res.exit_code is None and not res.ok
    name = fd.run_argv()[fd.run_argv().index("--name") + 1]
    assert ["kill", name] in fd.calls
    assert any(c[:2] == ["rm", "--force"] and c[-1] == name for c in fd.calls)
    _, back = load_run(proj, "RUN-0001")
    assert back.timed_out


def test_oom_and_nonzero_exit_are_normal_results(proj):
    fd = FakeDocker(rc=137, oom=True, stderr=b"Killed\n")
    res = fd.backend(proj).run(job())
    assert res.exit_code == 137 and res.oom_killed and not res.error and "Killed" in res.stderr_tail


def test_docker_run_error_125_is_backend_error(proj):
    fd = FakeDocker(rc=125, stderr=b"docker: Error response from daemon: invalid mount config\n")
    res = fd.backend(proj).run(job())
    assert res.exit_code is None and "container did not start" in res.error and "invalid mount" in res.error


def test_digest_capture_prefers_repo_digest_else_image_id(proj):
    fd = FakeDocker()
    res = fd.backend(proj).run(job())
    assert res.image_digest == REPO_DIGEST
    fd2 = FakeDocker(images={"img:1": (IMG_ID, [])})
    assert fd2.backend(proj).run(job(run_id="RUN-0002", mounts=[])).image_digest == IMG_ID


def test_missing_image_is_never_pulled(proj):
    fd = FakeDocker(images={})
    res = fd.backend(proj).run(job())
    assert res.exit_code is None and "not present locally" in res.error
    assert not any(c[0] in ("run", "pull") for c in fd.calls)


def test_record_run_integration_logs_reports_audit(proj):
    def container(argv):
        rep = FakeDocker().mount_target_host(argv, "/reports")
        (rep / "junit.xml").write_text("<testsuite tests='2' failures='0'/>")
        (rep / "evil").symlink_to(proj.root / "project.json")          # planted symlink: never followed
        (rep / ".hidden").write_text("x")
    fd = FakeDocker(stdout=b"BUILD OK\n", stderr=b"warn\n", on_run=container)
    res = fd.backend(proj).run(job())
    d = proj.root / "runs" / "RUN-0001"
    meta = json.loads((d / "meta.json").read_text())
    assert meta["result"]["exit_code"] == 0 and meta["job"]["image"] == "img:1"
    assert (d / "logs" / "stdout.txt").read_text() == "BUILD OK\n"
    assert res.reports == ["runs/RUN-0001/reports/junit.xml"]
    assert not (d / "work" / "reports").exists()
    audit = [json.loads(x) for x in (proj.root / "audit" / "audit.jsonl").read_text().splitlines()]
    acts = [(e["agent"], e["action"]) for e in audit]
    assert ("exec", "exec_start") in acts and ("exec", "exec_run") in acts
    run_rec = [e for e in audit if e["action"] == "exec_run"][0]
    assert run_rec.get("image_digest") == REPO_DIGEST or REPO_DIGEST in json.dumps(run_rec)


def test_synthetic_secret_values_are_scrubbed_from_logs(proj):
    seen = {}

    def container(argv):
        seen["v"] = fd.popen_env["API_TOKEN"]
        fd.stdout = f"token is {seen['v']}\n".encode()
    fd = FakeDocker(on_run=container)
    fd.backend(proj).run(job())
    out = (proj.root / "runs/RUN-0001/logs/stdout.txt").read_text()
    assert seen["v"] not in out and "[SYNTHETIC:API_TOKEN]" in out


def test_cli_environment_never_carries_model_or_cloud_keys(proj):
    fd = FakeDocker()
    env = {"PATH": "/usr/bin", "HOME": "/home/u", "ANTHROPIC_API_KEY": "sk-ant-" + "x" * 30,
           "AWS_SECRET_ACCESS_KEY": "y" * 40, "DOCKER_HOST": "unix:///run/user/1000/docker.sock"}
    fd.backend(proj, environ=env).run(job())
    assert "ANTHROPIC_API_KEY" not in fd.popen_env and "AWS_SECRET_ACCESS_KEY" not in fd.popen_env
    assert fd.popen_env["DOCKER_HOST"].startswith("unix://")       # the CLI's own connection setting
    for k in fd.kwargs:
        assert "ANTHROPIC_API_KEY" not in (k.get("env") or {})
    assert not any("sk-ant-" in x for c in fd.calls for x in c)


def test_long_logs_are_capped(proj):
    fd = FakeDocker(stdout=b"A" * 3_000_000 + b"END\n")
    res = fd.backend(proj).run(job())
    assert res.truncated and res.stdout_tail.endswith("END\n")
    assert (proj.root / "runs/RUN-0001/logs/stdout.txt").stat().st_size < 1_100_000


# ------------------------------------------------------------------ availability
def test_available_false_without_cli(proj):
    fd = FakeDocker(cli=False)
    b = fd.backend(proj)
    ok, reason = b.available()
    assert not ok and "docker CLI not found" in reason
    res = b.run(job())
    assert res.exit_code is None and "docker CLI not found" in res.error
    assert (proj.root / "runs/RUN-0001/meta.json").exists()


def test_available_false_when_daemon_down(proj):
    fd = FakeDocker(daemon=False)
    ok, reason = fd.backend(proj).available()
    assert not ok and reason.startswith("docker daemon not reachable") and "Cannot connect" in reason


def test_get_backend_discovers_docker_backend(proj):
    assert isinstance(p2exec.get_backend(proj), DockerBackend)


def test_run_without_project_returns_error_result():
    b = FakeDocker().backend(None)
    res = b.run(job(mounts=[]))
    assert res.exit_code is None and "project" in res.error


# ------------------------------------------------------------------ config
def test_mirror_config_from_file_and_env(proj):
    (proj.root / "harness").mkdir()
    (proj.root / "harness" / "mirror.json").write_text(json.dumps({"hosts": ["Nexus.Corp.Example"],
                                                                   "ports": [8443]}))
    cfg = load_exec_config(proj, {})
    assert cfg.hosts == ["nexus.corp.example"] and cfg.ports == [8443]
    cfg = load_exec_config(proj, {"MA_MIRROR_HOSTS": "a.example, b.example", "MA_EXEC_RUNTIME": "runc"})
    assert cfg.hosts == ["a.example", "b.example"] and cfg.runtime == "runc"
    assert load_exec_config(proj, {"MA_MIRROR_HOSTS": ""}).hosts == []
    assert load_exec_config(None, {}).hosts == []


@pytest.mark.parametrize("bad", ["*", "all", "evil.example\nhttp_access allow all", "a b", "http://x.example",
                                 "x.example:443/path", "."])
def test_mirror_hosts_cannot_inject_squid_config(bad):
    with pytest.raises(ValueError):
        ExecConfig(hosts=[bad])
    with pytest.raises(ValueError):
        render_squid_conf([bad], [443])


def test_invalid_mirror_file_makes_backend_unavailable(proj):
    (proj.root / "harness").mkdir()
    (proj.root / "harness" / "mirror.json").write_text('{"hosts": ["*"]}')
    ok, reason = FakeDocker().backend(proj).available()
    assert not ok and "exec config invalid" in reason


def test_user_is_never_root():
    with pytest.raises(ValueError):
        ExecConfig(user="0:0")
    assert DockerBackend(None, config=ExecConfig(user="10001:10001"), environ={}).user() == "10001:10001"


# ------------------------------------------------------------------ live (real daemon only)
def _daemon_ok() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


LIVE = pytest.mark.skipif(not _daemon_ok(), reason="docker daemon not available (docker info failed)")
LIVE_IMAGE = os.environ.get("MA_LIVE_TEST_IMAGE", "busybox:1.36")


def _live_backend(proj):
    b = DockerBackend(proj)
    if b.docker(["image", "inspect", LIVE_IMAGE]).returncode != 0:
        pytest.skip(f"{LIVE_IMAGE} not present locally (pulls are disabled)")
    return b


def _live_job(cmd, **kw):
    return ExecJob(run_id=kw.pop("run_id", "RUN-0001"), image=LIVE_IMAGE, command=cmd, phase="test",
                   limits=kw.pop("limits", Limits(timeout_s=60, memory_mb=128, pids=64, disk_mb=16)), **kw)


@LIVE
def test_live_network_canary_cannot_open_outbound_socket(proj):
    b = _live_backend(proj)
    res = b.run(_live_job(["sh", "-c", "wget -T 3 -q -O- http://1.1.1.1/ && echo CANARY-ESCAPED || echo blocked"]))
    assert "CANARY-ESCAPED" not in res.stdout_tail and "blocked" in res.stdout_tail


@LIVE
def test_live_fork_bomb_is_contained_by_pids_limit(proj):
    b = _live_backend(proj)
    res = b.run(_live_job(["sh", "-c", "i=0; while [ $i -lt 500 ]; do sleep 30 & i=$((i+1)); done; echo spawned"],
                          limits=Limits(timeout_s=20, memory_mb=128, pids=32, disk_mb=16)))
    assert "spawned" not in res.stdout_tail or res.exit_code != 0 or res.timed_out


@LIVE
def test_live_disk_fill_is_capped_by_tmpfs(proj):
    b = _live_backend(proj)
    res = b.run(_live_job(["sh", "-c", "dd if=/dev/zero of=/tmp/fill bs=1M count=64 && echo FILLED"]))
    assert "FILLED" not in res.stdout_tail and res.exit_code != 0


@LIVE
def test_live_rootfs_is_read_only_and_user_not_root(proj):
    b = _live_backend(proj)
    res = b.run(_live_job(["sh", "-c", "id -u; touch /etc/x && echo WROTE || echo ro"]))
    assert "WROTE" not in res.stdout_tail and res.stdout_tail.splitlines()[0] != "0"
