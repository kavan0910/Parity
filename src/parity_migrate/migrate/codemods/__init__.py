"""Deterministic codemods run in the exec sandbox before the model (docs/phase2-plan.md 3).

    results = run_codemods(project, spec, backend=None)    # list[CodemodResult], applied ones committed

Adapters: openrewrite.OpenRewriteAdapter (Java 8 -> 21, Boot 2 -> 3; Maven and Gradle), django_upgrade.DjangoUpgradeAdapter
(django-upgrade + pyupgrade), lebab.LebabAdapter (JS to JS modernisation; alias JscodeshiftAdapter). Interface and runner in base.py.
Results are recorded in migration/codemods.json by the slice runner, so applied codemods run once.
"""
from __future__ import annotations

from .base import Adapter, CodemodResult, run_codemod, tree_changes
from .django_upgrade import DjangoUpgradeAdapter
from .lebab import JscodeshiftAdapter, LebabAdapter
from .openrewrite import OpenRewriteAdapter

ADAPTERS = (OpenRewriteAdapter, DjangoUpgradeAdapter, LebabAdapter)


def run_codemods(project, spec, *, backend=None, adapters=None) -> list[CodemodResult]:
    out = []
    for cls in adapters or ADAPTERS:
        a = cls() if isinstance(cls, type) else cls
        r = run_codemod(project, a, spec, backend=backend)
        if r.status != "not_applicable":
            out.append(r)
    return out


__all__ = ["Adapter", "CodemodResult", "run_codemod", "run_codemods", "tree_changes", "ADAPTERS",
           "OpenRewriteAdapter", "DjangoUpgradeAdapter", "LebabAdapter", "JscodeshiftAdapter"]
