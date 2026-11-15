"""Phase 2 ID allocator: SLICE-, CT-, RISK-, EQ-, RUN- IDs, persisted in migration/ids.json.

Same rules as graph/registry.py: IDs are never reused or renumbered, and an optional identity key maps to
a stable ID (re-planning the same slice key returns the same SLICE id). Unlike the graph registry, several
Phase 2 processes may allocate at once (e.g. two sandbox runs), so every allocation is a locked
read-modify-write: an advisory `flock` on migration/ids.json.lock, then an atomic replace.

    from migration_agent.migrate.ids import allocate
    run_id = allocate(project, "RUN")                       # RUN-0001, RUN-0002 ...
    sid = allocate(project, "SLICE", key="entry:loans")     # same key -> same id forever
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import PREFIXES, format_id
from .paths import Phase2Layout


class IdAllocator:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._read()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"counters": {}, "entries": {}}
        data = json.loads(self.path.read_text())
        counters = {k: int(v) for k, v in data.get("counters", {}).items()}
        entries = {p: dict(v) for p, v in data.get("entries", {}).items()}
        for p, m in entries.items():
            if len(set(m.values())) != len(m):
                raise ValueError(f"corrupt {self.path.name}: duplicate {p} ids")
        return {"counters": counters, "entries": entries}

    def _write(self, data: dict) -> None:
        from .state import atomic_write_text
        payload = {"counters": dict(sorted(data["counters"].items())),
                   "entries": {p: dict(sorted(m.items(), key=lambda kv: kv[1]))
                               for p, m in sorted(data["entries"].items())}}
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")

    @staticmethod
    def _check(prefix: str) -> None:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown Phase 2 id prefix {prefix!r} (one of {', '.join(PREFIXES)})")

    @staticmethod
    def _alloc(data: dict, prefix: str, key: str | None) -> str:
        keyed = data["entries"].setdefault(prefix, {})
        if key is not None and key in keyed:
            return keyed[key]
        n = data["counters"].get(prefix, 0) + 1
        data["counters"][prefix] = n
        new = format_id(prefix, n)
        if key is not None:
            keyed[key] = new
        if not keyed:
            data["entries"].pop(prefix, None)
        return new

    def allocate(self, prefix: str, key: str | None = None) -> str:
        """Next ID for `prefix`; with `key`, the ID already bound to that key (allocating it once)."""
        self._check(prefix)
        with self._locked() as data:
            return self._alloc(data, prefix, key)

    def allocate_many(self, prefix: str, keys: list[str]) -> dict[str, str]:
        """Batch, one lock: new keys are allocated in sorted order so IDs do not depend on input order."""
        self._check(prefix)
        with self._locked() as data:
            for k in sorted(set(keys)):
                self._alloc(data, prefix, k)
            return {k: data["entries"][prefix][k] for k in keys}

    def lookup(self, prefix: str, key: str) -> str | None:
        self._check(prefix)
        return self._read()["entries"].get(prefix, {}).get(key)

    def last(self, prefix: str) -> int:
        """Highest number allocated so far for `prefix` (0 if none)."""
        self._check(prefix)
        return self._read()["counters"].get(prefix, 0)


def allocator(project) -> IdAllocator:
    layout = Phase2Layout(project)
    layout.ensure(layout.migration)
    return IdAllocator(layout.ids_file)


def allocate(project, prefix: str, key: str | None = None) -> str:
    return allocator(project).allocate(prefix, key)
