"""Deterministic codemods run in the exec sandbox before the model (docs/phase2-plan.md 3).

    results = run_codemods(project, spec, backend=None)    # list[CodemodResult], applied ones committed

Adapters: openrewrite.OpenRewriteAdapter (Java 8 -> 21, Boot 2 -> 3), django_upgrade.DjangoUpgradeAdapter
(django-upgrade + pyupgrade), jscodeshift.JscodeshiftAdapter (stub). Interface and runner in base.py.
Results are recorded in migration/codemods.json by the slice runner, so applied codemods run once.
"""
from __future__ import annotations

from .base import Adapter, CodemodResult, run_codemod, tree_changes
from .django_upgrade import DjangoUpgradeAdapter
from .jscodeshift import JscodeshiftAdapter
from .openrewrite import OpenRewriteAdapter

ADAPTERS = (OpenRewriteAdapter, DjangoUpgradeAdapter, JscodeshiftAdapter)


def run_codemods(project, spec, *, backend=None, adapters=None) -> list[CodemodResult]:
    out = []
    for cls in adapters or ADAPTERS:
        a = cls() if isinstance(cls, type) else cls
        r = run_codemod(project, a, spec, backend=backend)
        if r.status != "not_applicable":
            out.append(r)
    return out


__all__ = ["Adapter", "CodemodResult", "run_codemod", "run_codemods", "tree_changes", "ADAPTERS",
           "OpenRewriteAdapter", "DjangoUpgradeAdapter", "JscodeshiftAdapter"]
