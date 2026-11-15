"""Helpers for the Phase 2 integration tests: a scripted docker CLI (FakeDocker) whose runner jobs really execute
the staged stdlib-only runner (python -S: no site-packages, so no pydantic) against in-process services."""
from __future__ import annotations

import subprocess
import sys

from migration_agent.migrate import exec as p2exec
from migration_agent.migrate import harness as H

from p2_exec_helpers import IMG_ID, FakeDocker


def runner_fake(monkeypatch, urls: dict[str, str] | None = None, **kw) -> FakeDocker:
    """FakeDocker with the runner image present. Runner jobs (`/runner/netrunner.py`) run the staged code with
    `sys.executable -S`; in-network base URLs are rewritten through `urls` (e.g. {"http://legacy:3000": local})."""
    fd = FakeDocker(images={H.RUNNER_IMAGE: (IMG_ID, []), "img:1": (IMG_ID, [])}, **kw)
    fd.runner_runs = []
    fd.urls = dict(urls or {})

    def on_run(a):
        if "/runner/netrunner.py" not in a:
            return
        i = a.index("/runner/netrunner.py")
        args = list(a[i + 1:])
        host = {t: fd.mount_target_host(a, t) for t in ("/runner", "/cases", "/reports")}

        def tr(v: str) -> str:
            for t, h in host.items():
                if v.startswith(t + "/"):
                    return str(h / v[len(t) + 1:])
            return fd.urls.get(v, v)
        args = [tr(x) for x in args]
        fd.runner_runs.append(a)
        r = subprocess.run([sys.executable, "-S", "-E", "-s", "-B", str(host["/runner"] / "netrunner.py"), *args],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode in (0, 1), r.stderr

    fd.on_run = on_run
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fd.backend(project))
    return fd


def fake_up(p, fd: FakeDocker, role: str, alias: str, port: int) -> str:
    """Mark a harness role as up (state.json) on an internal network the fake docker knows."""
    name = fd.backend(p).internal_network_name(role)
    fd.nets[name] = True
    st = H.load_state(p)
    ent = st.get(role) or {}
    ent.update({"ok": True, "up": {"network": name, "containers": [f"ma-x-{role}"], "service": f"ma-x-{role}",
                                   "alias": alias, "port": port}})
    st[role] = ent
    H.save_state(p, st)
    return name
