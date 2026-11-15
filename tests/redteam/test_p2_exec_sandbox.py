"""Red-team tests for the Phase 2 exec sandbox (threat model T19-T26): hostile jobs never reach `docker run`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parity_migrate.migrate.exec.policy import PolicyViolation, check_job, scan_argv, scan_job  # noqa: E402
from parity_migrate.migrate.models import ExecJob, Limits, Mount  # noqa: E402
from parity_migrate.project import Project  # noqa: E402

from p2_exec_helpers import FakeDocker  # noqa: E402


@pytest.fixture
def proj(tmp_path):
    p = Project.init(tmp_path / "p.migration", source="s")
    (p.root / "runs" / "RUN-0001" / "work").mkdir(parents=True)
    (p.root / "legacy_src").mkdir()
    return p


def _job(**kw):
    base = dict(run_id="RUN-0001", image="img:1", command=["true"], phase="test")
    base.update(kw)
    return ExecJob(**base)


def _raw(**kw):
    """An ExecJob that skipped validation (model_construct): the backend must still refuse it."""
    base = dict(run_id="RUN-0001", image="img:1", command=["true"], phase="test", network="none", workdir="/work",
                mounts=[], limits=Limits(), env={}, synthetic_secrets=[], labels={})
    base.update(kw)
    return ExecJob.model_construct(**base)


def _refused(proj, j, fd=None, **run_kw):
    fd = fd or FakeDocker()
    res = fd.backend(proj).run(j, **run_kw)
    assert res.exit_code is None and "policy" in res.error, res.error
    assert not any(c[0] == "run" for c in fd.calls), "a refused job must never reach docker run"
    return res


# ------------------------------------------------------------------ network
def test_test_phase_job_requesting_network_is_refused_by_model():
    with pytest.raises(ValidationError):
        _job(network="mirror")


def test_test_phase_network_bypassing_validation_is_refused_by_backend(proj):
    res = _refused(proj, _raw(network="mirror"))
    assert "network" in res.error
    _refused(proj, _raw(network="host"))
    _refused(proj, _raw(network="bridge", phase="build"))


def test_internal_network_only_for_test_phase_and_only_backend_created(proj):
    fd = FakeDocker()
    res = fd.backend(proj).run(_job(phase="build", network="mirror"), internal_network="bridge")
    assert res.exit_code is None and "internal_network" in res.error
    fd.nets["ma-int-0000000000-legacy"] = True                 # internal, but not this project's
    res = fd.backend(proj).run(_job(run_id="RUN-0002"), internal_network="ma-int-0000000000-legacy")
    assert res.exit_code is None and "not an internal network created by this backend" in res.error
    b = fd.backend(proj)
    name = b.internal_network_name("legacy")
    fd.nets[name] = False                                      # exists but routable
    res = b.run(_job(run_id="RUN-0003"), internal_network=name)
    assert res.exit_code is None and "not internal" in res.error
    assert not any(c[0] == "run" for c in fd.calls)


def test_test_phase_argv_always_network_none(proj):
    fd = FakeDocker()
    fd.backend(proj).run(_job())
    a = fd.run_argv()
    assert a[a.index("--network") + 1] == "none" and a.count("--network") == 1


# ------------------------------------------------------------------ mounts
def test_docker_socket_mount_refused_everywhere(proj, tmp_path):
    with pytest.raises(ValidationError):
        Mount(source="/var/run/docker.sock", target="/var/run/docker.sock")
    _refused(proj, _raw(mounts=[Mount.model_construct(source="/var/run/docker.sock", target="/sock",
                                                       read_only=True)]))
    # symlink inside the project that points at the socket
    link = proj.root / "runs" / "RUN-0001" / "work" / "sock"
    link.symlink_to("/var/run/docker.sock")
    _refused(proj, _job(mounts=[Mount(source="runs/RUN-0001/work/sock", target="/x")]))
    # a file merely named docker.sock, and a socket target path
    (proj.root / "runs" / "RUN-0001" / "work" / "docker.sock").write_text("")
    _refused(proj, _job(mounts=[Mount(source="runs/RUN-0001/work/docker.sock", target="/x")]))
    _refused(proj, _job(mounts=[Mount(source="legacy_src", target="/var/run/docker.sock")]))


def test_host_paths_outside_project_refused(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj.root / "legacy_src" / "escape").symlink_to(outside)
    _refused(proj, _job(mounts=[Mount(source="legacy_src/escape", target="/x")]))
    (proj.root / "runs" / "RUN-0001" / "work" / "home").symlink_to(Path.home())
    _refused(proj, _job(mounts=[Mount(source="runs/RUN-0001/work/home", target="/x")]))
    _refused(proj, _raw(mounts=[Mount.model_construct(source="../outside", target="/x", read_only=True)]))
    _refused(proj, _raw(mounts=[Mount.model_construct(source=str(outside), target="/x", read_only=True)]))


@pytest.mark.parametrize("src", [".", "audit", "code", "docs", "project.json", "legacy_src/.ssh",
                                 "runs/RUN-0001/work/.aws"])
def test_only_exec_areas_of_the_project_are_mountable(proj, src):
    v = scan_job(_raw(mounts=[Mount.model_construct(source=src, target="/x", read_only=True)]), proj.root)
    assert v, src


@pytest.mark.parametrize("target", ["/", "/proc", "/sys/fs", "/dev", "/etc/passwd", "/var/run/x", "/usr/bin"])
def test_system_mount_targets_refused(proj, target):
    assert scan_job(_raw(mounts=[Mount.model_construct(source="legacy_src", target=target, read_only=True)]),
                    proj.root)


# ------------------------------------------------------------------ secrets
def test_secret_env_values_refused_by_model():
    with pytest.raises(ValidationError):
        _job(env={"DB_PASSWORD": "hunter2hunter2"})
    with pytest.raises(ValidationError):
        _job(env={"X": "ghp_" + "a" * 36})


@pytest.mark.parametrize("env", [
    {"API_KEY": "short"},                       # secret-looking NAME, value too short for the patterns
    {"STRIPE_SECRET": "x"},
    {"DB_PASS_TOKEN": "abc"},
    {"ANTHROPIC_API_KEY": "sk"},                # model key family
    {"OPENAI_BASE": "https://api.openai.com"},
    {"AWS_REGION": "eu-west-1"},                # cloud credential family
    {"GIT_ASKPASS": "/bin/echo"},
    {"DOCKER_HOST": "tcp://10.0.0.1:2375"},
    {"LD_PRELOAD": "/tmp/x.so"},
    {"HTTPS_PROXY": "http://attacker:3128"},    # proxy is set by the backend only
])
def test_secret_or_credential_env_refused_by_policy(proj, env):
    _refused(proj, _job(env=env) if _model_ok(env) else _raw(env=env))


def _model_ok(env):
    try:
        _job(env=env)
        return True
    except ValidationError:
        return False


def test_orchestrator_secret_value_under_innocent_name_refused(proj, monkeypatch):
    key = "sk-proj-" + "Q" * 40
    env = {"PATH": "/usr/bin", "OPENAI_API_KEY": key}
    j = _raw(env={"GREETING": key})
    with pytest.raises(PolicyViolation) as e:
        check_job(j, proj.root, environ=env)
    assert "orchestrator environment" in str(e.value)
    fd = FakeDocker()
    res = fd.backend(proj, environ=env).run(j)
    assert res.exit_code is None and key not in res.error
    assert not any(c[0] == "run" for c in fd.calls)


def test_synthetic_secret_names_cannot_be_credential_families(proj):
    _refused(proj, _job(synthetic_secrets=["AWS_SECRET_ACCESS_KEY"]))
    _refused(proj, _job(synthetic_secrets=["ANTHROPIC_API_KEY"]))
    fd = FakeDocker()
    assert fd.backend(proj).run(_job(synthetic_secrets=["DB_PASSWORD"])).ok   # app secrets: synthetic is fine


def test_secret_values_never_in_audit_or_meta(proj):
    fd = FakeDocker(stderr=b"password=SuperSecretValue123 leaked\n")
    fd.backend(proj).run(_job(synthetic_secrets=["DB_PASSWORD"]))
    v = fd.popen_env["DB_PASSWORD"]
    for f in (proj.root / "runs").rglob("*"):
        if f.is_file():
            t = f.read_text(errors="replace")
            assert v not in t and "SuperSecretValue123" not in t, f
    assert v not in (proj.root / "audit" / "audit.jsonl").read_text()


# ------------------------------------------------------------------ privileged flags / argv
@pytest.mark.parametrize("bad", [
    ["run", "--privileged", "img"], ["run", "--cap-add", "SYS_ADMIN", "img"], ["run", "--cap-add=NET_RAW", "img"],
    ["run", "--device", "/dev/kvm", "img"], ["run", "--pid", "host", "img"], ["run", "--network=host", "img"],
    ["run", "--ipc", "host", "img"], ["run", "--userns", "host", "img"], ["run", "--uts=host", "img"],
    ["run", "--security-opt", "seccomp=unconfined", "img"], ["run", "--security-opt", "apparmor=unconfined", "img"],
    ["run", "--security-opt", "no-new-privileges:false", "img"], ["run", "--volumes-from", "other", "img"],
    ["run", "-v", "/var/run/docker.sock:/var/run/docker.sock", "img"],
    ["run", "--mount", "type=bind,source=/run/containerd/containerd.sock,target=/s", "img"],
    ["run", "-v", f"{Path.home()}:/h", "img"], ["run", "-v", "/x/.ssh:/k", "img"],
    ["run", "--user", "0", "img"], ["run", "-u", "root:root", "img"], ["run", "-p", "8080:80", "img"],
    ["run", "--publish-all", "img"],
])
def test_privileged_argv_refused(bad):
    assert scan_argv(bad), bad


def test_container_command_after_image_is_not_parsed_as_docker_flags():
    argv = ["run", "--network", "none", "img", "mkdir", "-p", "/x", "--privileged"]
    assert scan_argv(argv, stop_at=3) == []
    assert scan_argv(argv)                         # without stop_at the trailing words would look like flags


def test_hostile_image_reference_refused(proj):
    for img in ("--privileged", "-v/:/host", "img;rm -rf /", "img with space"):
        assert scan_job(_raw(image=img), proj.root), img


def test_reports_symlink_to_host_file_is_not_exfiltrated(proj, tmp_path):
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-ONLY-DATA")

    def container(argv):
        rep = FakeDocker().mount_target_host(argv, "/reports")
        (rep / "results.json").symlink_to(secret)
        os.mkfifo(rep / "pipe.xml")
    fd = FakeDocker(on_run=container)
    res = fd.backend(proj).run(_job())
    assert res.reports == []
    assert "HOST-ONLY-DATA" not in "".join(p.read_text() for p in (proj.root / "runs").rglob("*") if p.is_file())
