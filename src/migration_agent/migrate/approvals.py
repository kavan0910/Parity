"""Human review loop for Phase 2 (docs/phase2-plan.md section 8, 4.4): comments, slice decisions, git
inspection of target_code/, and per-slice approval.

Files (all written atomically under a lock):

  migration/comments.json         P2CommentsFile. Schema (key format shared with the slice runner's reader):
                                  {"schema_version": 1, "comments": [{"id": "CM-0001", "item_id": "SLICE-0001",
                                   "file": "src/app.py", "line": 12, "text": "...", "author": "bob",
                                   "created": "<iso>", "processed": false}]}
                                  `item_id`: SLICE-/CT-/EQ-/RISK- or a graph ID (COMP-0001, EP-0003 ...).
                                  `file` is target_code-relative ("" when the comment is not on a file),
                                  `line` >= 1 or null. Text and author are redacted on write.
  slices/SLICE-NNNN/decisions.json SliceDecisionsFile: per-slice human decisions.
                                  kind accept_observed  -> deferred finding F-NNNN migrated "as observed"
                                  kind legacy_bug       -> equivalence triage says legacy is wrong (ref EQ-NNNN);
                                                           open until the user picks preserve | fix
                                  kind spec_ambiguity   -> open question (ref EQ-NNNN); the slice is blocked
                                                           until answered
  slices/SLICE-NNNN/approval.json SliceApproval (migrate/models.py)

Approval gate (`approval_blockers`): contract gate passes; state awaiting_human; latest iteration build green;
latest iteration suite green except cases whose only differences are approved intended divergences; latest
equivalence report has zero regression/unknown diffs; target_code HEAD unchanged (no human or agent commits)
since that equivalence run; plan blockers (F-/RISK-) and contract deferred findings on the slice resolved or
accepted; no open legacy_bug / spec_ambiguity decisions; no pending fix requests; no unapproved specified
expectations in the slice's cases.

Iteration files are written by the slice runner (slices/SLICE-NNNN/iterations/K/{build,tests}.json). The readers
here are tolerant: build is green when `ok` is true, or `exit_code == 0`, or `status` in (green, ok, passed,
success). The suite is green when `failed` (list of CT ids) is empty after removing approved divergences, else
`ok` / `status` as for build; `results: [{case_id, outcome}]` is also understood.

Git: every call uses a fixed argv, `core.hooksPath=/dev/null`, no external diff/textconv, user/system config
ignored, GIT_DIR pinned to target_code/.git (never a parent repository).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from .models import SliceApproval, is_id
from .paths import Phase2Layout
from .state import atomic_write_json, file_lock, now_iso, read_json, sha256_file

AGENT_NAME = "migration-agent"
AGENT_EMAIL = "agent@localhost"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_TEXT = 5000
MAX_DIFF_BYTES = 400_000
GRAPH_ID_RE = re.compile(r"^[A-Z]{2,10}-\d{4,}$")
P2_ITEM_RE = re.compile(r"^(SLICE|CT|EQ|RISK)-\d{4,}$")
GREEN = ("green", "ok", "passed", "pass", "success", "succeeded")


class ApprovalError(RuntimeError):
    """Refused approval or invalid input. `blockers` lists every reason for a refused approval."""

    def __init__(self, msg: str, blockers: list[str] | None = None):
        super().__init__(msg)
        self.blockers = blockers or []


class GitError(RuntimeError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ models (local to this module)
class P2Comment(_Strict):
    id: str = Field(pattern=r"^CM-\d{4,}$")
    item_id: str = Field(min_length=1, max_length=64)
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    author: str = "user"
    created: str = ""
    processed: bool = False


class P2CommentsFile(_Strict):
    schema_version: Literal[1] = 1
    comments: list[P2Comment] = Field(default_factory=list)


DecisionKind = Literal["accept_observed", "legacy_bug", "spec_ambiguity"]


class SliceDecision(_Strict):
    kind: DecisionKind
    ref: str = Field(min_length=1)                 # F-NNNN (accept_observed) or EQ-NNNN
    status: Literal["open", "resolved"] = "open"
    choice: Literal["", "accept_observed", "preserve", "fix", "answered"] = ""
    question: str = ""                             # what the user must decide (redacted, capped)
    case_id: str = ""
    by: str = ""
    note: str = ""
    created: str = ""
    resolved_at: str = ""


class SliceDecisionsFile(_Strict):
    schema_version: Literal[1] = 1
    slice_id: str = ""
    decisions: list[SliceDecision] = Field(default_factory=list)

    def open(self) -> list[SliceDecision]:
        return [d for d in self.decisions if d.status == "open"]

    def accepted(self) -> set[str]:
        return {d.ref for d in self.decisions if d.kind == "accept_observed" and d.status == "resolved"}


def _audit(project, action: str, **fields) -> None:
    from ..audit import AuditLog
    try:
        AuditLog(Path(project.root) / "audit" / "audit.jsonl").record("approvals", action, **fields)
    except OSError:
        pass


def _redact(text: str, limit: int = MAX_TEXT) -> str:
    from ..ingest.secrets import redact_string
    return redact_string(str(text or "").strip())[:limit]


def _check_slice(slice_id: str) -> str:
    if not is_id(slice_id or "", "SLICE"):
        raise ApprovalError(f"not a valid SLICE id: {str(slice_id)[:40]!r}")
    return slice_id


# ------------------------------------------------------------------ comments
def load_comments(project) -> P2CommentsFile:
    return read_json(Phase2Layout(project).comments, P2CommentsFile, P2CommentsFile())


def _graph_ids(project) -> set[str] | None:
    from ..graph.store import GraphStoreError, load_latest
    try:
        g = load_latest(Path(project.root) / "graph")
    except (GraphStoreError, OSError, ValueError):
        return None
    if g is None:
        return None
    return {n.id for n in g.nodes} | {e.id for e in g.edges}


def known_item(project, item_id: str) -> bool:
    """SLICE-/CT-/EQ-/RISK- ids that were allocated (migration/ids.json), or graph IDs of the latest graph."""
    if P2_ITEM_RE.match(item_id):
        from .ids import IdAllocator
        prefix, num = item_id.split("-", 1)
        lay = Phase2Layout(project)
        if not lay.ids_file.exists():
            return False
        return 1 <= int(num) <= IdAllocator(lay.ids_file).last(prefix)
    if not GRAPH_ID_RE.match(item_id):
        return False
    ids = _graph_ids(project)
    return ids is None or item_id in ids


def _clean_file(project, file: str | None) -> str:
    if not file:
        return ""
    f = str(file).strip().replace("\\", "/")
    if (f.startswith("/") or ".." in f.split("/") or re.search(r"[\x00-\x1f\x7f]", f) or len(f) > 500
            or (len(f) > 1 and f[1] == ":")):
        raise ApprovalError("file must be a target_code-relative path without '..'")
    root = Phase2Layout(project).target_code.resolve()
    p = (root / f).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise ApprovalError(f"no such file in target_code/: {f[:200]}")
    if ".git" in Path(f).parts:
        raise ApprovalError("comments on git internals are not allowed")
    return f


def add_comment(project, item_id: str, text: str, *, author: str = "user", file: str | None = None,
                line: int | None = None) -> P2Comment:
    """Append a redacted comment to migration/comments.json (ApprovalError on bad input)."""
    item_id = (item_id or "").strip()
    if not known_item(project, item_id):
        raise ApprovalError(f"unknown item_id: {item_id[:64]}")
    body = _redact(text)
    if not body:
        raise ApprovalError("text must not be empty")
    f = _clean_file(project, file)
    if line is not None:
        if not f:
            raise ApprovalError("line requires file")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ApprovalError("line must be a positive integer")
    lay = Phase2Layout(project)
    lay.ensure(lay.migration)
    with file_lock(lay.comments):
        cf = load_comments(project)
        nums = [int(c.id[3:]) for c in cf.comments]
        c = P2Comment(id=f"CM-{(max(nums) if nums else 0) + 1:04d}", item_id=item_id, file=f, line=line,
                      text=body, author=_redact(author, 64) or "user", created=now_iso())
        cf.comments.append(c)
        atomic_write_json(lay.comments, cf)
    _audit(project, "p2_comment", comment=c.id, item_id=item_id, file=f)
    return c


# ------------------------------------------------------------------ slice decisions
def load_decisions(project, slice_id: str) -> SliceDecisionsFile:
    df = read_json(Phase2Layout(project).slice_decisions(_check_slice(slice_id)), SliceDecisionsFile, None)
    return df if df is not None else SliceDecisionsFile(slice_id=slice_id)


def _save_decisions(project, slice_id: str, df: SliceDecisionsFile) -> None:
    lay = Phase2Layout(project)
    lay.ensure(lay.slice_dir(slice_id))
    atomic_write_json(lay.slice_decisions(slice_id), df)


def upsert_open_decision(project, slice_id: str, kind: DecisionKind, ref: str, question: str,
                         case_id: str = "") -> SliceDecision:
    """Add an open decision request (idempotent per kind+ref while open). Used by equivalence triage."""
    path = Phase2Layout(project).slice_decisions(_check_slice(slice_id))
    with file_lock(path):
        df = load_decisions(project, slice_id)
        for d in df.decisions:
            if d.kind == kind and d.ref == ref and d.status == "open":
                return d
        d = SliceDecision(kind=kind, ref=ref, question=_redact(question, 2000), case_id=case_id, created=now_iso())
        df.decisions.append(d)
        _save_decisions(project, slice_id, df)
    _audit(project, "slice_decision_request", slice=slice_id, kind=kind, ref=ref)
    return d


def slice_blockers(project, slice_id: str) -> list[str]:
    """F-/RISK- blockers attached to the slice: plan Slice.blockers plus contract deferred findings whose
    item_ids intersect the slice's nodes."""
    from .casestore import load_plan
    out: list[str] = []
    plan = load_plan(project)
    sl = next((s for s in plan.slices if s.id == slice_id), None) if plan else None
    if sl is not None:
        out += list(sl.blockers)
        try:
            from .contract import check_contract
            nodes = set(sl.node_ids)
            out += [b.finding_id for b in check_contract(project).blockers if nodes & set(b.item_ids)]
        except Exception:  # noqa: BLE001 - the contract gate itself is reported separately
            pass
    return sorted(set(out))


def accept_observed(project, slice_id: str, finding_id: str, by: str, note: str) -> SliceDecision:
    """Accept "migrate legacy behaviour as observed" for a deferred finding blocking the slice."""
    _check_slice(slice_id)
    by, note = _redact(by, 64), _redact(note, 2000)
    if not by:
        raise ApprovalError("--by is required")
    if not note:
        raise ApprovalError("a note is required (why the observed behaviour is acceptable)")
    if not re.fullmatch(r"F-\d{4,}", finding_id or ""):
        raise ApprovalError(f"not a finding id: {str(finding_id)[:40]!r}")
    if finding_id not in slice_blockers(project, slice_id):
        raise ApprovalError(f"{finding_id} is not a blocker of {slice_id}")
    path = Phase2Layout(project).slice_decisions(slice_id)
    with file_lock(path):
        df = load_decisions(project, slice_id)
        d = SliceDecision(kind="accept_observed", ref=finding_id, status="resolved", choice="accept_observed",
                          by=by, note=note, created=now_iso(), resolved_at=now_iso())
        df.decisions = [x for x in df.decisions if not (x.kind == "accept_observed" and x.ref == finding_id)]
        df.decisions.append(d)
        _save_decisions(project, slice_id, df)
    _audit(project, "accept_observed", slice=slice_id, finding=finding_id, by=by)
    return d


def resolve_decision(project, slice_id: str, ref: str, choice: str, by: str, note: str = "") -> SliceDecision:
    """Answer an open legacy_bug (preserve | fix) or spec_ambiguity (answered) decision.

    preserve -> a pending fix request for the new service (legacy output stays the truth);
    fix      -> the case needs an approved specified expectation (`tests approve-spec`) before re-running;
    answered -> the open question is closed; when no spec_ambiguity remains open a blocked slice is unblocked."""
    _check_slice(slice_id)
    by = _redact(by, 64)
    if not by:
        raise ApprovalError("--by is required")
    path = Phase2Layout(project).slice_decisions(slice_id)
    with file_lock(path):
        df = load_decisions(project, slice_id)
        d = next((x for x in df.decisions if x.ref == ref and x.status == "open"), None)
        if d is None:
            raise ApprovalError(f"no open decision for {str(ref)[:40]} on {slice_id}")
        allowed = {"legacy_bug": ("preserve", "fix"), "spec_ambiguity": ("answered",)}.get(d.kind, ())
        if choice not in allowed:
            raise ApprovalError(f"{d.kind} decisions accept: {', '.join(allowed)}")
        d.status, d.choice, d.by, d.note, d.resolved_at = "resolved", choice, by, _redact(note, 2000), now_iso()
        _save_decisions(project, slice_id, df)
        still_ambiguous = any(x.kind == "spec_ambiguity" and x.status == "open" for x in df.decisions)
    if d.kind == "legacy_bug" and choice == "preserve":
        from .equivalence import add_fix_request
        add_fix_request(project, slice_id, eq_id=ref, case_id=d.case_id, reason="legacy_bug_preserve",
                        note=f"user chose preserve: match the legacy output ({d.note[:200]})")
    if d.kind == "spec_ambiguity" and not still_ambiguous:
        from .state import IllegalTransition, load_state, transition
        st = load_state(project, slice_id)
        if st.status == "blocked" and st.blocked_from:
            try:
                transition(project, slice_id, st.blocked_from, actor=f"human:{by}", reason="open questions answered")
            except IllegalTransition:
                pass
    _audit(project, "slice_decision", slice=slice_id, ref=ref, choice=choice, by=by)
    return d



# ------------------------------------------------------------------ reviewer finding marks (precision)
REVIEW_MARK_STATUSES = ("accepted", "dismissed")


def review_marks_path(project, slice_id: str) -> Path:
    return Phase2Layout(project).slice_dir(_check_slice(slice_id)) / "review_marks.json"


def load_review_marks(project, slice_id: str) -> dict[str, dict]:
    """{"K:I": {"status", "by", "note", "at"}}: a human's verdict on reviewer finding I of iteration K.

    Kept apart from review.json (which is the reviewer's immutable output). Metrics read it: reviewer precision is
    accepted / (accepted + dismissed) over the findings a human has marked.
    """
    data = read_json(review_marks_path(project, slice_id), None, None)
    marks = (data or {}).get("marks") if isinstance(data, dict) else None
    return marks if isinstance(marks, dict) else {}


def mark_review_finding(project, slice_id: str, iteration: int, index: int, status: str, by: str,
                        note: str = "") -> dict:
    """Record that a reviewer finding was `accepted` (a real problem) or `dismissed` (noise)."""
    _check_slice(slice_id)
    if status not in REVIEW_MARK_STATUSES:
        raise ApprovalError(f"status must be one of {', '.join(REVIEW_MARK_STATUSES)}")
    by = _redact(by, 64)
    if not by:
        raise ApprovalError("--by is required")
    path = Phase2Layout(project).slice_dir(slice_id) / "iterations" / str(int(iteration)) / "review.json"
    data = _read_any(path)
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list) or not (0 <= int(index) < len(findings)):
        raise ApprovalError(f"{slice_id} iteration {iteration} has no reviewer finding #{index}")
    key = f"{int(iteration)}:{int(index)}"
    mark = {"status": status, "by": by, "note": _redact(note, 1000), "at": now_iso()}
    path = review_marks_path(project, slice_id)
    lay = Phase2Layout(project)
    lay.ensure(lay.slice_dir(slice_id))
    with file_lock(path):
        marks = dict(load_review_marks(project, slice_id))
        marks[key] = mark
        atomic_write_json(path, {"schema_version": 1, "slice_id": slice_id, "marks": marks})
    _audit(project, "review_finding_marked", slice=slice_id, key=key, status=status, by=by)
    return {"key": key, **mark}


# ------------------------------------------------------------------ git (target_code/)
_GIT_CFG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "diff.external=",
            "-c", "core.pager=cat", "-c", "color.ui=false", "-c", "protocol.allow=never",
            "-c", "core.sshCommand=false", "-c", "submodule.recurse=false")


def is_git_repo(project) -> bool:
    return (Phase2Layout(project).target_code / ".git").is_dir()


def git(project, *args: str, timeout: float = 30) -> str:
    """Run git on target_code/ (fixed argv, no hooks, no user/system config). Returns stdout; GitError otherwise."""
    tc = Phase2Layout(project).target_code
    if not is_git_repo(project):
        raise GitError("target_code/ is not a git repository")
    for a in args:
        if not isinstance(a, str) or "\x00" in a:
            raise GitError("invalid git argument")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_DIR=str(tc / ".git"), GIT_WORK_TREE=str(tc), GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", PAGER="cat",
               GIT_CEILING_DIRECTORIES=str(Path(project.root).resolve()), LC_ALL="C")
    try:
        r = subprocess.run(["git", "--no-pager", *_GIT_CFG, *args], cwd=tc, env=env, capture_output=True,
                           timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git failed: {type(e).__name__}") from None
    if r.returncode != 0:
        raise GitError(r.stderr.decode("utf-8", "replace").strip()[:300] or f"git exit {r.returncode}")
    return r.stdout.decode("utf-8", "replace")


def git_head(project) -> str:
    """target_code HEAD sha, or "" when there is no repository / no commit."""
    if not is_git_repo(project):
        return ""
    try:
        return git(project, "rev-parse", "--verify", "-q", "HEAD^{commit}").strip()
    except GitError:
        return ""


def _is_agent(name: str, email: str) -> bool:
    return name == AGENT_NAME and email.lower() == AGENT_EMAIL


def _sha(v: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40,64}", v or ""):
        raise GitError("not a commit sha")
    return v


def commits(project, since: str | None = None) -> list[dict]:
    """Commits reachable from HEAD (newest first), only those after `since` (a sha) if given:
    [{sha, author, email, time, subject, human}]."""
    if not git_head(project):
        return []
    rng = f"{_sha(since)}..HEAD" if since else "HEAD"
    out = git(project, "log", "--no-color", "--format=%H%x1f%an%x1f%ae%x1f%ct%x1f%s", rng, "--")
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, an, ae, ct, subj = parts
        rows.append({"sha": sha, "author": an, "email": ae, "time": int(ct) if ct.isdigit() else 0,
                     "subject": subj[:200], "human": not _is_agent(an, ae)})
    return rows


def human_commits(project, since: str | None = None) -> list[dict]:
    """Non-agent commits in target_code/ (after `since` if given): the human-edit signal (plan 8)."""
    return [c for c in commits(project, since) if c["human"]]


def human_edit_stats(project) -> dict:
    """Lines changed by human vs agent commits (git attribution; plan 10 human-edit rate)."""
    out = {"human_commits": 0, "agent_commits": 0, "human_lines": 0, "agent_lines": 0}
    if not git_head(project):
        return out
    log = git(project, "log", "--no-color", "--numstat", "--format=@@%an%x1f%ae", "HEAD", "--")
    who = None
    for line in log.splitlines():
        if line.startswith("@@"):
            an, _, ae = line[2:].partition("\x1f")
            who = "agent" if _is_agent(an, ae) else "human"
            out[f"{who}_commits"] += 1
        elif who and line.strip():
            a, _, rest = line.partition("\t")
            d, _, _ = rest.partition("\t")
            out[f"{who}_lines"] += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return out


def approved_tag(slice_id: str) -> str:
    return f"slice/{_check_slice(slice_id)}/approved"


def tag_sha(project, tag: str) -> str:
    try:
        return git(project, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}").strip()
    except GitError:
        return ""


def diff_base(project, slice_id: str) -> tuple[str, str]:
    """(base sha, label) for the slice diff: the slice's own approved tag, else the latest approved tag of an
    earlier slice in plan order, else the root commit (empty tree if HEAD is the only commit)."""
    from .casestore import load_plan
    own = tag_sha(project, approved_tag(slice_id))
    if own:
        return own, approved_tag(slice_id)
    plan = load_plan(project)
    if plan is not None:
        order = {s.id: s.order for s in plan.slices}
        mine = order.get(slice_id)
        for s in sorted(plan.slices, key=lambda s: s.order, reverse=True):
            if mine is not None and s.order < mine:
                sha = tag_sha(project, approved_tag(s.id))
                if sha:
                    return sha, approved_tag(s.id)
    roots = git(project, "rev-list", "--max-parents=0", "HEAD").split()
    if roots and roots[-1] != git_head(project):
        return _sha(roots[-1]), "base (root commit)"
    return EMPTY_TREE, "empty tree"


def slice_diff(project, slice_id: str) -> dict:
    """{base, base_label, head, stat, diff, truncated, error} of target_code/ between diff_base and HEAD."""
    out = {"base": "", "base_label": "", "head": "", "stat": "", "diff": "", "truncated": False, "error": ""}
    try:
        head = git_head(project)
        if not head:
            out["error"] = "target_code/ has no git repository or no commit yet"
            return out
        base, label = diff_base(project, slice_id)
        args = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames")
        out.update(base=base, base_label=label, head=head,
                   stat=git(project, *args, "--stat", base, head, "--")[:20_000],
                   diff=git(project, *args, base, head, "--"))
        if len(out["diff"]) > MAX_DIFF_BYTES:
            out["diff"], out["truncated"] = out["diff"][:MAX_DIFF_BYTES], True
    except GitError as e:
        out["error"] = str(e)
    return out


def tag_approved(project, slice_id: str, sha: str) -> str:
    """Lightweight tag slice/SLICE-NNNN/approved at `sha` (moved if it exists). Returns "" or an error."""
    try:
        git(project, "tag", "-f", "--no-sign", approved_tag(slice_id), _sha(sha))
        return ""
    except GitError as e:
        try:   # very old git has no --no-sign
            git(project, "tag", "-f", approved_tag(slice_id), _sha(sha))
            return ""
        except GitError:
            return str(e)


# ------------------------------------------------------------------ iteration results (slice runner)
def latest_iteration(project, slice_id: str) -> Path | None:
    d = Phase2Layout(project).slice_iterations(_check_slice(slice_id))
    if not d.is_dir():
        return None
    nums = sorted(int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit())
    return d / str(nums[-1]) if nums else None


def iterations(project, slice_id: str) -> list[dict]:
    d = Phase2Layout(project).slice_iterations(_check_slice(slice_id))
    if not d.is_dir():
        return []
    out = []
    for n in sorted(int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit()):
        it = d / str(n)
        row: dict = {"iteration": n, "files": sorted(p.name for p in it.glob("*.json"))}
        for name in ("build", "tests"):
            data = _read_any(it / f"{name}.json")
            row[name] = None if data is None else _green(data, set())
        out.append(row)
    return out


def _read_any(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return {"__corrupt__": True}


def _green(data, allowed_failures: set[str]) -> bool:
    if not isinstance(data, dict) or data.get("__corrupt__"):
        return False
    failed = data.get("failed")
    if isinstance(data.get("results"), list):
        failed = [r.get("case_id") for r in data["results"]
                  if isinstance(r, dict) and r.get("outcome") not in ("pass", "passed", "ok")]
    if isinstance(failed, list):
        return not (set(map(str, failed)) - allowed_failures)
    if isinstance(data.get("ok"), bool):
        return data["ok"]
    if "exit_code" in data:
        return data["exit_code"] == 0
    return str(data.get("status", "")).lower() in GREEN


def review_findings(project, slice_id: str) -> list:
    """Reviewer findings from the latest iteration's review.json, else slices/<id>/review.json (any shape)."""
    it = latest_iteration(project, slice_id)
    for p in ([it / "review.json"] if it else []) + [Phase2Layout(project).slice_review(slice_id)]:
        data = _read_any(p)
        if data is not None:
            if isinstance(data, dict):
                return data.get("findings", data.get("issues", [data]))
            return data if isinstance(data, list) else [data]
    return []


# ------------------------------------------------------------------ approval gate
def approval_blockers(project, slice_id: str) -> list[str]:
    from .casestore import load_cases
    from .equivalence import load_fix_requests, load_report, load_run_meta
    from .fit import load_resolutions
    from .state import load_state
    _check_slice(slice_id)
    out: list[str] = []
    try:
        from .contract import ContractError, require_contract
        require_contract(project)
    except ContractError as e:
        out.append(f"contract gate: {e}")
    st = load_state(project, slice_id)
    if st.status != "awaiting_human":
        out.append(f"slice state is {st.status!r}, approval needs 'awaiting_human'")

    rep = load_report(project, slice_id)
    intended: set[str] = set()
    if rep is None:
        out.append("no equivalence report: run `migration-agent equivalence --slice " + slice_id + "`")
    else:
        bad = rep.unexplained
        if bad:
            out.append(f"equivalence: {len(bad)} regression/unknown diff(s): "
                       + ", ".join(f"{d.id} ({d.classification} {d.case_id} {d.field})" for d in bad[:10]))
        by_case: dict[str, set[str]] = {}
        for d in rep.diffs:
            by_case.setdefault(d.case_id, set()).add(d.classification)
        intended = {c for c, kinds in by_case.items() if kinds <= {"intended_divergence", "noise"}}
        meta = load_run_meta(project, slice_id)
        head = git_head(project)
        eq_head = meta.target_head if meta else ""
        if head and head != eq_head:
            try:
                humans = human_commits(project, eq_head or None) if eq_head else human_commits(project)
            except GitError:
                humans = []
            if humans:
                out.append(f"{len(humans)} human commit(s) in target_code/ since the last equivalence run "
                           f"({', '.join(c['sha'][:10] for c in humans[:5])}): re-run build, tests and equivalence")
            else:
                out.append("target_code/ HEAD changed since the last equivalence run: re-run equivalence")

    it = latest_iteration(project, slice_id)
    build = _read_any(it / "build.json") if it else None
    if build is None:
        out.append("no build result for the latest iteration")
    elif not _green(build, set()):
        out.append(f"build of iteration {it.name} is not green")
    tests = _read_any(it / "tests.json") if it else None
    if tests is None:
        out.append("no test-suite result for the latest iteration")
    elif not _green(tests, intended):
        out.append(f"test suite of iteration {it.name} is not green (beyond approved intended divergences)")

    accepted = load_decisions(project, slice_id).accepted()
    risk_done = set(load_resolutions(project).current())
    for b in slice_blockers(project, slice_id):
        if b.startswith("F-") and b not in accepted:
            out.append(f"blocker {b}: resolve it or `slice accept-observed {slice_id} {b}`")
        elif b.startswith("RISK-") and b not in risk_done:
            out.append(f"blocker {b}: resolve the fit risk (`target resolve`)")
        elif not b.startswith(("F-", "RISK-")):
            out.append(f"blocker {b}")
    for d in load_decisions(project, slice_id).open():
        out.append(f"open {d.kind} decision on {d.ref}" + (f" ({d.case_id})" if d.case_id else ""))
    pending = [r for r in load_fix_requests(project, slice_id).requests if r.status == "pending"]
    if pending:
        out.append(f"{len(pending)} pending fix request(s): " + ", ".join(r.eq_id for r in pending[:10]))
    unapproved = [c.id for c in load_cases(project)
                  if c.slice == slice_id and c.expect.source == "specified" and not c.expect.approved_by]
    if unapproved:
        out.append("unapproved specified expectation(s): " + ", ".join(unapproved[:10]))
    return out


def _hashes(project, slice_id: str) -> dict[str, str]:
    lay = Phase2Layout(project)
    files = [lay.slice_equivalence(slice_id), lay.slice_dir(slice_id) / "equivalence_run.json",
             lay.slice_decisions(slice_id), lay.slice_review(slice_id), lay.equivalence_report_md]
    it = latest_iteration(project, slice_id)
    if it:
        files += [it / n for n in ("build.json", "tests.json", "review.json", "edits.json")]
    return {lay.rel(f): sha256_file(f) for f in files if f.is_file()}


def approve_slice(project, slice_id: str, by: str, note: str = "") -> SliceApproval:
    """Approve one slice (ApprovalError with `.blockers` when refused). Writes approval.json, moves the state to
    approved and tags target_code `slice/SLICE-NNNN/approved` when it is a git repository."""
    from .state import IllegalTransition, transition
    _check_slice(slice_id)
    by = _redact(by, 64)
    if not by:
        raise ApprovalError("--by is required")
    lay = Phase2Layout(project)
    with file_lock(lay.slice_approval(slice_id)):
        blockers = approval_blockers(project, slice_id)
        if blockers:
            _audit(project, "approve_slice_refused", slice=slice_id, by=by, blockers=blockers[:20])
            raise ApprovalError(f"{slice_id} cannot be approved:\n  - " + "\n  - ".join(blockers), blockers)
        head = git_head(project)
        accepted = sorted(load_decisions(project, slice_id).accepted())
        appr = SliceApproval(slice_id=slice_id, approved_by=by, approved_at=now_iso(), commit_sha=head,
                             report_hashes=_hashes(project, slice_id), accepted_blockers=accepted,
                             note=_redact(note, 2000))
        try:
            transition(project, slice_id, "approved", actor=f"human:{by}", reason=appr.note or "approved")
        except IllegalTransition as e:
            raise ApprovalError(str(e), [str(e)]) from None
        atomic_write_json(lay.slice_approval(slice_id), appr)
    tag_err = tag_approved(project, slice_id, head) if head else ""
    _audit(project, "approve_slice", slice=slice_id, by=by, commit=head, tag_error=tag_err[:200])
    return appr


def reject_slice(project, slice_id: str, by: str, reason: str) -> dict:
    """awaiting_human -> rejected (reason required)."""
    from .state import IllegalTransition, transition
    _check_slice(slice_id)
    by, reason = _redact(by, 64), _redact(reason, 2000)
    if not by:
        raise ApprovalError("--by is required")
    if not reason:
        raise ApprovalError("a reason is required to reject a slice")
    try:
        st = transition(project, slice_id, "rejected", actor=f"human:{by}", reason=reason)
    except IllegalTransition as e:
        raise ApprovalError(str(e), [str(e)]) from None
    _audit(project, "reject_slice", slice=slice_id, by=by)
    return {"slice_id": slice_id, "status": st.status, "by": by, "reason": reason}


def load_approval(project, slice_id: str) -> SliceApproval | None:
    return read_json(Phase2Layout(project).slice_approval(_check_slice(slice_id)), SliceApproval, None)


# ------------------------------------------------------------------ CLI
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _slice_group(main: click.Group) -> click.Group:
    existing = main.commands.get("slice")
    if isinstance(existing, click.Group):
        return existing
    if existing is not None:
        raise RuntimeError("`slice` already exists as a command; cannot create the group")
    grp = click.Group(name="slice", help="Per-slice human decisions and comments.")
    main.add_command(grp)
    return grp


def register(groups) -> None:
    sg = _slice_group(groups.main)

    @groups.main.command("approve-slice")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.option("--by", required=True, help="Who approves.")
    @click.option("--note", default="")
    def approve_cmd(project: Path, slice_id: str, by: str, note: str) -> None:
        """Approve a slice (refused while any gate fails; lists every blocker)."""
        try:
            a = approve_slice(_project(project), slice_id, by, note)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: approved by {a.approved_by}" + (f" at {a.commit_sha[:12]}" if a.commit_sha else ""))

    @groups.main.command("reject-slice")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.option("--by", required=True)
    @click.option("--reason", required=True)
    def reject_cmd(project: Path, slice_id: str, by: str, reason: str) -> None:
        """Reject a slice awaiting human review."""
        try:
            reject_slice(_project(project), slice_id, by, reason)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: rejected")

    @sg.command("accept-observed")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.argument("finding_id")
    @click.option("--by", required=True)
    @click.option("--note", required=True)
    def accept_cmd(project: Path, slice_id: str, finding_id: str, by: str, note: str) -> None:
        """Accept 'migrate legacy behaviour as observed' for a deferred finding blocking the slice."""
        try:
            accept_observed(_project(project), slice_id, finding_id, by, note)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: {finding_id} accepted as observed")

    @sg.command("decide")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.argument("ref")
    @click.option("--choice", type=click.Choice(["preserve", "fix", "answered"]), required=True)
    @click.option("--by", required=True)
    @click.option("--note", default="")
    def decide_cmd(project: Path, slice_id: str, ref: str, choice: str, by: str, note: str) -> None:
        """Answer an open legacy_bug / spec_ambiguity decision raised by equivalence triage."""
        try:
            resolve_decision(_project(project), slice_id, ref, choice, by, note)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: {ref} -> {choice}")

    @sg.command("review-finding")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("slice_id")
    @click.argument("finding", metavar="ITERATION:INDEX")
    @click.option("--accept", "verdict", flag_value="accepted", help="A real problem.")
    @click.option("--dismiss", "verdict", flag_value="dismissed", help="Noise or wrong.")
    @click.option("--by", required=True)
    @click.option("--note", default="")
    def review_finding_cmd(project: Path, slice_id: str, finding: str, verdict: str | None, by: str,
                           note: str) -> None:
        """Mark a reviewer finding accepted or dismissed (feeds reviewer precision in `metrics`)."""
        m = re.fullmatch(r"(\d+):(\d+)", finding or "")
        if not m or not verdict:
            raise click.ClickException("usage: slice review-finding PROJECT SLICE-NNNN K:I (--accept|--dismiss) --by NAME")
        try:
            mark_review_finding(_project(project), slice_id, int(m.group(1)), int(m.group(2)), verdict, by, note)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{slice_id}: finding {finding} {verdict}")

    @sg.command("comment")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.argument("item_id")
    @click.option("--text", required=True)
    @click.option("--file", "file_", default=None, help="target_code-relative file")
    @click.option("--line", type=int, default=None)
    @click.option("--by", default="user")
    def comment_cmd(project: Path, item_id: str, text: str, file_: str | None, line: int | None, by: str) -> None:
        """Comment on a Phase 2 / graph ID (optionally a target_code file:line)."""
        try:
            c = add_comment(_project(project), item_id, text, author=by, file=file_, line=line)
        except ApprovalError as e:
            raise click.ClickException(str(e))
        click.echo(f"{c.id} on {c.item_id}" + (f" ({c.file}:{c.line or ''})" if c.file else ""))
