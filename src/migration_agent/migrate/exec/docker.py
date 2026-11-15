"""DockerBackend: the real ExecBackend (docs/phase2-plan.md section 6; contract in exec/base.py).

Every container is started through the docker CLI with a FIXED argv list (never a shell, `shell=False`):

    docker run --name ma-<proj>-run-0007 --pull never --init --hostname sandbox
               --network none                                   # test phase, or build without mirror hosts
               --read-only --tmpfs /tmp:rw,nosuid,nodev,size=<disk_mb>m
               --cap-drop ALL --security-opt no-new-privileges:true --user <uid:gid, never 0>
               --pids-limit N --memory Mm --memory-swap Mm --cpus C --ulimit nofile=4096:4096 --ulimit core=0
               [--runtime runsc]                                # gVisor when configured / installed
               --workdir /work --label ma.*=... --mount type=bind,source=<project>/<rel>,target=...,readonly
               --mount type=bind,source=<project>/runs/RUN-NNNN/work/reports,target=/reports
               -e K=V ... -e SYNTHETIC_NAME (value only in the docker CLI's environment, never in argv)
               --entrypoint <argv[0]> <image id sha256:...> <argv[1:]>

Build phase with network "mirror" and at least one mirror host: a per-run `--internal` network (no route out) plus a
Squid allowlisting proxy (same pattern as docker/squid.conf) that is the only member with a routable network and
only allows the configured mirror hosts/ports. No mirror hosts configured => the build runs with `--network none`.

Mirror hosts: `harness/mirror.json` {"hosts": ["nexus.corp.example"], "ports": [443], "runtime": "auto",
"proxy_image": "ubuntu/squid:6.6-24.04_beta"}; env `MA_MIRROR_HOSTS` (comma separated) overrides `hosts`;
`MA_EXEC_RUNTIME` (auto|runsc|runc) overrides `runtime`.

Images are never pulled (`--pull never`); the image is resolved with `docker image inspect` and run by its
immutable ID, and its digest is recorded in the result. Every job and every assembled argv goes through
exec/policy.py first. Results are written by `record_run` into runs/RUN-NNNN/.

Harness helpers (not part of the ExecBackend protocol): `build_image`, `ensure_internal_network`,
`remove_network`, `start_service`, `exec_in`, `remove_containers`, `list_managed`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...ingest.secrets import redact_string
from ..models import ExecJob, ExecResult
from .base import LOG_CAP_BYTES, record_run
from .policy import PolicyViolation, check_argv, check_job, scan_mount_target

DEFAULT_PROXY_IMAGE = "ubuntu/squid:6.6-24.04_beta"   # TODO: pin by digest (docker buildx imagetools inspect)
PROXY_ALIAS = "egress-proxy"
PROXY_PORT = 3128
REPORTS_TARGET = "/reports"
MAX_REPORT_BYTES = 5_000_000
MAX_REPORTS = 50
_CLI_ENV_KEYS = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
                 "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "TMPDIR", "USER", "SYSTEMROOT")
_HOST = re.compile(r"^\.?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$")
_INTERNAL_NET = re.compile(r"^ma-int-[0-9a-f]{10}-[a-z0-9-]{1,40}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class _BackendError(Exception):
    pass


# ------------------------------------------------------------------ config
class ExecConfig(BaseModel):
    """Backend configuration (harness/mirror.json + env). Not a shared model: lives with the backend."""
    model_config = ConfigDict(extra="forbid")
    hosts: list[str] = Field(default_factory=list)          # package-mirror host names (Squid dstdomain)
    ports: list[int] = Field(default_factory=lambda: [443])
    runtime: Literal["auto", "runsc", "runc"] = "auto"
    proxy_image: str = DEFAULT_PROXY_IMAGE
    user: str = "auto"                                       # "auto" = invoking uid:gid (10001 when root)
    docker: str = "docker"

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip().lower()
            if not h:
                continue
            if not _HOST.match(h) or h in (".", "all") or "*" in h:
                raise ValueError(f"invalid mirror host {h!r} (plain host name, optional leading '.')")
            out.append(h)
        return sorted(set(out))

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[int]) -> list[int]:
        if not v or any(not 1 <= p <= 65535 for p in v):
            raise ValueError("mirror ports must be 1..65535")
        return sorted(set(v))

    @field_validator("user")
    @classmethod
    def _user(cls, v: str) -> str:
        if v != "auto" and (not re.match(r"^\d+(:\d+)?$", v) or v.split(":")[0] == "0"):
            raise ValueError("user must be 'auto' or a numeric non-root uid[:gid]")
        return v


def mirror_config_path(project) -> Path:
    from ..paths import Phase2Layout
    return Phase2Layout(project).harness_dir / "mirror.json"


def load_exec_config(project=None, environ: Mapping[str, str] | None = None) -> ExecConfig:
    env = os.environ if environ is None else environ
    data: dict[str, Any] = {}
    if project is not None:
        p = mirror_config_path(project)
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except ValueError as e:
                raise ValueError(f"{p}: invalid JSON ({e})") from None
            if not isinstance(data, dict):
                raise ValueError(f"{p}: must be a JSON object")
    if env.get("MA_MIRROR_HOSTS") is not None:
        data["hosts"] = [h for h in re.split(r"[,\s]+", env["MA_MIRROR_HOSTS"]) if h]
    if env.get("MA_EXEC_RUNTIME"):
        data["runtime"] = env["MA_EXEC_RUNTIME"]
    return ExecConfig.model_validate(data)


def render_squid_conf(hosts: list[str], ports: list[int]) -> str:
    """Allowlist proxy config: only the mirror hosts, only the mirror ports; everything else denied."""
    if not hosts:
        raise ValueError("no mirror hosts: the build runs without network instead")
    hosts = ExecConfig(hosts=hosts, ports=ports).hosts
    return "\n".join([
        "# generated by migration-agent (migrate/exec/docker.py): package-mirror allowlist",
        f"http_port {PROXY_PORT}",
        "acl mirror_hosts dstdomain " + " ".join(hosts),
        "acl mirror_ports port " + " ".join(str(p) for p in sorted(set(ports))),
        "acl CONNECT method CONNECT",
        "http_access deny !mirror_ports",
        "http_access allow CONNECT mirror_hosts",
        "http_access allow mirror_hosts",
        "http_access deny all",
        "cache deny all",
        "via off",
        "forwarded_for delete",
        "access_log stdio:/dev/stdout",
        "cache_log stdio:/dev/stderr",
        "pid_filename /var/run/squid/squid.pid",
        "coredump_dir /tmp",
        "",
    ])


# ------------------------------------------------------------------ helpers
class _Tail:
    """Bounded byte sink keeping the END of a stream (log floods cannot exhaust orchestrator memory)."""

    def __init__(self, cap: int = LOG_CAP_BYTES + 4096):
        self.cap, self.buf, self.total = cap, bytearray(), 0

    def feed(self, stream) -> None:
        try:
            while chunk := stream.read(65536):
                self.total += len(chunk)
                self.buf += chunk
                if len(self.buf) > 2 * self.cap:
                    del self.buf[:-self.cap]
        except (OSError, ValueError):
            pass
        if len(self.buf) > self.cap:
            del self.buf[:-self.cap]

    def text(self) -> str:
        out = bytes(self.buf).decode("utf-8", errors="replace")
        return ("[... truncated ...]\n" + out) if self.total > len(self.buf) else out


def _now() -> str:
    from ..state import now_iso
    return now_iso()


def _scrub(text: str, values: Mapping[str, str]) -> str:
    for k, v in values.items():
        if v:
            text = text.replace(v, f"[SYNTHETIC:{k}]")
    return text


def synthetic_value() -> str:
    return _secrets.token_urlsafe(24)


# ------------------------------------------------------------------ backend
class DockerBackend:
    name = "docker"

    def __init__(self, project=None, *, config: ExecConfig | None = None, run: Callable | None = None,
                 popen: Callable | None = None, which: Callable | None = None,
                 environ: Mapping[str, str] | None = None):
        self.project = project
        self._environ = dict(os.environ if environ is None else environ)
        self._config = config
        self._config_error = ""
        if config is None:
            try:
                self._config = load_exec_config(project, self._environ)
            except ValueError as e:
                self._config, self._config_error = ExecConfig(), str(e)
        self._run = run or subprocess.run
        self._popen = popen or subprocess.Popen
        self._which = which or shutil.which
        self._ok: tuple[bool, str] | None = None
        self._runtimes: list[str] | None = None

    # ---- identity
    @property
    def config(self) -> ExecConfig:
        return self._config

    @property
    def root(self) -> Path | None:
        return None if self.project is None else Path(self.project.root).resolve()

    @property
    def project_tag(self) -> str:
        return hashlib.sha256(str(self.root or "-").encode()).hexdigest()[:10]

    def container_name(self, run_id: str, kind: str = "run") -> str:
        return f"ma-{self.project_tag}-{kind}-{run_id.lower()}"

    def user(self) -> str:
        if self.config.user != "auto":
            return self.config.user
        uid = getattr(os, "getuid", lambda: 10001)()
        gid = getattr(os, "getgid", lambda: 10001)()
        return "10001:10001" if uid == 0 else f"{uid}:{gid if gid != 0 else uid}"

    # ---- low-level CLI
    def _exe(self) -> str | None:
        return self._which(self.config.docker)

    def _cli_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: self._environ[k] for k in _CLI_ENV_KEYS if k in self._environ}
        env.update(extra or {})
        return env

    def docker(self, args: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess:
        exe = self._exe()
        if exe is None:
            return subprocess.CompletedProcess([self.config.docker, *args], 127, "", "docker CLI not found on PATH")
        try:
            return self._run([exe, *args], capture_output=True, text=True, timeout=timeout, check=False,
                             env=self._cli_env(), shell=False, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([exe, *args], 124, "", f"docker {args[0]} timed out after {timeout}s")
        except OSError as e:
            return subprocess.CompletedProcess([exe, *args], 125, "", f"docker not runnable: {type(e).__name__}: {e}")

    # ---- availability
    def available(self) -> tuple[bool, str]:
        if self._ok is not None and self._ok[0]:
            return self._ok
        if self._config_error:
            return False, f"exec config invalid: {self._config_error}"
        if self._exe() is None:
            return False, "docker CLI not found on PATH"
        r = self.docker(["info", "--format", "{{json .Runtimes}}"], timeout=15)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            return False, "docker daemon not reachable: " + (msg[-1][:200] if msg else f"exit {r.returncode}")
        try:
            rt = json.loads((r.stdout or "").strip() or "{}")
            self._runtimes = sorted(rt) if isinstance(rt, dict) else []
        except ValueError:
            self._runtimes = []
        if self.config.runtime == "runsc" and "runsc" not in self._runtimes:
            return False, "gVisor runtime 'runsc' is configured (MA_EXEC_RUNTIME/mirror.json) but not installed"
        self._ok = (True, "")
        return self._ok

    def runtime_args(self) -> list[str]:
        if self.config.runtime == "runc":
            return []
        if self.config.runtime == "runsc" or "runsc" in (self._runtimes or []):
            return ["--runtime", "runsc"]
        return []

    # ---- images
    def image_ref(self, image: str) -> tuple[str, str]:
        """(image id to run, digest to record). Never pulls."""
        r = self.docker(["image", "inspect", "--format", "{{.Id}}|{{json .RepoDigests}}", image], timeout=30)
        if r.returncode != 0:
            raise _BackendError(f"image {image!r} is not present locally (pulls are disabled: build or load it "
                                "first, e.g. `migration-agent harness build`)")
        out = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        img_id, _, rd = out.partition("|")
        img_id = img_id.strip()
        if not re.match(r"^sha256:[0-9a-f]{64}$", img_id):
            raise _BackendError(f"unexpected image id for {image!r}: {img_id[:80]!r}")
        digest = img_id
        try:
            rds = json.loads(rd) if rd.strip() else []
        except ValueError:
            rds = []
        for d in rds or []:
            if isinstance(d, str) and "@sha256:" in d:
                digest = d.split("@", 1)[1]
                break
        return img_id, digest

    def build_image(self, context: Path, dockerfile: Path, tag: str, *, timeout: float = 1800,
                    labels: Mapping[str, str] | None = None) -> tuple[bool, str, str]:
        """`docker build --network none` of a harness Dockerfile -> (ok, digest or "", log). Never pulls."""
        root = self.root
        if root is None:
            return False, "", "build_image needs a project"
        ctx, df = Path(context).resolve(), Path(dockerfile).resolve()
        for p in (ctx, df):
            if not p.is_relative_to(root):
                return False, "", f"{p} is outside the project folder"
        if not _TAG.match(tag):
            return False, "", f"invalid image tag {tag!r}"
        args = ["build", "--network", "none", "--pull=false", "-f", str(df), "-t", tag,
                "--label", "ma.managed=1", "--label", f"ma.project={self.project_tag}"]
        for k, v in sorted((labels or {}).items()):
            args += ["--label", f"ma.{k}={v}"]
        args.append(str(ctx))
        check_argv(args)
        r = self.docker(args, timeout=timeout)
        log = redact_string((r.stdout or "") + (r.stderr or ""))
        if r.returncode != 0:
            return False, "", log
        try:
            return True, self.image_ref(tag)[1], log
        except _BackendError as e:
            return False, "", log + f"\n{e}"

    # ---- networks
    def internal_network_name(self, suffix: str) -> str:
        suffix = re.sub(r"[^a-z0-9-]", "-", suffix.lower())[:40].strip("-") or "net"
        return f"ma-int-{self.project_tag}-{suffix}"

    def _network_internal(self, name: str) -> bool | None:
        r = self.docker(["network", "inspect", "--format", "{{.Internal}}", name], timeout=30)
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip().lower() == "true"

    def ensure_internal_network(self, suffix: str) -> str:
        """Create (or reuse) an `--internal` network: containers on it reach each other, never the outside."""
        name = self.internal_network_name(suffix)
        state = self._network_internal(name)
        if state is True:
            return name
        if state is False:
            raise _BackendError(f"network {name} exists but is not internal; refusing to use it")
        r = self.docker(["network", "create", "--internal", "--label", "ma.managed=1",
                         "--label", f"ma.project={self.project_tag}", name], timeout=30)
        if r.returncode != 0:
            raise _BackendError(f"cannot create internal network {name}: {(r.stderr or '').strip()[:200]}")
        return name

    def remove_network(self, name: str) -> bool:
        return self.docker(["network", "rm", name], timeout=30).returncode == 0

    def _validated_internal(self, network: str) -> str:
        if not _INTERNAL_NET.match(network or "") or not network.startswith(f"ma-int-{self.project_tag}-"):
            raise PolicyViolation([f"network {network!r} is not an internal network created by this backend"])
        if self._network_internal(network) is not True:
            raise PolicyViolation([f"network {network!r} is missing or not internal (no route out required)"])
        return network

    # ---- argv
    def _mount_args(self, job: ExecJob, extra: list[tuple[Path, str, bool]]) -> list[str]:
        root = self.root
        out: list[str] = []
        items = [(root / m.source, m.target, m.read_only) for m in job.mounts] + list(extra)
        for host, target, ro in items:
            hp = Path(host).resolve()
            if "," in str(hp) or "," in target or "=" in target:
                raise PolicyViolation([f"mount path {hp} / {target} contains ',' or '='"])
            if not hp.exists():
                raise _BackendError(f"mount source {hp.relative_to(root) if hp.is_relative_to(root) else hp} "
                                    "does not exist")
            out += ["--mount", f"type=bind,source={hp},target={target}" + (",readonly" if ro else "")]
        return out

    def build_argv(self, job: ExecJob, *, image_ref: str, name: str, network_args: list[str],
                   extra_mounts: list[tuple[Path, str, bool]] = (), synthetic: Mapping[str, str] | None = None,
                   detach: bool = False, extra_env: Mapping[str, str] | None = None,
                   extra_tmpfs: list[str] = ()) -> tuple[list[str], int]:
        """(docker argv without the executable, index of the image). Pure: no side effects."""
        lim = job.limits
        mem = f"{lim.memory_mb}m"
        argv = ["run", "--name", name, "--pull", "never", "--init", "--hostname", "sandbox"]
        if detach:
            argv += ["--detach", "--rm"]
        argv += list(network_args)
        argv += ["--read-only", "--tmpfs", f"/tmp:rw,nosuid,nodev,size={lim.disk_mb}m",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--user", self.user(), "--pids-limit", str(lim.pids), "--memory", mem, "--memory-swap", mem,
                 "--cpus", f"{lim.cpus:g}", "--ulimit", "nofile=4096:4096", "--ulimit", "core=0"]
        argv += self.runtime_args()
        argv += ["--workdir", job.workdir]
        targets = [m.target.rstrip("/") for m in job.mounts] + [t.rstrip("/") for _, t, _ in extra_mounts]
        if not any(job.workdir.rstrip("/") == t or job.workdir.startswith(t + "/") for t in targets):
            argv += ["--tmpfs", f"{job.workdir}:rw,nosuid,nodev,size={lim.disk_mb}m"]
        for t in extra_tmpfs:
            bad = scan_mount_target(t) if not t.startswith(("/var/run/", "/run/")) else []
            if bad or not t.startswith("/") or ".." in t.split("/") or "," in t or ":" in t:
                raise PolicyViolation(bad or [f"invalid tmpfs target {t!r}"])
            argv += ["--tmpfs", f"{t}:rw,nosuid,nodev,size={lim.disk_mb}m"]
        argv += ["--label", "ma.managed=1", "--label", f"ma.project={self.project_tag}",
                 "--label", f"ma.run={job.run_id}", "--label", f"ma.phase={job.phase}"]
        for k, v in sorted(job.labels.items()):
            argv += ["--label", f"ma.l.{k}={v}"]
        argv += self._mount_args(job, list(extra_mounts))
        for k, v in sorted({**job.env, **(extra_env or {})}.items()):
            argv += ["-e", f"{k}={v}"]
        for k in sorted(synthetic or {}):
            argv += ["-e", k]                      # value only in the CLI process environment
        argv += ["--entrypoint", job.command[0]]
        idx = len(argv)
        argv += [image_ref, *job.command[1:]]
        return argv, idx

    # ---- container execution
    def _run_container(self, argv: list[str], *, name: str, timeout: float,
                       secret_env: Mapping[str, str]) -> tuple[int | None, str, str, bool, float]:
        exe = self._exe()
        t0 = time.monotonic()
        try:
            p = self._popen([exe, *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, env=self._cli_env(secret_env), shell=False)
        except OSError as e:
            raise _BackendError(f"docker not runnable: {type(e).__name__}: {e}") from None
        out, err = _Tail(), _Tail()
        threads = [threading.Thread(target=out.feed, args=(p.stdout,), daemon=True),
                   threading.Thread(target=err.feed, args=(p.stderr,), daemon=True)]
        for t in threads:
            t.start()
        timed_out = False
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.docker(["kill", name], timeout=30)
            try:
                rc = p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = p.wait(timeout=10)
        for t in threads:
            t.join(timeout=10)
        return rc, out.text(), err.text(), timed_out, time.monotonic() - t0

    def _inspect_state(self, name: str) -> tuple[bool, int | None] | None:
        r = self.docker(["inspect", "--format", "{{.State.OOMKilled}}|{{.State.ExitCode}}", name], timeout=30)
        if r.returncode != 0:
            return None
        oom, _, code = (r.stdout or "").strip().partition("|")
        try:
            return oom.strip().lower() == "true", int(code.strip())
        except ValueError:
            return oom.strip().lower() == "true", None

    def _collect_reports(self, rep_dir: Path) -> dict[str, bytes]:
        """Copy-out of report files the container wrote. Symlinks and special files are never followed."""
        out: dict[str, bytes] = {}
        if not rep_dir.is_dir() or rep_dir.is_symlink():
            return out
        for entry in sorted(os.scandir(rep_dir), key=lambda e: e.name)[:MAX_REPORTS * 4]:
            if len(out) >= MAX_REPORTS:
                break
            if entry.name.startswith(".") or not re.match(r"^[A-Za-z0-9_.-]{1,128}$", entry.name):
                continue
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REPORT_BYTES:
                continue
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                out[entry.name] = f.read(MAX_REPORT_BYTES)
        return out

    def _error(self, job: ExecJob, msg: str, digest: str = "") -> ExecResult:
        if self.project is None:
            return ExecResult(run_id=job.run_id, exit_code=None, image_digest=digest, error=redact_string(msg))
        return record_run(self.project, job, exit_code=None, image_digest=digest, error=msg)

    def _audit(self, action: str, **fields) -> None:
        if self.project is None:
            return
        from ...audit import AuditLog
        AuditLog(Path(self.project.root) / "audit" / "audit.jsonl").record("exec", action, **fields)

    def run(self, job: ExecJob, *, internal_network: str | None = None,
            secret_values: Mapping[str, str] | None = None) -> ExecResult:
        """Run one job to completion (ExecBackend.run). `internal_network` (test phase only) attaches the job to
        a backend-created `--internal` network instead of `none`, so it can reach harness services but nothing
        outside. `secret_values` supplies values for names in `job.synthetic_secrets` shared with services."""
        if self.project is None:
            return ExecResult(run_id=job.run_id, exit_code=None, error="DockerBackend needs a project folder")
        try:
            return self._run_job(job, internal_network, secret_values)
        except PolicyViolation as e:
            return self._error(job, str(e))
        except _BackendError as e:
            return self._error(job, str(e))
        except Exception as e:  # noqa: BLE001 - run() never raises for job/backend failures
            return self._error(job, f"backend failure: {type(e).__name__}: {e}")

    def _secret_values(self, job: ExecJob, given: Mapping[str, str] | None) -> dict[str, str]:
        given = dict(given or {})
        unknown = set(given) - set(job.synthetic_secrets)
        if unknown:
            raise PolicyViolation([f"secret value for {sorted(unknown)} not declared in synthetic_secrets"])
        return {k: given.get(k) or synthetic_value() for k in job.synthetic_secrets}

    def _run_job(self, job: ExecJob, internal_network: str | None,
                 secret_values: Mapping[str, str] | None) -> ExecResult:
        from ..paths import Phase2Layout
        root = self.root
        check_job(job, root, environ=self._environ)
        if internal_network is not None and job.phase != "test":
            raise PolicyViolation(["internal_network is only for test-phase jobs"])
        ok, reason = self.available()
        if not ok:
            raise _BackendError(reason)
        img_id, digest = self.image_ref(job.image)
        lay = Phase2Layout(self.project)
        rep = lay.ensure(lay.run_work(job.run_id) / "reports")
        os.chmod(rep, 0o777)   # container uid may differ from ours (rootless / Linux); folder is per run
        synth = self._secret_values(job, secret_values)
        name = self.container_name(job.run_id)
        extra_mounts = [(rep, REPORTS_TARGET, False)]
        extra_env = {"MA_REPORTS_DIR": REPORTS_TARGET}
        cleanup: list[list[str]] = []
        network_args: list[str]
        effective = "none"
        reports: dict[str, bytes] = {}
        try:
            if job.phase == "test":
                if internal_network is not None:
                    network_args = ["--network", self._validated_internal(internal_network)]
                    effective = "internal"
                else:
                    network_args = ["--network", "none"]
            elif job.network == "mirror" and self.config.hosts:
                net, proxy = self._start_mirror(job, cleanup)
                proxy_url = f"http://{PROXY_ALIAS}:{PROXY_PORT}"
                network_args = ["--network", net]
                extra_env.update({"HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "http_proxy": proxy_url,
                                  "https_proxy": proxy_url, "NO_PROXY": "localhost,127.0.0.1",
                                  "no_proxy": "localhost,127.0.0.1"})
                effective = "mirror"
                cleanup.insert(0, ["__logs__", proxy])
            else:
                network_args = ["--network", "none"]
            argv, idx = self.build_argv(job, image_ref=img_id, name=name, network_args=network_args,
                                        extra_mounts=extra_mounts, synthetic=synth, extra_env=extra_env)
            check_argv(argv, stop_at=idx)
            self._audit("exec_start", run_id=job.run_id, phase=job.phase, network=effective, image=job.image,
                        image_digest=digest, docker_argv=argv[:idx])
            started = _now()
            rc, out, err, timed_out, dur = self._run_container(argv, name=name, timeout=job.limits.timeout_s,
                                                               secret_env=synth)
            finished = _now()
            state = self._inspect_state(name)
            if state is None and not timed_out and rc in (125, None):
                raise _BackendError("container did not start: " + _scrub(err, synth).strip()[-500:])
            oom, code = state if state is not None else (False, rc)
            exit_code = None if timed_out else (code if code is not None else rc)
            for c in cleanup:
                if c[0] == "__logs__":
                    lg = self.docker(["logs", c[1]], timeout=60)
                    reports["egress.log"] = ((lg.stdout or "") + (lg.stderr or "")).encode()[-MAX_REPORT_BYTES:]
            reports.update(self._collect_reports(rep))
            return record_run(self.project, job, exit_code=exit_code, stdout=_scrub(out, synth),
                              stderr=_scrub(err, synth), duration_s=dur, timed_out=timed_out, oom_killed=oom,
                              reports={k: v for k, v in reports.items()}, image_digest=digest,
                              started_at=started, finished_at=finished,
                              error="timed out (killed)" if timed_out else "")
        finally:
            self.docker(["rm", "--force", "--volumes", name], timeout=60)
            for c in cleanup:
                if c[0] != "__logs__":
                    self.docker(c, timeout=60)
            shutil.rmtree(rep, ignore_errors=True)

    def _start_mirror(self, job: ExecJob, cleanup: list[list[str]]) -> tuple[str, str]:
        """Per-run internal network + allowlisting Squid proxy (the only container with a route out)."""
        from ..paths import Phase2Layout
        from ..state import atomic_write_text
        lay = Phase2Layout(self.project)
        net = f"ma-build-{self.project_tag}-{job.run_id.lower()}"
        proxy = self.container_name(job.run_id, "proxy")
        proxy_id, proxy_digest = self.image_ref(self.config.proxy_image)
        conf = atomic_write_text(lay.run_work(job.run_id) / "proxy" / "squid.conf",
                                 render_squid_conf(self.config.hosts, self.config.ports))
        r = self.docker(["network", "create", "--internal", "--label", "ma.managed=1",
                         "--label", f"ma.project={self.project_tag}", net], timeout=30)
        if r.returncode != 0:
            raise _BackendError(f"cannot create build network: {(r.stderr or '').strip()[:200]}")
        cleanup.append(["network", "rm", net])
        cleanup.insert(0, ["rm", "--force", proxy])
        r = self.docker(["run", "--detach", "--name", proxy, "--pull", "never", "--network", "bridge",
                         "--read-only", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
                         "--security-opt", "no-new-privileges:true", "--pids-limit", "128", "--memory", "256m",
                         "--tmpfs", "/var/run/squid:rw,size=8m", "--tmpfs", "/var/log/squid:rw,size=32m",
                         "--tmpfs", "/var/spool/squid:rw,size=32m", "--tmpfs", "/tmp:rw,size=16m",
                         "--label", "ma.managed=1", "--label", f"ma.project={self.project_tag}",
                         "--label", f"ma.run={job.run_id}", "--label", "ma.role=egress-proxy",
                         "--mount", f"type=bind,source={conf},target=/etc/squid/squid.conf,readonly",
                         "--entrypoint", "squid", proxy_id, "-f", "/etc/squid/squid.conf", "-N", "-d", "1"],
                        timeout=60)
        if r.returncode != 0:
            raise _BackendError(f"cannot start egress proxy: {(r.stderr or '').strip()[:200]}")
        r = self.docker(["network", "connect", "--alias", PROXY_ALIAS, net, proxy], timeout=30)
        if r.returncode != 0:
            raise _BackendError(f"cannot attach egress proxy: {(r.stderr or '').strip()[:200]}")
        self._audit("exec_mirror", run_id=job.run_id, hosts=self.config.hosts, ports=self.config.ports,
                    proxy_image=self.config.proxy_image, proxy_digest=proxy_digest, network=net)
        return net, proxy

    # ---- long-running services (harness up)
    def start_service(self, job: ExecJob, *, network: str, aliases: list[str] = (),
                      secret_values: Mapping[str, str] | None = None, extra_tmpfs: list[str] = ()) -> str:
        """Start a detached, disposable (`--rm`) test-phase service on a backend-created internal network.
        Returns the container name. Raises PolicyViolation / RuntimeError on refusal or failure."""
        if self.project is None:
            raise RuntimeError("start_service needs a project")
        check_job(job, self.root, environ=self._environ)
        if job.phase != "test":
            raise PolicyViolation(["services run in the test phase (no route out)"])
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(reason)
        try:
            img_id, digest = self.image_ref(job.image)
        except _BackendError as e:
            raise RuntimeError(str(e)) from None
        net = self._validated_internal(network)
        synth = self._secret_values(job, secret_values)
        name = self.container_name(job.run_id, job.labels.get("service", "svc"))
        net_args = ["--network", net]
        for a in aliases:
            if not _NAME.match(a):
                raise PolicyViolation([f"invalid network alias {a!r}"])
            net_args += ["--network-alias", a]
        try:
            argv, idx = self.build_argv(job, image_ref=img_id, name=name, network_args=net_args, synthetic=synth,
                                        detach=True, extra_tmpfs=list(extra_tmpfs))
        except _BackendError as e:
            raise RuntimeError(str(e)) from None
        check_argv(argv, stop_at=idx)
        exe = self._exe()
        try:
            r = self._run([exe, *argv], capture_output=True, text=True, timeout=120, check=False,
                          env=self._cli_env(synth), shell=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"docker run failed: {type(e).__name__}") from None
        self._audit("exec_service", run_id=job.run_id, service=job.labels.get("service", ""), image=job.image,
                    image_digest=digest, network=net, exit_code=r.returncode, docker_argv=argv[:idx])
        if r.returncode != 0:
            raise RuntimeError(f"service {name} did not start: "
                               + _scrub(redact_string((r.stderr or "").strip()), synth)[-300:])
        return name

    def exec_in(self, container: str, argv: list[str], *, timeout: float = 30) -> tuple[int, str]:
        """Run argv inside a running harness container (health checks). Same sandbox, same user."""
        if not _NAME.match(container) or not container.startswith(f"ma-{self.project_tag}-"):
            raise PolicyViolation([f"container {container!r} is not managed by this project"])
        r = self.docker(["exec", container, *argv], timeout=timeout)
        return r.returncode, redact_string((r.stdout or "") + (r.stderr or ""))[-4096:]

    def remove_containers(self, names: list[str]) -> None:
        names = [n for n in names if _NAME.match(n) and n.startswith(f"ma-{self.project_tag}-")]
        if names:
            self.docker(["rm", "--force", *names], timeout=120)

    def list_managed(self, **labels: str) -> list[dict]:
        args = ["ps", "--all", "--filter", f"label=ma.project={self.project_tag}"]
        for k, v in sorted(labels.items()):
            args += ["--filter", f"label=ma.l.{k}={v}"]
        r = self.docker(args + ["--format", "{{json .}}"], timeout=30)
        if r.returncode != 0:
            return []
        out = []
        for line in (r.stdout or "").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append({"name": d.get("Names", ""), "state": d.get("State", ""), "status": d.get("Status", ""),
                            "image": d.get("Image", "")})
        return out
