"""Scripted docker CLI for DockerBackend tests: records every argv, never runs a process."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

IMG_ID = "sha256:" + "ab" * 32
REPO_DIGEST = "sha256:" + "cd" * 32
PROXY_ID = "sha256:" + "ef" * 32


class FakeProc:
    def __init__(self, rc, out: bytes, err: bytes, hang: bool):
        self.stdout, self.stderr = io.BytesIO(out), io.BytesIO(err)
        self._rc, self._hang, self.killed = rc, hang, False

    def wait(self, timeout=None):
        if self._hang:
            self._hang = False
            raise subprocess.TimeoutExpired("docker", timeout)
        return self._rc

    def kill(self):
        self.killed = True


class FakeDocker:
    def __init__(self, *, cli=True, daemon=True, runtimes=("runc",), images=None, rc=0, stdout=b"", stderr=b"",
                 hang=False, oom=False, start_fails=False, on_run=None, exec_rc=0, ps=None):
        self.cli, self.daemon, self.runtimes = cli, daemon, list(runtimes)
        self.images = dict(images if images is not None else {
            "img:1": (IMG_ID, [f"img@{REPO_DIGEST}"]),
            "ubuntu/squid:6.6-24.04_beta": (PROXY_ID, [])})
        self.rc, self.stdout, self.stderr, self.hang, self.oom = rc, stdout, stderr, hang, oom
        self.start_fails, self.on_run, self.exec_rc, self.ps = start_fails, on_run, exec_rc, ps or []
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.popen_env: dict | None = None
        self.nets: dict[str, bool] = {}
        self.containers: set[str] = set()

    # injected into DockerBackend(run=..., popen=..., which=...)
    def which(self, name):
        return "/usr/local/bin/docker" if self.cli else None

    def _cp(self, argv, rc=0, out="", err=""):
        return subprocess.CompletedProcess(argv, rc, out, err)

    def run(self, argv, **kw):
        a = list(argv[1:])
        self.calls.append(a)
        self.kwargs.append(kw)
        assert kw.get("shell") is False and isinstance(argv, list)
        if not self.daemon:
            return self._cp(argv, 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
        cmd = a[0]
        if cmd == "info":
            return self._cp(argv, 0, json.dumps({r: {} for r in self.runtimes}))
        if a[:2] == ["image", "inspect"]:
            img = self.images.get(a[-1])
            return self._cp(argv, 0, f"{img[0]}|{json.dumps(img[1])}") if img else self._cp(argv, 1, "", "No such image")
        if a[:2] == ["network", "inspect"]:
            return self._cp(argv, 0, "true" if self.nets[a[-1]] else "false") if a[-1] in self.nets else \
                self._cp(argv, 1, "", "not found")
        if a[:2] == ["network", "create"]:
            self.nets[a[-1]] = "--internal" in a
            return self._cp(argv)
        if a[:2] == ["network", "rm"]:
            self.nets.pop(a[-1], None)
            return self._cp(argv)
        if cmd == "inspect":
            name = a[-1]
            if name not in self.containers:
                return self._cp(argv, 1, "", "No such container")
            return self._cp(argv, 0, f"{'true' if self.oom else 'false'}|{self.rc}")
        if cmd == "build":
            tag = a[a.index("-t") + 1]
            self.images[tag] = (IMG_ID, [])
            return self._cp(argv, 0, "built " + tag)
        if cmd == "run":                       # detached proxy / service
            if self.start_fails:
                return self._cp(argv, 125, "", "boom")
            self.containers.add(a[a.index("--name") + 1])
            return self._cp(argv, 0, "cid\n")
        if cmd == "exec":
            return self._cp(argv, self.exec_rc, "ok" if self.exec_rc == 0 else "connection refused")
        if cmd == "logs":
            return self._cp(argv, 0, "1700000000.000 5 10.0.0.2 TCP_TUNNEL/200 0 CONNECT registry.example:443 -\n")
        if cmd == "ps":
            return self._cp(argv, 0, "\n".join(json.dumps(x) for x in self.ps))
        if cmd in ("rm", "kill"):
            for n in a[1:]:
                self.containers.discard(n)
            return self._cp(argv)
        return self._cp(argv)

    def popen(self, argv, **kw):
        a = list(argv[1:])
        self.calls.append(a)
        self.kwargs.append(kw)
        assert kw.get("shell") is False and isinstance(argv, list)
        self.popen_env = dict(kw.get("env") or {})
        if self.rc != 125:
            self.containers.add(a[a.index("--name") + 1])
        if self.on_run:
            self.on_run(a)
        return FakeProc(self.rc, self.stdout, self.stderr, self.hang)

    # helpers for assertions
    def run_argv(self) -> list[str]:
        """The attached `docker run` argv (the job container)."""
        return [c for c, k in zip(self.calls, self.kwargs) if c[0] == "run" and "stdout" in k][-1]

    def mount_target_host(self, argv: list[str], target: str) -> Path:
        for i, x in enumerate(argv):
            if x == "--mount" and f"target={target}" in argv[i + 1]:
                src = [p for p in argv[i + 1].split(",") if p.startswith("source=")][0]
                return Path(src.split("=", 1)[1])
        raise KeyError(target)

    def backend(self, project, **kw):
        from migration_agent.migrate.exec.docker import DockerBackend
        kw.setdefault("environ", {"PATH": "/usr/bin", "HOME": "/home/u"})
        return DockerBackend(project, run=self.run, popen=self.popen, which=self.which, **kw)


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, x in enumerate(argv[:-1]) if x == flag]
