"""M5 review UI: Phase 2 tabs only with Phase 2 files, slice view (git diff, equivalence, blockers), and the new
POST routes under the existing token / Origin / Content-Type / size rules; escaping."""
import json

import pytest

from migration_agent.migrate import equivalence as eq
from migration_agent.migrate.ids import allocate
from migration_agent.migrate.models import FitReport, Risk
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.state import atomic_write_json, load_state
from migration_agent.project import Project
from migration_agent.review_ui.page import render_page
from migration_agent.review_ui.server import MAX_BODY, serve

from p2_m5_helpers import Svc, commit, init_target, m5_project, standard_cases, walk_to_awaiting, write_iteration
from test_review_ui import base, req

EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>'


@pytest.fixture
def ui(tmp_path, tmp_path_factory):
    p, s1, s2 = m5_project(tmp_path, tmp_path_factory)
    with Svc("legacy") as legacy, Svc("regressed") as bad, Svc("new") as new:
        cases = standard_cases(p, s1, legacy.base_url)
        init_target(p)
        commit(p, "price.py", f"TOTAL = 10  # {EVIL}\n", "slice one")
        walk_to_awaiting(p, s1)
        write_iteration(p, s1)
        eq.run_equivalence(p, s1, legacy.base_url, bad.base_url)          # one regression
        rid = allocate(p, "RISK", key="semantic_shift:EP-0001")
        atomic_write_json(Phase2Layout(p).fit_report_json, FitReport(risks=[
            Risk(id=rid, kind="semantic_shift", severity="high", title=EVIL, item_ids=["EP-0001"])]))
        server, url = serve(p.root)
        try:
            yield dict(p=p, s1=s1, s2=s2, cases=cases, server=server, b=base(url), rid=rid,
                       rerun=lambda: eq.run_equivalence(p, s1, legacy.base_url, new.base_url))
        finally:
            server.stop()


def _post(u, route, body, **kw):
    return req(u["server"], "POST", u["b"] + route, body, **kw)


def _get(u, route):
    st, out, _ = req(u["server"], "GET", u["b"] + route)
    return st, json.loads(out)


def test_phase1_only_project_has_no_p2(tmp_path):
    p = Project.init(tmp_path / "p", source="src")
    server, url = serve(p.root)
    try:
        st, out, _ = req(server, "GET", base(url) + "/api/state")
        assert st == 200 and json.loads(out)["p2"] is None
    finally:
        server.stop()


def test_state_has_fit_plan_and_known_ids(ui):
    st, s = _get(ui, "/api/state")
    assert st == 200 and s["p2"]["present"]
    rows = s["p2"]["plan"]["slices"]
    assert [r["id"] for r in rows] == [ui["s1"], ui["s2"]]
    assert rows[0]["status"] == "awaiting_human" and rows[0]["unexplained"] == 1
    assert s["p2"]["fit"]["blockers"] == [ui["rid"]] and s["p2"]["fit"]["risks"][0]["title"] == EVIL
    assert {ui["s1"], ui["rid"], ui["cases"]["price"].id} <= set(s["p2"]["known_ids"])


def test_slice_view(ui):
    st, d = _get(ui, f"/api/p2/slice?id={ui['s1']}")
    assert st == 200
    assert d["state"]["status"] == "awaiting_human" and d["iterations"][0]["build"] is True
    assert "price.py" in d["diff"]["stat"] and EVIL in d["diff"]["diff"]       # raw JSON; page uses textContent
    assert d["equivalence"]["equivalence_pct"] < 100
    assert any("regression" in b for b in d["blockers"])
    assert d["fix_requests"]["requests"][0]["reason"] == "regression"
    assert _get(ui, "/api/p2/slice?id=SLICE-0999")[0] == 404
    assert _get(ui, "/api/p2/slice?id=../../etc")[0] == 400
    assert req(ui["server"], "GET", "/api/p2/slice?id=" + ui["s1"])[0] == 403          # token required


@pytest.mark.parametrize("route", ["/api/p2/comments", "/api/p2/approve-slice", "/api/p2/reject-slice",
                                   "/api/p2/resolve-risk", "/api/p2/approve-mask"])
def test_post_routes_keep_hardening(ui, route):
    s = ui["server"]
    assert req(s, "POST", route, {})[0] == 403                                          # no token
    assert req(s, "POST", ui["b"] + route, {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert req(s, "POST", ui["b"] + route, {}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert req(s, "POST", ui["b"] + route, {}, host="evil.example:80")[0] == 403
    assert req(s, "POST", ui["b"] + route, "x=1", headers={"Content-Type": "text/plain"})[0] == 415
    assert req(s, "POST", ui["b"] + route, b"x" * (MAX_BODY + 1))[0] == 413
    assert req(s, "POST", ui["b"] + route, "[1]")[0] == 400
    assert req(s, "POST", ui["b"] + route, {})[0] == 400                                # validation


def test_comment_happy_path_redaction_and_unknown(ui):
    st, out, _ = _post(ui, "/api/p2/comments", {"item_id": ui["s1"], "text": EVIL + " AKIAABCDEFGHIJKLMNOP",
                                                "file": "price.py", "line": 1, "author": "bob"})
    c = json.loads(out)
    assert st == 201 and c["id"] == "CM-0001" and c["file"] == "price.py" and c["line"] == 1
    assert EVIL in c["text"] and "AKIAABCDEFGHIJKLMNOP" not in c["text"]
    assert _post(ui, "/api/p2/comments", {"item_id": "SLICE-0999", "text": "x"})[0] == 404
    assert _post(ui, "/api/p2/comments", {"item_id": ui["s1"], "text": "x", "file": "../x"})[0] == 400
    assert _post(ui, "/api/p2/comments", {"item_id": ui["s1"], "text": "x", "file": "nope.py"})[0] == 404
    _, d = _get(ui, f"/api/p2/slice?id={ui['s1']}")
    assert [x["id"] for x in d["comments"]] == ["CM-0001"]


def test_resolve_risk(ui):
    assert _post(ui, "/api/p2/resolve-risk", {"risk_id": "RISK-0999", "status": "accepted", "note": "n"})[0] == 404
    _, s = _get(ui, "/api/state")
    status = s["p2"]["fit"]["statuses"][0]
    st, out, _ = _post(ui, "/api/p2/resolve-risk", {"risk_id": ui["rid"], "status": status, "note": "ok", "by": "al"})
    assert st == 201, out
    assert _get(ui, "/api/state")[1]["p2"]["fit"]["blockers"] == []


def test_approve_refused_mask_then_approve(ui):
    p, s1 = ui["p"], ui["s1"]
    st, out, _ = _post(ui, "/api/p2/approve-slice", {"slice_id": s1, "approved_by": "alice"})
    assert st == 409 and "regression" in json.loads(out)["error"]
    assert _post(ui, "/api/p2/approve-mask", {"eq_id": "EQ-0999", "by": "carol"})[0] == 404
    reg = eq.load_report(p, s1).unexplained[0]
    st, out, _ = _post(ui, "/api/p2/approve-mask", {"eq_id": reg.id, "by": "carol", "reason": "test"})
    assert st == 201 and json.loads(out)["mask"] == "$.total"
    ui["rerun"]()
    st, out, _ = _post(ui, "/api/p2/approve-slice", {"slice_id": s1, "approved_by": "alice", "note": "ok"})
    assert st == 201, out
    assert load_state(p, s1).status == "approved" and json.loads(out)["commit_sha"]
    assert _post(ui, "/api/p2/approve-slice", {"slice_id": "../x", "approved_by": "a"})[0] == 400


def test_reject_slice(ui):
    assert _post(ui, "/api/p2/reject-slice", {"slice_id": ui["s1"], "by": "alice", "reason": " "})[0] == 400
    st, out, _ = _post(ui, "/api/p2/reject-slice", {"slice_id": ui["s1"], "by": "alice", "reason": "wrong"})
    assert st == 201 and json.loads(out)["status"] == "rejected"
    assert _post(ui, "/api/p2/reject-slice", {"slice_id": ui["s1"], "by": "alice", "reason": "again"})[0] == 409


def test_page_renders_phase2_tabs_with_text_only():
    html = render_page("/tok")
    for t in ('"Target fit"', '"Plan"', '"Slice"', "/api/p2/slice?id=", "/api/p2/approve-slice", "/api/p2/approve-mask",
              "/api/p2/resolve-risk", "/api/p2/reject-slice", "/api/p2/comments"):
        assert t in html
    phase2 = html.split("// ---- Phase 2 tabs")[1].split("function renderAll")[0]
    assert "innerHTML" not in phase2 and "insertAdjacentHTML" not in phase2 and "document.write" not in phase2
    assert html.count("innerHTML") == 1                      # the existing server-escaped document section only
