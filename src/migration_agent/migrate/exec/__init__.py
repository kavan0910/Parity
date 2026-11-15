"""Sandbox execution: ExecBackend protocol (base.py), test double (fake.py), real backend (docker.py), job policy
(policy.py).

`get_backend(project)` returns the real backend when migrate/exec/docker.py exists (class DockerBackend,
constructed as DockerBackend(project)), else an UnavailableBackend whose available() explains why.
Tests monkeypatch `migration_agent.migrate.exec.get_backend` to inject a FakeExecBackend.
"""
from __future__ import annotations

import importlib


def get_backend(project=None):
    from .base import UnavailableBackend
    try:
        mod = importlib.import_module(f"{__name__}.docker")
    except ModuleNotFoundError as e:
        if e.name != f"{__name__}.docker":
            raise
        return UnavailableBackend(project, "Docker exec backend not installed yet (migrate/exec/docker.py)")
    return mod.DockerBackend(project)
