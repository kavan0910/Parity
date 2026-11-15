"""Phase 2 file I/O helpers and the per-slice state machine (docs/phase2-plan.md 5.1).

I/O: every Phase 2 JSON file is written with `atomic_write_json` / `atomic_write_text` (temp file in the same
folder + os.replace, the graph/store.py pattern), so a crash never leaves a half-written file and any session
can resume from the folder (Q18).

State machine (slices/SLICE-NNNN/state.json, model SliceState):

    planned -> tests_recorded -> generating -> built -> tests_passing -> reviewed -> awaiting_human
        -> approved | rejected
    fix loop:   built | tests_passing | reviewed | awaiting_human -> generating   (iteration += 1)
    retry:      generating -> generating (iteration += 1); rejected -> generating | planned
    reopen:     approved -> generating (e.g. a human commit in target_code/ invalidated the approval)
    blocked:    any status except approved -> blocked (reason required); blocked -> the status it was
                blocked from, or planned

`transition()` is the only writer of state.json. Illegal transitions raise IllegalTransition and change nothing.
Each transition is appended to the state history and recorded in audit/audit.jsonl (agent "slice_runner").
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TypeVar

from pydantic import BaseModel

from .models import SliceState, SliceStatus, StateTransition

M = TypeVar("M", bound=BaseModel)


# ------------------------------------------------------------------ I/O helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def atomic_write_json(path: str | Path, obj: BaseModel | dict | list) -> Path:
    if isinstance(obj, BaseModel):
        text = obj.model_dump_json(indent=2)
    else:
        text = json.dumps(obj, indent=2, sort_keys=True, default=str)
    return atomic_write_text(path, text + "\n")


def read_json(path: str | Path, model: type[M] | None = None, default=None):
    """Parse `path` (into `model` if given). Missing file -> `default`. Corrupt file raises (never guessed)."""
    path = Path(path)
    if not path.exists():
        return default
    text = path.read_text()
    return model.model_validate_json(text) if model is not None else json.loads(text)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Advisory exclusive lock on `<path>.lock` (cross-process read-modify-write)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ------------------------------------------------------------------ slice state machine
STATUSES: tuple[str, ...] = ("planned", "tests_recorded", "generating", "built", "tests_passing", "reviewed",
                             "awaiting_human", "approved", "rejected", "blocked")
TERMINAL_OK = "approved"
REASON_REQUIRED = frozenset({"blocked", "rejected"})

# static edges; `blocked` exits are dynamic (see allowed_next)
TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"tests_recorded"}),
    "tests_recorded": frozenset({"generating"}),
    "generating": frozenset({"built", "generating"}),
    "built": frozenset({"tests_passing", "generating"}),
    "tests_passing": frozenset({"reviewed", "generating"}),
    "reviewed": frozenset({"awaiting_human", "generating"}),
    "awaiting_human": frozenset({"approved", "rejected", "generating"}),
    "approved": frozenset({"generating"}),
    "rejected": frozenset({"generating", "planned"}),
    "blocked": frozenset(),
}


class IllegalTransition(ValueError):
    pass


def allowed_next(state: SliceState) -> frozenset[str]:
    if state.status == "blocked":
        return frozenset({state.blocked_from or "planned", "planned"})
    nxt = set(TRANSITIONS[state.status])
    if state.status != "approved":
        nxt.add("blocked")
    return frozenset(nxt)


def can_transition(state: SliceState, to_status: str) -> bool:
    return to_status in allowed_next(state)


def apply_transition(state: SliceState, to_status: SliceStatus, *, actor: str = "agent", reason: str = "",
                     at: str | None = None) -> SliceState:
    """Pure: the new state after a validated transition (raises IllegalTransition)."""
    if to_status not in STATUSES:
        raise IllegalTransition(f"unknown status {to_status!r}")
    if not can_transition(state, to_status):
        raise IllegalTransition(f"{state.slice_id}: {state.status} -> {to_status} is not allowed "
                                f"(allowed: {', '.join(sorted(allowed_next(state)))})")
    reason = reason.strip()
    if to_status in REASON_REQUIRED and not reason:
        raise IllegalTransition(f"{state.slice_id}: a reason is required to enter {to_status!r}")
    at = at or now_iso()
    hist = state.history + [StateTransition(from_status=state.status, to_status=to_status, at=at, actor=actor,
                                            reason=reason)]
    return state.model_copy(update={
        "status": to_status,
        "iteration": state.iteration + (1 if to_status == "generating" else 0),
        "blocked_from": state.status if to_status == "blocked" else None,
        "reason": reason,
        "history": hist,
    })


def _path(project, slice_id: str) -> Path:
    from .paths import Phase2Layout
    return Phase2Layout(project).slice_state(slice_id)


def load_state(project, slice_id: str) -> SliceState:
    """Current state; a slice without state.json is `planned`."""
    st = read_json(_path(project, slice_id), SliceState)
    return st if st is not None else SliceState(slice_id=slice_id)


def save_state(project, state: SliceState) -> Path:
    """Low-level write (no validation of the change). Use `transition` for status changes."""
    from .paths import Phase2Layout
    lay = Phase2Layout(project)
    lay.ensure(lay.slice_dir(state.slice_id))
    return atomic_write_json(lay.slice_state(state.slice_id), state)


def transition(project, slice_id: str, to_status: SliceStatus, *, actor: str = "agent",
               reason: str = "") -> SliceState:
    """Validated, locked, atomic status change of one slice, audited."""
    path = _path(project, slice_id)
    with file_lock(path):
        cur = load_state(project, slice_id)
        new = apply_transition(cur, to_status, actor=actor, reason=reason)
        save_state(project, new)
    from ..audit import AuditLog
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "slice_runner", "slice_transition", slice=slice_id, from_status=cur.status, to_status=to_status,
        actor=actor, reason=reason[:300], iteration=new.iteration)
    return new
