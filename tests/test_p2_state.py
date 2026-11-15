"""Slice state machine and atomic I/O helpers (migrate/state.py)."""
import json

import pytest

from migration_agent.audit import read_audit
from migration_agent.migrate.models import SliceState
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import (
    IllegalTransition, allowed_next, apply_transition, atomic_write_json, atomic_write_text, load_state, read_json,
    sha256_file, transition,
)
from migration_agent.project import Project

S = "SLICE-0001"
HAPPY = ["tests_recorded", "generating", "built", "tests_passing", "reviewed", "awaiting_human", "approved"]


@pytest.fixture()
def proj(tmp_path):
    return Project.init(tmp_path / "p", source="s")


def test_missing_state_is_planned(proj):
    st = load_state(proj, S)
    assert st.status == "planned" and st.history == [] and not Phase2Layout(proj).slices_dir.exists()


def test_happy_path_persists_and_audits(proj):
    for to in HAPPY:
        transition(proj, S, to, actor="human:alice" if to == "approved" else "agent")
    st = load_state(proj, S)
    assert st.status == "approved" and st.iteration == 1 and len(st.history) == len(HAPPY)
    assert st.history[-1].actor == "human:alice" and st.history[0].from_status == "planned"
    assert SliceState.model_validate_json(Phase2Layout(proj).slice_state(S).read_text()) == st
    recs = [e for e in read_audit(proj.audit_dir / "audit.jsonl") if e["action"] == "slice_transition"]
    assert [r["to_status"] for r in recs] == HAPPY and recs[0]["slice"] == S
    assert proj.meta().schema_version == 2


def test_illegal_transition_changes_nothing(proj):
    transition(proj, S, "tests_recorded")
    before = Phase2Layout(proj).slice_state(S).read_text()
    for bad in ("approved", "built", "planned", "nonsense"):
        with pytest.raises(IllegalTransition):
            transition(proj, S, bad)
    assert Phase2Layout(proj).slice_state(S).read_text() == before


def test_fix_loop_counts_iterations(proj):
    for to in ["tests_recorded", "generating", "built", "generating", "built", "tests_passing", "generating"]:
        transition(proj, S, to)
    assert load_state(proj, S).iteration == 3


def test_blocked_needs_reason_and_returns_to_previous(proj):
    transition(proj, S, "tests_recorded")
    transition(proj, S, "generating")
    with pytest.raises(IllegalTransition, match="reason"):
        transition(proj, S, "blocked")
    st = transition(proj, S, "blocked", reason="per-slice budget reached")
    assert st.blocked_from == "generating" and st.reason == "per-slice budget reached"
    assert allowed_next(st) == {"generating", "planned"}
    with pytest.raises(IllegalTransition):
        transition(proj, S, "built")
    st = transition(proj, S, "generating", actor="human:alice")
    assert st.blocked_from is None and st.reason == "" and st.iteration == 2


def test_reject_and_reopen(proj):
    for to in HAPPY[:-1]:
        transition(proj, S, to)
    with pytest.raises(IllegalTransition, match="reason"):
        transition(proj, S, "rejected")
    transition(proj, S, "rejected", reason="wrong rounding")
    assert transition(proj, S, "generating").status == "generating"
    for to in ["built", "tests_passing", "reviewed", "awaiting_human", "approved"]:
        transition(proj, S, to)
    st = load_state(proj, S)
    assert "blocked" not in allowed_next(st) and allowed_next(st) == {"generating"}
    assert transition(proj, S, "generating", reason="human commit").status == "generating"


def test_apply_transition_is_pure():
    st = SliceState(slice_id=S)
    new = apply_transition(st, "tests_recorded", at="t0")
    assert st.status == "planned" and new.status == "tests_recorded" and new.history[0].at == "t0"


def test_bad_slice_id_rejected(proj):
    with pytest.raises(ValueError):
        transition(proj, "../../etc", "tests_recorded")


def test_atomic_helpers(tmp_path):
    p = atomic_write_json(tmp_path / "a" / "b.json", {"b": 1, "a": [1, 2]})
    assert json.loads(p.read_text()) == {"a": [1, 2], "b": 1} and not list(p.parent.glob(".tmp-*"))
    atomic_write_json(tmp_path / "s.json", SliceState(slice_id=S))
    assert read_json(tmp_path / "s.json", SliceState).slice_id == S
    assert read_json(tmp_path / "missing.json", default={}) == {}
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(ValueError):
        read_json(tmp_path / "bad.json")
    atomic_write_text(tmp_path / "t.txt", "abc")
    assert sha256_file(tmp_path / "t.txt") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
