"""Local review server (C-06). stdlib only, bound to 127.0.0.1, token-protected."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, ValidationError

from migration_agent.graph.models import Graph
from migration_agent.ingest.secrets import redact_string
from migration_agent.graph.store import GraphStoreError, diff_graphs, load_latest, load_snapshot, parse_graph
from migration_agent.project import Project
from migration_agent.review import approval as _approval
from migration_agent.review._io import ReviewError
from migration_agent.review.decisions import UnknownItem, add_decision, effective_list
from migration_agent.review.docrender import write_latest
from migration_agent.review_ui.markdown import anchors, render_sections
from migration_agent.review_ui.page import render_page
from migration_agent.verify.models import (
    Comment, CommentsFile, FindingsFile, Resolution, ResolutionsFile, ResolutionStatus,
)
from migration_agent.viz.graph_html import render_graph_html

MAX_BODY = 64 * 1024
MAX_TEXT = 5000
_SNAP_RE = re.compile(r"v(\d+)\.json")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class _CommentIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    author: str = Field(default="user", max_length=64)


class _ResolutionIn(BaseModel):
    finding_id: str = Field(min_length=1, max_length=64)
    status: ResolutionStatus
    note: str = Field(default="", max_length=MAX_TEXT)


class _DecisionIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    decision: Literal["preserve", "fix"]
    note: str = Field(default="", max_length=MAX_TEXT)


class _ApproveIn(BaseModel):
    approved_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=MAX_TEXT)


class _P2CommentIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    author: str = Field(default="user", max_length=64)
    file: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1, le=10_000_000)


class _P2ApproveIn(BaseModel):
    slice_id: str = Field(min_length=1, max_length=32)
    approved_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=MAX_TEXT)


class _P2RejectIn(BaseModel):
    slice_id: str = Field(min_length=1, max_length=32)
    by: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=MAX_TEXT)


class _P2RiskIn(BaseModel):
    risk_id: str = Field(min_length=1, max_length=32)
    status: str = Field(min_length=1, max_length=32)
    note: str = Field(min_length=1, max_length=MAX_TEXT)
    by: str = Field(default="user", max_length=64)


class _P2MaskIn(BaseModel):
    eq_id: str = Field(min_length=1, max_length=32)
    by: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=MAX_TEXT)


class _P2MarkIn(BaseModel):
    slice_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=0, le=10000)
    index: int = Field(ge=0, le=100000)
    status: str = Field(min_length=1, max_length=16)
    by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=MAX_TEXT)


class _P2CutoverIn(BaseModel):
    by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=MAX_TEXT)


P2_POST_ROUTES = ("/api/p2/comments", "/api/p2/approve-slice", "/api/p2/reject-slice", "/api/p2/resolve-risk",
                  "/api/p2/approve-mask", "/api/p2/review-mark", "/api/p2/cutover-approve")
_P2_MARKERS = ("target/fit_report.json", "plan/migration_plan.json", "slices")


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        self.code, self.msg = code, msg


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ReviewState:
    """File-backed state for one project (decisions and approval go through the review/ package)."""

    def __init__(self, project: Project):
        self.project = project
        self.lock = threading.Lock()
        self.fb = project.root / "feedback"

    # -- readers -------------------------------------------------------
    def _load(self, name: str, model):
        p = self.fb / name
        if not p.exists():
            return model()
        return model.model_validate_json(p.read_text(encoding="utf-8"))

    def findings(self) -> FindingsFile:
        return self._load("findings.json", FindingsFile)

    def resolutions(self) -> ResolutionsFile:
        return self._load("resolutions.json", ResolutionsFile)

    def comments(self) -> CommentsFile:
        return self._load("comments.json", CommentsFile)

    def versions(self) -> list[int]:
        d = self.project.graph_dir / "snapshots"
        if not d.is_dir():
            return []
        return sorted(int(m.group(1)) for p in d.iterdir() if (m := _SNAP_RE.fullmatch(p.name)))

    def graph(self) -> Graph | None:
        try:
            return load_latest(self.project.graph_dir)
        except GraphStoreError:
            return None

    def doc_text(self) -> str:
        p = self.project.docs / "understanding.latest.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def known_item_ids(self) -> set[str]:
        ids = anchors(self.doc_text())
        g = self.graph()
        if g:
            ids |= {n.id for n in g.nodes} | {e.id for e in g.edges}
        return ids

    def coverage(self, doc: str, g: Graph | None) -> dict:
        inv: dict[str, int] = {}
        p = self.project.inventory_path
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)):
                        inv[str(k)] = int(v)
                    elif isinstance(v, (list, dict)):
                        inv[str(k)] = len(v)
        by_type: dict[str, int] = {}
        if g:
            for n in g.nodes:
                by_type[n.type] = by_type.get(n.type, 0) + 1
        doc_rows: list[list[str]] = []
        in_cov = False
        for line in doc.splitlines():
            if line.startswith("## "):
                in_cov = "coverage" in line.lower()
                continue
            m = re.match(r"^\s*-\s+([^:]+):\s*(.*)$", line)
            if in_cov and m:
                doc_rows.append([m.group(1).strip(), m.group(2).strip()])
        return {"inventory": inv, "nodes_by_type": by_type, "doc": doc_rows,
                "node_total": len(g.nodes) if g else 0, "edge_total": len(g.edges) if g else 0}

    def state(self) -> dict:
        errors: list[str] = []

        def safe(fn, default):
            try:
                return fn()
            except (ValidationError, ValueError, OSError) as e:
                errors.append(f"{type(e).__name__}: {str(e)[:200]}")
                return default

        findings = safe(self.findings, FindingsFile())
        resolutions = safe(self.resolutions, ResolutionsFile())
        comments = safe(self.comments, CommentsFile())
        doc = safe(self.doc_text, "")
        g = self.graph()
        return {
            "project": safe(lambda: self.project.meta().name, ""),
            "readiness": safe(lambda: _approval.readiness(self.project).model_dump(),
                              {"ready": False, "blockers": ["readiness check failed"], "warnings": []}),
            "approval": safe(lambda: _approval.approval_status(self.project),
                             {"state": "none", "approval": None, "reason": "approval status unreadable"}),
            "decisions": safe(lambda: [d.model_dump() for d in effective_list(self.project, g)] if g else [], []),
            "findings": findings.model_dump(),
            "resolutions": resolutions.model_dump(),
            "comments": comments.model_dump(),
            "versions": self.versions(),
            "sections": render_sections(doc),
            "coverage": self.coverage(doc, g),
            "known_ids": sorted(self.known_item_ids()),
            "p2": safe(self.p2_state, None),
            "errors": errors,
        }

    # -- writers -------------------------------------------------------
    def add_comment(self, data: dict) -> dict:
        inp = _CommentIn.model_validate(data)
        text = redact_string(inp.text.strip())  # pasted credentials never reach disk, prompts or the UI
        if not text:
            raise ApiError(400, "text must not be empty")
        with self.lock:
            if inp.item_id not in self.known_item_ids():
                raise ApiError(404, f"unknown item_id: {inp.item_id[:64]}")
            cf = self.comments()
            nums = [int(c.id.split("-")[-1]) for c in cf.comments if c.id.startswith("CM-") and c.id[3:].isdigit()]
            c = Comment(id=f"CM-{(max(nums) if nums else 0) + 1:04d}", item_id=inp.item_id, text=text,
                        author=redact_string(inp.author.strip()) or "user", created=_now())
            cf.comments.append(c)
            _atomic_write(self.fb / "comments.json", cf.model_dump_json(indent=2) + "\n")
        return c.model_dump()

    def add_resolution(self, data: dict) -> dict:
        inp = _ResolutionIn.model_validate(data)
        with self.lock:
            if inp.finding_id not in {f.id for f in self.findings().findings}:
                raise ApiError(404, f"unknown finding_id: {inp.finding_id[:64]}")
            rf = self.resolutions()
            r = Resolution(finding_id=inp.finding_id, status=inp.status, note=redact_string(inp.note.strip()), created=_now())
            rf.resolutions.append(r)
            _atomic_write(self.fb / "resolutions.json", rf.model_dump_json(indent=2) + "\n")
        return r.model_dump()

    def add_decision(self, data: dict) -> dict:
        inp = _DecisionIn.model_validate(data)
        with self.lock:
            try:
                d = add_decision(self.project, inp.item_id, inp.decision, inp.note)
            except UnknownItem:
                raise ApiError(404, f"unknown item_id: {inp.item_id[:64]}")
            except ReviewError as e:
                raise ApiError(400, str(e)[:300])
            write_latest(self.project)  # the doc shows the decision; keeps the stale-doc check green
        return d.model_dump()

    def approve(self, data: dict) -> dict:
        inp = _ApproveIn.model_validate(data)
        if not inp.approved_by.strip():
            raise ApiError(400, "approved_by must not be empty")
        with self.lock:
            try:
                a = _approval.approve(self.project, inp.approved_by, inp.note)
            except ReviewError as e:
                raise ApiError(400, str(e)[:2000])
        return a.model_dump()

    # -- Phase 2 (plan section 8): shown only when Phase 2 files exist ------------------------------
    def p2_present(self) -> bool:
        return any((self.project.root / m).exists() for m in _P2_MARKERS)

    def p2_state(self) -> dict | None:
        """Target fit, plan, comments and known Phase 2 IDs; None for a Phase 1-only project. Never raises."""
        if not self.p2_present():
            return None
        try:
            return self._p2_state()
        except Exception as e:  # noqa: BLE001 - Phase 2 problems must not break the Phase 1 UI
            return {"present": True, "fit": None, "plan": None, "comments": [], "known_ids": [],
                    "errors": [f"phase 2 state: {type(e).__name__}"]}

    def _p2_state(self) -> dict:
        from migration_agent.migrate import approvals as _ap
        from migration_agent.migrate import equivalence as _eq
        from migration_agent.migrate.casestore import load_cases, load_plan
        from migration_agent.migrate.fit import fit_state
        from migration_agent.migrate.state import load_state
        errors: list[str] = []
        out: dict = {"present": True, "fit": None, "plan": None, "comments": [], "known_ids": [], "errors": errors}
        lay_fit = self.project.root / "target" / "fit_report.json"
        if lay_fit.exists():
            out["fit"] = fit_state(self.project)
        try:
            plan = load_plan(self.project)
        except (ValidationError, ValueError, OSError) as e:
            plan = None
            errors.append(f"plan: {type(e).__name__}")
        known: set[str] = set()
        if plan is not None:
            rows = []
            for sl in sorted(plan.slices, key=lambda x: (x.order, x.id)):
                row = sl.model_dump(mode="json")
                try:
                    st = load_state(self.project, sl.id)
                    row["status"], row["iteration"], row["reason"] = st.status, st.iteration, st.reason
                    rep = _eq.load_report(self.project, sl.id)
                    row["equivalence_pct"] = rep.equivalence_pct if rep else None
                    row["unexplained"] = len(rep.unexplained) if rep else None
                    row["approved"] = _ap.load_approval(self.project, sl.id) is not None
                except (ValidationError, ValueError, OSError) as e:
                    errors.append(f"{sl.id}: {type(e).__name__}")
                rows.append(row)
                known.add(sl.id)
            out["plan"] = {"version": plan.version, "created": plan.created, "slices": rows,
                           "excluded": len(plan.excluded_nodes),
                           "estimate_usd": round(sum(x.estimate_usd for x in plan.slices), 4)}
        try:
            out["comments"] = [c.model_dump() for c in _ap.load_comments(self.project).comments]
        except (ValidationError, ValueError, OSError) as e:
            errors.append(f"comments: {type(e).__name__}")
        try:
            known |= {c.id for c in load_cases(self.project)}
        except (ValidationError, ValueError, OSError):
            pass
        if out["fit"]:
            known |= {r["id"] for r in out["fit"].get("risks", [])}
        out["known_ids"] = sorted(known)
        return out

    def p2_slice(self, slice_id: str) -> dict:
        try:
            return self._p2_slice(slice_id)
        except (ApiError, ValidationError, ValueError, OSError):
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(500, f"server error: {type(e).__name__}")

    def _p2_slice(self, slice_id: str) -> dict:
        from migration_agent.migrate import approvals as _ap
        from migration_agent.migrate import equivalence as _eq
        from migration_agent.migrate.casestore import load_cases, load_plan
        from migration_agent.migrate.models import is_id
        from migration_agent.migrate.state import load_state
        if not is_id(slice_id, "SLICE"):
            raise ApiError(400, "bad slice id")
        plan = load_plan(self.project)
        sl = next((x for x in plan.slices if x.id == slice_id), None) if plan else None
        if sl is None:
            raise ApiError(404, "unknown slice")
        it = _ap.latest_iteration(self.project, slice_id)

        def raw(name):
            if it is None or not (it / name).is_file():
                return None
            try:
                return json.loads((it / name).read_text(encoding="utf-8"))
            except ValueError:
                return {"error": f"{name} is not valid JSON"}

        rep = _eq.load_report(self.project, slice_id)
        meta = _eq.load_run_meta(self.project, slice_id)
        appr = _ap.load_approval(self.project, slice_id)
        cases = [c for c in load_cases(self.project) if c.slice == slice_id]
        eq_ids = {d.id for d in rep.diffs} if rep else set()
        mine = {slice_id} | {c.id for c in cases} | eq_ids | set(sl.node_ids)
        try:
            humans = _ap.human_commits(self.project)[:50]
        except _ap.GitError:
            humans = []
        return {
            "slice": sl.model_dump(mode="json"),
            "state": load_state(self.project, slice_id).model_dump(mode="json"),
            "iterations": _ap.iterations(self.project, slice_id),
            "build": raw("build.json"), "tests": raw("tests.json"),
            "review": _ap.review_findings(self.project, slice_id),
            "review_iteration": int(it.name) if it is not None and it.name.isdigit() else None,
            "review_marks": _ap.load_review_marks(self.project, slice_id),
            "diff": _ap.slice_diff(self.project, slice_id),
            "equivalence": (rep.model_dump(mode="json") | {"equivalence_pct": rep.equivalence_pct}) if rep else None,
            "run": meta.model_dump(mode="json") if meta else None,
            "cases": [{"id": c.id, "covers": c.covers, "decision": c.decision, "tags": c.tags,
                       "source": c.expect.source, "approved_by": c.expect.approved_by} for c in cases],
            "blockers": _ap.approval_blockers(self.project, slice_id),
            "decisions": _ap.load_decisions(self.project, slice_id).model_dump(mode="json"),
            "fix_requests": _eq.load_fix_requests(self.project, slice_id).model_dump(mode="json"),
            "masks": [m.model_dump() for m in _eq.load_masks(self.project).masks if m.eq_id in eq_ids],
            "approval": appr.model_dump(mode="json") if appr else None,
            "human_commits": humans,
            "comments": [c.model_dump() for c in _ap.load_comments(self.project).comments if c.item_id in mine],
        }

    def p2_add_comment(self, data: dict) -> dict:
        from migration_agent.migrate import approvals as _ap
        inp = _P2CommentIn.model_validate(data)
        with self.lock:
            try:
                c = _ap.add_comment(self.project, inp.item_id.strip(), inp.text, author=inp.author or "user",
                                    file=inp.file, line=inp.line)
            except _ap.ApprovalError as e:
                raise ApiError(404 if str(e).startswith(("unknown item_id", "no such file")) else 400, str(e)[:300])
        return c.model_dump()

    def p2_approve_slice(self, data: dict) -> dict:
        from migration_agent.migrate import approvals as _ap
        inp = _P2ApproveIn.model_validate(data)
        with self.lock:
            try:
                a = _ap.approve_slice(self.project, inp.slice_id, inp.approved_by, inp.note)
            except _ap.ApprovalError as e:
                raise ApiError(409 if e.blockers else 400, str(e)[:2000])
        return a.model_dump(mode="json")

    def p2_reject_slice(self, data: dict) -> dict:
        from migration_agent.migrate import approvals as _ap
        inp = _P2RejectIn.model_validate(data)
        with self.lock:
            try:
                return _ap.reject_slice(self.project, inp.slice_id, inp.by, inp.reason)
            except _ap.ApprovalError as e:
                raise ApiError(409 if e.blockers else 400, str(e)[:2000])

    def p2_resolve_risk(self, data: dict) -> dict:
        from migration_agent.migrate.fit import FitError, resolve_risk
        inp = _P2RiskIn.model_validate(data)
        with self.lock:
            try:
                r = resolve_risk(self.project, inp.risk_id, inp.status, redact_string(inp.note),
                                 by=redact_string(inp.by.strip()) or "user")
            except FitError as e:
                raise ApiError(404 if "not in" in str(e) else 400, str(e)[:300])
        return r.model_dump(mode="json")

    def p2_review_mark(self, data: dict) -> dict:
        from migration_agent.migrate import approvals as _ap
        inp = _P2MarkIn.model_validate(data)
        with self.lock:
            try:
                return _ap.mark_review_finding(self.project, inp.slice_id, inp.iteration, inp.index, inp.status,
                                               redact_string(inp.by.strip()), redact_string(inp.note))
            except _ap.ApprovalError as e:
                raise ApiError(400, str(e)[:300])

    def p2_cutover(self) -> dict:
        """Cut-over readiness (every check), the approval and whether its hashes still match."""
        from migration_agent.migrate import cutover as _co
        with self.lock:
            try:
                rd = _co.readiness(self.project)
                appr = _co.load_approval(self.project)
                ver = _co.verify_approval(self.project)
            except (_co.CutoverError, ValidationError, ValueError, OSError) as e:
                raise ApiError(400, f"cut-over state: {type(e).__name__}: {str(e)[:200]}")
        return {"ready": rd.ready, "checks": [c.model_dump(mode="json") for c in rd.checks],
                "blockers": rd.blockers, "warnings": rd.warnings, "target_head": rd.target_head,
                "full_suite": rd.full_suite.model_dump(mode="json") if rd.full_suite else None,
                "approval": appr.model_dump(mode="json") if appr else None, "approval_check": ver,
                "plan_exists": (self.project.root / "cutover" / "plan.md").is_file()}

    def p2_cutover_approve(self, data: dict) -> dict:
        from migration_agent.migrate import cutover as _co
        inp = _P2CutoverIn.model_validate(data)
        with self.lock:
            try:
                a = _co.approve(self.project, redact_string(inp.by.strip()), redact_string(inp.note))
            except _co.CutoverError as e:
                raise ApiError(409, str(e)[:2000])
        return a.model_dump(mode="json")

    def p2_approve_mask(self, data: dict) -> dict:
        from migration_agent.migrate.equivalence import EquivalenceError, approve_mask
        inp = _P2MaskIn.model_validate(data)
        with self.lock:
            try:
                m = approve_mask(self.project, inp.eq_id, inp.by, inp.reason)
            except EquivalenceError as e:
                raise ApiError(404 if "not a diff" in str(e) else 400, str(e)[:300])
        return m.model_dump()

    def diff(self, a: int, b: int) -> dict:
        vs = set(self.versions())
        if a not in vs or b not in vs:
            raise ApiError(404, "unknown version")
        ga, gb = load_snapshot(self.project.graph_dir, a), load_snapshot(self.project.graph_dir, b)
        d = diff_graphs(ga, gb)
        names = {n.id: n.name for n in ga.nodes} | {n.id: n.name for n in gb.nodes}
        row = lambda ids: [{"id": i, "name": names.get(i, "")} for i in ids]  # noqa: E731
        return {"a": a, "b": b, "added_nodes": row(d.added_nodes), "removed_nodes": row(d.removed_nodes),
                "changed_nodes": row(d.changed_nodes), "added_edges": len(d.added_edges),
                "removed_edges": len(d.removed_edges), "changed_edges": len(d.changed_edges)}


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, state: ReviewState, token: str):
        super().__init__(addr, handler)
        self.state = state
        self.token = token
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    server: ReviewServer
    server_version = "ReviewUI"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- plumbing ------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; frame-src 'self'; frame-ancestors 'self'; "
                         "base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._json(code, {"error": msg})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in (
                f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"):
            return False
        sfs = self.headers.get("Sec-Fetch-Site")
        return sfs in (None, "same-origin", "none")

    def _eq(self, cand: str | None) -> bool:
        return bool(cand) and hmac.compare_digest(cand.encode(), self.server.token.encode())

    def _authorize(self, segs: list[str]) -> tuple[bool, list[str]]:
        """Token via first path segment, X-Token header, or cookie. Returns (ok, remaining segs)."""
        if segs and self._eq(segs[0]):
            return True, segs[1:]
        if self._eq(self.headers.get("X-Token")):
            return True, segs
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "review_token" and self._eq(v):
                return True, segs
        return False, segs

    def _dispatch(self, method: str):
        try:
            if not self._host_ok():
                return self._err(403, "bad host")
            parts = urlsplit(self.path)
            if "\\" in parts.path or "\x00" in parts.path or ".." in parts.path.split("/"):
                return self._err(400, "bad path")
            segs = [s for s in parts.path.split("/") if s]
            ok, rest = self._authorize(segs)
            if not ok:
                return self._err(403, "forbidden")
            if method == "POST" and not self._origin_ok():
                return self._err(403, "cross-origin request rejected")
            query = parse_qs(parts.query)
            route = "/" + "/".join(rest)
            st = self.server.state
            if method == "GET":
                return self._get(route, query, st)
            if method == "POST":
                return self._post(route, st)
            return self._err(405, "method not allowed")
        except ApiError as e:
            self._err(e.code, e.msg)
        except ValidationError as e:
            self._err(400, "invalid input: " + "; ".join(
                f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:300])
        except (OSError, ValueError, GraphStoreError) as e:
            self._err(500, f"server error: {type(e).__name__}")

    def _get(self, route: str, query: dict, st: ReviewState):
        if route == "/":
            cookie = f"review_token={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            base = "/" + self.server.token
            return self._send(200, render_page(base).encode(), "text/html; charset=utf-8", {"Set-Cookie": cookie})
        if route == "/api/state":
            return self._json(200, st.state())
        if route == "/api/diff":
            return self._json(200, st.diff(self._int(query, "a"), self._int(query, "b")))
        if route == "/api/p2/cutover":
            return self._json(200, st.p2_cutover())
        if route == "/api/p2/slice":
            sid = (query.get("id") or [""])[0]
            return self._json(200, st.p2_slice(sid))
        if route == "/graph":
            v = self._int(query, "v", None)
            prev = self._int(query, "prev", None)
            if v is None:
                g = st.graph()
                if g is None:
                    raise ApiError(404, "no graph")
            else:
                if v not in st.versions():
                    raise ApiError(404, "unknown version")
                g = load_snapshot(st.project.graph_dir, v)
            p = None
            if prev is not None:
                if prev not in st.versions():
                    raise ApiError(404, "unknown version")
                p = load_snapshot(st.project.graph_dir, prev)
            html_ = render_graph_html(g, previous=p, title=f"Graph v{g.version}")
            return self._send(200, html_.encode(), "text/html; charset=utf-8")
        return self._err(404, "not found")

    @staticmethod
    def _int(query: dict, key: str, default=...):
        vals = query.get(key)
        if not vals:
            if default is ...:
                raise ApiError(400, f"missing {key}")
            return default
        if not re.fullmatch(r"\d{1,9}", vals[0]):
            raise ApiError(400, f"bad {key}")
        return int(vals[0])

    def _post(self, route: str, st: ReviewState):
        if route not in ("/api/comments", "/api/resolutions", "/api/decisions", "/api/approve") + P2_POST_ROUTES:
            return self._err(404, "not found")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._err(415, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._err(411, "Content-Length required")
        if length < 0:
            return self._err(400, "bad length")
        if length > MAX_BODY:
            self.close_connection = True
            return self._err(413, "body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"")
        except ValueError:
            return self._err(400, "invalid JSON")
        if not isinstance(data, dict):
            return self._err(400, "JSON object required")
        if route == "/api/comments":
            return self._json(201, st.add_comment(data))
        if route == "/api/decisions":
            return self._json(201, st.add_decision(data))
        if route == "/api/approve":
            return self._json(201, st.approve(data))
        if route in P2_POST_ROUTES:
            handler = {"/api/p2/comments": st.p2_add_comment, "/api/p2/approve-slice": st.p2_approve_slice,
                       "/api/p2/reject-slice": st.p2_reject_slice, "/api/p2/resolve-risk": st.p2_resolve_risk,
                       "/api/p2/approve-mask": st.p2_approve_mask, "/api/p2/review-mark": st.p2_review_mark,
                       "/api/p2/cutover-approve": st.p2_cutover_approve}[route]
            return self._json(201, handler(data))
        return self._json(201, st.add_resolution(data))

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")


def serve(project_dir, host: str = "127.0.0.1", port: int = 0, open_browser: bool = False):
    """Start the review server in a background thread. Returns (server, url).

    Only loopback is allowed. Stop with ``server.stop()``.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("review server only binds to 127.0.0.1")
    project = Project.load(Path(project_dir))
    token = secrets.token_urlsafe(24)
    assert _TOKEN_RE.match(token)
    server = ReviewServer(("127.0.0.1", port), Handler, ReviewState(project), token)
    t = threading.Thread(target=server.serve_forever, name="review-ui", daemon=True)
    t.start()
    server.thread = t
    url = f"http://127.0.0.1:{server.port}/{token}/"
    if open_browser:
        webbrowser.open(url)
    return server, url
