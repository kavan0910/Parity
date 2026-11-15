"""Static safety scan run before EVERY sandbox job (docs/phase2-plan.md section 6, threat model T19-T26).

`ExecJob` validation already rejects the obvious cases, but a job can be built with `model_construct` (no
validation), mounts can be symlinks, and the final docker argv is assembled later. This module re-checks the job
against the project on disk and checks the assembled argv, so a backend never starts a container that:

* mounts the docker socket, a host path outside the project, a home/credential folder or the whole project;
* runs a test-phase job with any network, or a non-build job with the mirror network;
* passes env that is (or looks like) a secret: secret-looking names, values matching ingest secret patterns,
  credential prefixes (model, cloud, git, docker), or the exact value of any secret in the host environment;
* carries privileged docker flags (`--privileged`, `--cap-add`, `--device`, host namespaces, unconfined
  seccomp/apparmor, `--volumes-from`, root user, ...).

    violations = scan_job(job, project_root)          # [] when the job is acceptable
    check_job(job, project_root)                      # raises PolicyViolation with every reason
    scan_argv(argv) / check_argv(argv)                # the docker argv the backend is about to run
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from ...ingest.secrets import redact_text


class PolicyViolation(ValueError):
    """The job or argv breaks the sandbox policy. `.violations` lists every reason."""

    def __init__(self, violations: list[str]):
        super().__init__("sandbox policy violation: " + "; ".join(violations))
        self.violations = list(violations)


# project-relative top-level folders a job may mount (everything else in the project stays out of containers)
MOUNTABLE_ROOTS = ("legacy_src", "target_code", "runs", "harness", "characterization")

# env names that look like credentials: must be synthetic_secrets (fresh random value per run), never real values
SECRET_NAME = re.compile(
    r"(?i)(?:^|_)(?:API_?KEY|APIKEY|SECRET|SECRETS|TOKEN|TOKENS|PASSWORD|PASSWD|PASSPHRASE|PWD|CREDENTIALS?|"
    r"PRIVATE_?KEY|ACCESS_?KEY|AUTH|SESSION_?KEY|SIGNING_?KEY|CLIENT_?SECRET)(?:_|$)")
# credential families that never enter a container (not even as synthetic names)
FORBIDDEN_ENV_PREFIXES = (
    "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_", "AZURE_", "AWS_", "GCP_", "GCLOUD_", "CLOUDSDK_", "LITELLM_",
    "MA_LLM_", "GITHUB_", "GH_", "GITLAB_", "GIT_", "DOCKER_", "SSH_", "KUBE", "VAULT_", "NPM_TOKEN", "HF_",
    "HUGGING", "COHERE_", "MISTRAL_", "GROQ_", "XAI_", "DEEPSEEK_", "OPENROUTER_",
)
FORBIDDEN_ENV_NAMES = frozenset({"MA_MAX_USD", "NETRC", "KUBECONFIG", "SSH_AUTH_SOCK", "LD_PRELOAD",
                                 "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV"})
# names the backend sets itself in mirror builds
RESERVED_ENV_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
                                "ALL_PROXY", "all_proxy"})

# path components that mark credential / home material on the host
_SENSITIVE_PARTS = frozenset({".ssh", ".aws", ".azure", ".gnupg", ".docker", ".kube", ".config", ".netrc",
                              ".npmrc", ".pypirc", ".git-credentials", ".gcloud", ".local", "docker.sock",
                              "containerd.sock", "podman.sock", "crio.sock"})
# container paths that must never be mount targets
_FORBIDDEN_TARGETS = ("/proc", "/sys", "/dev", "/var/run", "/run", "/etc", "/bin", "/sbin", "/usr", "/lib",
                      "/lib64", "/boot", "/root")
_SOCKET_NAMES = re.compile(r"(?i)(?:docker|containerd|podman|crio|buildkit)[\w.-]*\.sock$")


def _host_secret_values(environ: Mapping[str, str] | None = None) -> set[str]:
    """Values of secret-looking variables in the orchestrator's own environment (never forwarded)."""
    env = os.environ if environ is None else environ
    out = set()
    for k, v in env.items():
        if k in ("PWD", "OLDPWD", "PATH", "HOME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "DOCKER_HOST"):
            continue
        if v and len(v) >= 8 and (SECRET_NAME.search(k) or k.upper().startswith(FORBIDDEN_ENV_PREFIXES)):
            out.add(v)
    return out


def env_name_problem(name: str) -> str | None:
    up = name.upper()
    if name in FORBIDDEN_ENV_NAMES or up in FORBIDDEN_ENV_NAMES:
        return f"env {name} is not allowed in sandbox jobs"
    if up.startswith(FORBIDDEN_ENV_PREFIXES):
        return f"env {name} belongs to a credential family (model, cloud, git, docker) that never enters a container"
    if name in RESERVED_ENV_NAMES:
        return f"env {name} is set by the backend (mirror proxy), not by jobs"
    return None


def scan_env(env: Mapping[str, str], synthetic: Iterable[str] = (),
             environ: Mapping[str, str] | None = None) -> list[str]:
    out: list[str] = []
    host_secrets = _host_secret_values(environ)
    for k, v in env.items():
        p = env_name_problem(k)
        if p:
            out.append(p)
            continue
        if SECRET_NAME.search(k):
            out.append(f"env {k} looks like a secret name; list it in synthetic_secrets instead of passing a value")
            continue
        sval = str(v)
        if redact_text("", f"{k}={sval}")[1] or redact_text("", sval)[1]:
            out.append(f"env {k} has a secret-looking value")
        elif sval in host_secrets or any(len(s) >= 12 and s in sval for s in host_secrets):
            out.append(f"env {k} carries a credential from the orchestrator environment")
    for k in synthetic:
        p = env_name_problem(k)
        if p:
            out.append(p)
    return out


def scan_mount_source(project_root: Path, source: str) -> list[str]:
    out: list[str] = []
    root = Path(project_root).resolve()
    s = source.replace("\\", "/")
    parts = PurePosixPath(s).parts
    if s.startswith("/") or (len(s) > 1 and s[1] == ":") or ".." in parts or "\x00" in s:
        return [f"mount {source!r}: source must be project-relative without '..'"]
    if not parts or parts[0] in (".", "") or s.strip("/.") == "":
        return [f"mount {source!r}: the whole project folder is never mounted"]
    if parts[0] not in MOUNTABLE_ROOTS:
        out.append(f"mount {source!r}: only {', '.join(MOUNTABLE_ROOTS)} may be mounted")
    if any(p.lower() in _SENSITIVE_PARTS or _SOCKET_NAMES.search(p) for p in parts):
        out.append(f"mount {source!r}: credential/home/socket path refused")
    p = root / s
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        return out + [f"mount {source!r}: cannot resolve"]
    if not resolved.is_relative_to(root):
        out.append(f"mount {source!r}: resolves outside the project folder ({'symlink' if p.is_symlink() else 'path'})")
    else:
        rparts = resolved.relative_to(root).parts
        if not rparts or rparts[0] not in MOUNTABLE_ROOTS:
            out.append(f"mount {source!r}: resolves to a project area that is never mounted")
    if _SOCKET_NAMES.search(resolved.name) or resolved.name in ("docker.sock",) or (
            resolved.exists() and resolved.is_socket()):
        out.append(f"mount {source!r}: sockets (docker/containerd) are never mounted")
    if resolved == Path.home().resolve():
        out.append(f"mount {source!r}: home directories are never mounted")
    return out


def scan_mount_target(target: str) -> list[str]:
    t = target.rstrip("/") or "/"
    if t == "/":
        return [f"mount target {target!r}: cannot mount over /"]
    for f in _FORBIDDEN_TARGETS:
        if t == f or t.startswith(f + "/"):
            return [f"mount target {target!r}: system path {f} is never a mount target"]
    if _SOCKET_NAMES.search(t):
        return [f"mount target {target!r}: socket paths refused"]
    return []


_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$")


def scan_job(job, project_root: Path | str | None, *, environ: Mapping[str, str] | None = None) -> list[str]:
    """Every policy violation of `job` (an ExecJob, validated or not). Empty list = acceptable."""
    out: list[str] = []
    phase = getattr(job, "phase", None)
    network = getattr(job, "network", None)
    if phase not in ("build", "test"):
        out.append(f"unknown phase {phase!r}")
    if network not in ("none", "mirror"):
        out.append(f"network {network!r} is not allowed (only none, or mirror for build jobs)")
    if phase == "test" and network != "none":
        out.append("test-phase jobs have no network (network must be 'none')")
    if network == "mirror" and phase != "build":
        out.append("the mirror network is only for build-phase jobs")
    image = str(getattr(job, "image", "") or "")
    if not _IMAGE.match(image):
        out.append(f"image reference {image!r} is not a plain image name/digest")
    cmd = list(getattr(job, "command", []) or [])
    if not cmd or not all(isinstance(a, str) for a in cmd):
        out.append("command must be a non-empty argv list of strings")
    elif any("\x00" in a for a in cmd):
        out.append("NUL byte in command")
    wd = str(getattr(job, "workdir", "/work"))
    if not wd.startswith("/") or ".." in wd.split("/"):
        out.append("workdir must be an absolute container path without '..'")
    for m in getattr(job, "mounts", []) or []:
        if project_root is None:
            out.append("mounts need a project folder")
            break
        out += scan_mount_source(Path(project_root), str(m.source))
        out += scan_mount_target(str(m.target))
    env = dict(getattr(job, "env", {}) or {})
    synth = list(getattr(job, "synthetic_secrets", []) or [])
    out += scan_env(env, synth, environ)
    if set(env) & set(synth):
        out.append("a name cannot be both in env and synthetic_secrets")
    for k, v in (getattr(job, "labels", {}) or {}).items():
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", str(k)) or "\n" in str(v) or "\x00" in str(v):
            out.append(f"invalid label {k!r}")
    return list(dict.fromkeys(out))


def check_job(job, project_root: Path | str | None, *, environ: Mapping[str, str] | None = None) -> None:
    v = scan_job(job, project_root, environ=environ)
    if v:
        raise PolicyViolation(v)


# ------------------------------------------------------------------ docker argv
_FLAG_VALUE_RULES: dict[str, str] = {
    "--network": "host", "--net": "host", "--pid": "host", "--ipc": "host", "--uts": "host", "--userns": "host",
    "--cgroupns": "host",
}
_ALWAYS_FORBIDDEN = ("--privileged", "--cap-add", "--device", "--device-cgroup-rule", "--volumes-from",
                     "--security-opt=seccomp=unconfined", "--group-add", "--oom-kill-disable", "--add-host",
                     "--dns", "--publish-all", "-P")


def _volume_problem(spec: str) -> str | None:
    src = spec.split(":", 1)[0] if not spec.startswith("type=") else ""
    if spec.startswith("type=") or "," in spec and "src=" in spec:
        m = re.search(r"(?:src|source)=([^,]+)", spec)
        src = m.group(1) if m else ""
    if _SOCKET_NAMES.search(src) or src.rstrip("/").endswith((".sock",)) or src.rstrip("/") in ("/", "/var/run",
                                                                                                "/run"):
        return f"volume {spec!r}: socket or system path refused"
    home = str(Path.home())
    if src.rstrip("/") == home.rstrip("/"):
        return f"volume {spec!r}: home directory refused"
    if any(p in _SENSITIVE_PARTS for p in Path(src).parts):
        return f"volume {spec!r}: credential path refused"
    return None


def scan_argv(argv: list[str], stop_at: int | None = None) -> list[str]:
    """Violations in an assembled `docker ...` argv. `stop_at` = index of the image: the container's own
    command after it is data for the container, not docker flags."""
    out: list[str] = []
    if not argv or not all(isinstance(a, str) for a in argv):
        return ["argv must be a non-empty list of strings"]
    end = len(argv) if stop_at is None else min(stop_at, len(argv))
    i = 0
    while i < end:
        a = argv[i]
        name, eq, val = a.partition("=")
        nxt = val if eq else (argv[i + 1] if i + 1 < end else "")
        if a in _ALWAYS_FORBIDDEN or name in _ALWAYS_FORBIDDEN:
            out.append(f"privileged flag {name} refused")
        elif name in _FLAG_VALUE_RULES and nxt == _FLAG_VALUE_RULES[name]:
            out.append(f"{name} {nxt} refused")
        elif name == "--security-opt" and ("unconfined" in nxt or nxt.startswith("no-new-privileges:false")):
            out.append(f"--security-opt {nxt} refused")
        elif name in ("-v", "--volume", "--mount"):
            p = _volume_problem(nxt)
            if p:
                out.append(p)
        elif name in ("-u", "--user") and nxt.split(":", 1)[0] in ("0", "root", ""):
            out.append("root user refused")
        elif name in ("-p", "--publish") and not nxt.startswith("127.0.0.1:"):
            out.append(f"--publish {nxt} refused (loopback only)")
        i += 1
    return list(dict.fromkeys(out))


def check_argv(argv: list[str], stop_at: int | None = None) -> None:
    v = scan_argv(argv, stop_at)
    if v:
        raise PolicyViolation(v)
