"""Phase 2 metrics (docs/phase2-plan.md section 10), derived from existing files only (no new storage):

    migration-agent metrics PROJECT [--format table|json]

Sources: audit/audit.jsonl (`llm_call` records tagged slice=SLICE-NNNN: cost, tokens, `cached`; `generate_step` spans
of the slice runner: duration per step; `exec_run` records: duration and labels.slice), slices/SLICE-NNNN/
(state.json, iterations/K/{build,tests,review}.json, equivalence.json), characterization/recordings/*/report.json
(masks added at recording), equivalence/masks.json (approved masks), migration/comments.json,
migration/runtime_findings.json, plan/migration_plan.json and target_code/ git history (approvals.human_edit_stats;
per slice: commits between the previous approved tag and `slice/SLICE-NNNN/approved`).

`compute_metrics(project) -> dict` is a pure read (UI / service.run_stage use it). A metric without data is None
(rendered "n/a"), never 0: e.g. reviewer precision needs findings a human has marked, with
`migration-agent slice review-finding PROJECT SLICE K:I --accept|--dismiss` (stored in
slices/SLICE-NNNN/review_marks.json) or the review UI, or a finding dict with `"accepted": true` /
`"status": "accepted"`. Precision = accepted / marked.

Definitions:
  cost per slice / node / 1k legacy lines  sum of llm_call cost_usd tagged slice= ; legacy lines = text lines in code/
  cache hit rate          cached llm_call records / llm_call records
  iterations to green     first iteration K whose tests.json is green (None while never green)
  build first try         iteration 1 build.json ok (per slice; % over slices with a build; per target kind)
  characterization rate   passed / total of the latest iteration's characterization suite, cases whose only
                          differences are approved intended divergences removed from both
  equivalence %           cases_equivalent / cases_total of the slice equivalence report (aggregate: summed)
  unknown-diff rate       unknown diffs / all non-match diffs of the slice report
  mask additions          masks added while recording (auto noise masks) + human-approved equivalence masks
  human-edit rate         lines changed by human commits / lines changed by agent commits
  wall-clock              generate_step durations: model = codegen + review, exec = build + tests (exec_run
                          durations labelled with the slice when no generate_step spans exist)
  runtime findings        count; confirmed = status accepted_recorded or reopened_phase1
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click

from .paths import Phase2Layout

MODEL_STEPS = frozenset({"codegen", "review"})
EXEC_STEPS = frozenset({"build", "tests"})
GREEN = ("green", "ok", "passed", "pass", "success", "succeeded")
_TEXT_EXT_SKIP = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".jar", ".class", ".so",
                            ".dll", ".exe", ".woff", ".woff2", ".ttf", ".gz", ".tar", ".bin", ".db", ".sqlite3"})
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "target", "build", "dist"})


# ------------------------------------------------------------------ small readers
def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _audit_entries(project) -> list[dict]:
    p = Path(project.audit_dir) / "audit.jsonl"
    out: list[dict] = []
    if not p.exists():
        return out
    with open(p, "rb") as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def _num(v, cast=float, default=0):
    try:
        return cast(v or 0)
    except (TypeError, ValueError):
        return default


def _rate(a: float, b: float, nd: int = 4) -> float | None:
    return round(a / b, nd) if b else None


def _pct(a: float, b: float) -> float | None:
    return round(100.0 * a / b, 2) if b else None


def _green(data) -> bool:
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("ok"), bool):
        return data["ok"]
    if "result" in data:
        return str(data["result"]).lower() in GREEN
    if "exit_code" in data:
        return data["exit_code"] == 0
    return str(data.get("status", "")).lower() in GREEN


def legacy_lines(project) -> int:
    """Text lines in code/ (the Phase 1 copy of the legacy source)."""
    root = Path(project.code)
    n = 0
    if not root.is_dir():
        return 0
    for dp, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            p = Path(dp) / name
            if p.is_symlink() or p.suffix.lower() in _TEXT_EXT_SKIP:
                continue
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            n += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return n


def _iterations(lay: Phase2Layout, sid: str) -> list[tuple[int, Path]]:
    d = lay.slice_iterations(sid)
    if not d.is_dir():
        return []
    return sorted((int(p.name), p) for p in d.iterdir() if p.is_dir() and p.name.isdigit())


# ------------------------------------------------------------------ git attribution per slice
def _numstat(project, rng: str) -> dict:
    from .approvals import AGENT_EMAIL, AGENT_NAME, git
    out = {"human_commits": 0, "agent_commits": 0, "human_lines": 0, "agent_lines": 0}
    log = git(project, "log", "--no-color", "--numstat", "--format=@@%an%x1f%ae", rng, "--")
    who = None
    for line in log.splitlines():
        if line.startswith("@@"):
            an, _, ae = line[2:].partition("\x1f")
            who = "agent" if an == AGENT_NAME and ae.lower() == AGENT_EMAIL else "human"
            out[f"{who}_commits"] += 1
        elif who and line.strip():
            a, _, rest = line.partition("\t")
            d, _, _ = rest.partition("\t")
            out[f"{who}_lines"] += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return out


def _slice_edits(project, ordered_ids: list[str]) -> dict[str, dict]:
    """Per approved slice: commits between the previous approved tag (plan order) and its own approved tag."""
    from .approvals import GitError, approved_tag, git_head, tag_sha
    out: dict[str, dict] = {}
    try:
        if not git_head(project):
            return out
        prev = ""
        for sid in ordered_ids:
            sha = tag_sha(project, approved_tag(sid))
            if not sha:
                continue
            out[sid] = _numstat(project, f"{prev}..{sha}" if prev else sha)
            prev = sha
    except GitError:
        return out
    return out


# ------------------------------------------------------------------ main computation
def compute_metrics(project) -> dict:
    """All plan-section-10 metrics as a JSON-ready dict (see module docstring)."""
    from .casestore import load_cases, load_plan
    from .equivalence import load_report
    from .state import load_state
    lay = Phase2Layout(project)
    plan = load_plan(project)
    slices = sorted(plan.slices, key=lambda s: (s.order, s.id)) if plan is not None else []
    ids = [s.id for s in slices]
    entries = _audit_entries(project)

    # ---- audit: model calls, spans, exec runs
    tot = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0, "cached_calls": 0}
    per: dict[str, dict] = {sid: {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "calls": 0, "cached_calls": 0,
                                  "model_s": 0.0, "exec_s": 0.0, "exec_runs_s": 0.0, "spans": 0} for sid in ids}
    for e in entries:
        act = e.get("action")
        sid = e.get("slice") if isinstance(e.get("slice"), str) else None
        if act == "llm_call":
            cost, tin, tout = _num(e.get("cost_usd")), _num(e.get("tokens_in"), int), _num(e.get("tokens_out"), int)
            for bucket in [tot] + ([per[sid]] if sid in per else []):
                bucket["cost_usd"] += cost
                bucket["tokens_in"] += tin
                bucket["tokens_out"] += tout
                bucket["calls"] += 1
                bucket["cached_calls"] += 1 if e.get("cached") else 0
        elif act == "generate_step" and sid in per:
            dur = _num(e.get("duration_ms")) / 1000.0
            step = e.get("step")
            if step in MODEL_STEPS:
                per[sid]["model_s"] += dur
            elif step in EXEC_STEPS:
                per[sid]["exec_s"] += dur
            per[sid]["spans"] += 1
        elif act == "exec_run":
            labels = e.get("labels") if isinstance(e.get("labels"), dict) else {}
            ls = labels.get("slice")
            if ls in per:
                per[ls]["exec_runs_s"] += _num(e.get("duration_ms")) / 1000.0

    # ---- cases, masks, comments
    cases = load_cases(project)
    case_slice = {c.id: c.slice for c in cases}
    masks_rec: dict[str, int] = {}
    rec_dir = lay.recordings_dir
    if rec_dir.is_dir():
        for rp in sorted(rec_dir.glob("RUN-*/report.json")):
            data = _read(rp) or {}
            for c in data.get("cases") or []:
                if isinstance(c, dict) and c.get("masks_added"):
                    s = case_slice.get(str(c.get("case_id"))) or "unplanned"
                    masks_rec[s] = masks_rec.get(s, 0) + len(c["masks_added"])
    masks_appr: dict[str, int] = {}
    mf = _read(lay.equivalence_dir / "masks.json") or {}
    for m in mf.get("masks") or []:
        if isinstance(m, dict) and m.get("status") == "approved":
            s = case_slice.get(str(m.get("case_id"))) or "unplanned"
            masks_appr[s] = masks_appr.get(s, 0) + 1
    comments: dict[str, int] = {}
    cf = _read(lay.comments) or {}
    node_slice = {n: s.id for s in slices for n in s.node_ids}
    for c in cf.get("comments") or []:
        if not isinstance(c, dict):
            continue
        item = str(c.get("item_id", ""))
        s = item if item in per else node_slice.get(item) or case_slice.get(item)
        if s:
            comments[s] = comments.get(s, 0) + 1

    edits = _slice_edits(project, ids)
    try:
        from .approvals import human_edit_stats
        hes = human_edit_stats(project)
    except Exception:  # noqa: BLE001 - no git / broken repo: no attribution
        hes = {"human_commits": 0, "agent_commits": 0, "human_lines": 0, "agent_lines": 0}

    try:
        from .target import load_target
        kind = load_target(project).kind
    except Exception:  # noqa: BLE001
        kind = "unknown"

    # ---- per slice
    rows = []
    agg = {"builds": 0, "first_try": 0, "char_pass": 0, "char_total": 0, "eq_ok": 0, "eq_total": 0, "unknown": 0,
           "diffs": 0, "masks": 0, "rev_total": 0, "rev_marked": 0, "rev_accepted": 0, "itg": [], "model_s": 0.0,
           "exec_s": 0.0}
    for s in slices:
        a = per[s.id]
        its = _iterations(lay, s.id)
        builds = [(k, _read(p / "build.json")) for k, p in its]
        tests = [(k, _read(p / "tests.json")) for k, p in its]
        first_build = next((b for k, b in builds if k == 1 and b is not None), None)
        build_first = None if first_build is None else _green(first_build)
        itg = next((k for k, t in tests if t is not None and _green(t)), None)
        rep = load_report(project, s.id)
        intended: set[str] = set()
        if rep is not None:
            by_case: dict[str, set[str]] = {}
            for d in rep.diffs:
                by_case.setdefault(d.case_id, set()).add(d.classification)
            intended = {c for c, k in by_case.items() if "intended_divergence" in k and k <= {"intended_divergence",
                                                                                               "noise"}}
        char = {"passed": None, "total": None, "rate": None}
        last_t = next((t for _k, t in reversed(tests) if isinstance(t, dict)), None)
        ch = last_t.get("characterization") if isinstance(last_t, dict) else None
        if isinstance(ch, dict) and isinstance(ch.get("totals"), dict) and ch["totals"]:
            total = sum(_num(v, int) for v in ch["totals"].values())
            failed = {str(f.get("case_id")) for f in ch.get("failures") or [] if isinstance(f, dict)}
            excl = len(failed & intended)
            total -= excl
            passed = _num(ch["totals"].get("pass"), int)
            char = {"passed": passed, "total": total, "rate": _rate(passed, total)}
            agg["char_pass"] += passed
            agg["char_total"] += total
        eq = {"pct": None, "cases_total": None, "unknown_diffs": None, "diffs": None, "unknown_diff_rate": None}
        if rep is not None:
            nonmatch = [d for d in rep.diffs if d.classification != "match"]
            unk = sum(d.classification == "unknown" for d in nonmatch)
            eq = {"pct": rep.equivalence_pct, "cases_total": rep.cases_total, "unknown_diffs": unk,
                  "diffs": len(nonmatch), "unknown_diff_rate": _rate(unk, len(nonmatch))}
            agg["eq_ok"] += rep.cases_equivalent
            agg["eq_total"] += rep.cases_total
            agg["unknown"] += unk
            agg["diffs"] += len(nonmatch)
        masks = masks_rec.get(s.id, 0) + masks_appr.get(s.id, 0)
        agg["masks"] += masks
        # reviewer findings over all iterations
        rv_total = rv_marked = rv_acc = 0
        try:
            from .approvals import load_review_marks
            marks = load_review_marks(project, s.id)
        except Exception:  # noqa: BLE001 - metrics never fail on a bad marks file
            marks = {}
        for k, p in its:
            data = _read(p / "review.json")
            for i, f in enumerate((data.get("findings") if isinstance(data, dict) else None) or []):
                if not isinstance(f, dict):
                    continue
                rv_total += 1
                mk = marks.get(f"{k}:{i}") if isinstance(marks.get(f"{k}:{i}"), dict) else None
                if mk and mk.get("status") in ("accepted", "dismissed"):
                    rv_marked += 1
                    rv_acc += 1 if mk["status"] == "accepted" else 0
                elif "accepted" in f or "status" in f:
                    rv_marked += 1
                    rv_acc += 1 if (f.get("accepted") is True or f.get("status") == "accepted") else 0
        agg["rev_total"] += rv_total
        agg["rev_marked"] += rv_marked
        agg["rev_accepted"] += rv_acc
        ed = edits.get(s.id)
        model_s = round(a["model_s"], 3)
        exec_s = round(a["exec_s"] if a["spans"] else a["exec_runs_s"], 3)
        agg["model_s"] += model_s
        agg["exec_s"] += exec_s
        if build_first is not None:
            agg["builds"] += 1
            agg["first_try"] += 1 if build_first else 0
        if itg is not None:
            agg["itg"].append(itg)
        cost = round(a["cost_usd"], 6)
        rows.append({
            "slice_id": s.id, "name": s.name, "kind": s.kind, "status": load_state(project, s.id).status,
            "nodes": len(s.node_ids), "cost_usd": cost, "cost_per_node_usd": _rate(cost, len(s.node_ids), 6),
            "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"], "calls": a["calls"],
            "cache_hit_rate": _rate(a["cached_calls"], a["calls"]), "iterations": len(its),
            "iterations_to_green": itg, "build_first_try": build_first, "characterization": char,
            "equivalence": eq, "mask_additions": masks,
            "human_edits": ed, "human_edit_rate": _rate(ed["human_lines"], ed["agent_lines"]) if ed else None,
            "comments": comments.get(s.id, 0),
            "reviewer": {"findings": rv_total, "marked": rv_marked, "accepted": rv_acc,
                         "precision": _rate(rv_acc, rv_marked)},
            "wall_clock_s": {"model": model_s, "exec": exec_s, "total": round(model_s + exec_s, 3)},
        })

    # ---- runtime findings
    rf = {"total": 0, "confirmed": 0, "open": 0, "by_kind": {}}
    data = _read(lay.runtime_findings) or {}
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        rf["total"] += 1
        st = f.get("status", "open")
        rf["confirmed"] += 1 if st in ("accepted_recorded", "reopened_phase1") else 0
        rf["open"] += 1 if st == "open" else 0
        k = str(f.get("kind", "unknown"))
        rf["by_kind"][k] = rf["by_kind"].get(k, 0) + 1

    lines = legacy_lines(project)
    p2_cost = round(sum(r["cost_usd"] for r in rows), 6)
    first_try_pct = _pct(agg["first_try"], agg["builds"])
    itg = agg["itg"]
    return {
        "schema_version": 1,
        "project": Path(project.root).name,
        "target_kind": kind,
        "legacy_lines": lines,
        "totals": {
            "cost_usd": round(tot["cost_usd"], 6), "slice_cost_usd": p2_cost,
            "cost_per_1k_legacy_lines_usd": _rate(p2_cost * 1000, lines, 6),
            "tokens_in": tot["tokens_in"], "tokens_out": tot["tokens_out"], "calls": tot["calls"],
            "cached_calls": tot["cached_calls"], "cache_hit_rate": _rate(tot["cached_calls"], tot["calls"]),
        },
        "aggregate": {
            "slices": len(rows), "approved": sum(r["status"] == "approved" for r in rows),
            "build_first_try_pct": first_try_pct,
            "build_first_try_pct_by_target_kind": {kind: first_try_pct},
            "iterations_to_green_avg": round(sum(itg) / len(itg), 2) if itg else None,
            "characterization_pass_rate": _rate(agg["char_pass"], agg["char_total"]),
            "equivalence_pct": _pct(agg["eq_ok"], agg["eq_total"]),
            "unknown_diff_rate": _rate(agg["unknown"], agg["diffs"]),
            "mask_additions": agg["masks"] + masks_rec.get("unplanned", 0) + masks_appr.get("unplanned", 0),
            "human_edit_rate": _rate(hes["human_lines"], hes["agent_lines"]),
            "comments": sum(comments.values()),
            "reviewer_precision": _rate(agg["rev_accepted"], agg["rev_marked"]),
            "reviewer_findings": agg["rev_total"],
            "wall_clock_s": {"model": round(agg["model_s"], 3), "exec": round(agg["exec_s"], 3),
                             "total": round(agg["model_s"] + agg["exec_s"], 3)},
        },
        "human_edits": hes,
        "runtime_findings": rf,
        "slices": rows,
    }


# ------------------------------------------------------------------ rendering
def _fmt(v: Any, kind: str = "") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if kind == "usd":
        return f"${v:.4f}"
    if kind == "rate":
        return f"{100 * v:.1f}%"
    if kind == "pct":
        return f"{v:.2f}%"
    return str(v)


def render_table(m: dict) -> str:
    t, a, rf = m["totals"], m["aggregate"], m["runtime_findings"]
    out = [f"metrics: {m['project']} ({m['target_kind']}, {m['legacy_lines']} legacy lines)", "",
           f"cost total {_fmt(t['cost_usd'], 'usd')}, slices {_fmt(t['slice_cost_usd'], 'usd')}, per 1k legacy lines "
           f"{_fmt(t['cost_per_1k_legacy_lines_usd'], 'usd')}",
           f"tokens in/out {t['tokens_in']}/{t['tokens_out']}, calls {t['calls']}, cache hit rate "
           f"{_fmt(t['cache_hit_rate'], 'rate')}",
           f"slices {a['slices']} ({a['approved']} approved), build first try {_fmt(a['build_first_try_pct'], 'pct')}, "
           f"iterations to green avg {_fmt(a['iterations_to_green_avg'])}",
           f"characterization pass rate {_fmt(a['characterization_pass_rate'], 'rate')}, equivalence "
           f"{_fmt(a['equivalence_pct'], 'pct')}, unknown-diff rate {_fmt(a['unknown_diff_rate'], 'rate')}, "
           f"mask additions {a['mask_additions']}",
           f"human-edit rate {_fmt(a['human_edit_rate'], 'rate')} ({m['human_edits']['human_lines']} human / "
           f"{m['human_edits']['agent_lines']} agent lines), comments {a['comments']}, reviewer precision "
           f"{_fmt(a['reviewer_precision'], 'rate')} ({a['reviewer_findings']} finding(s))",
           f"wall-clock model {a['wall_clock_s']['model']}s, exec {a['wall_clock_s']['exec']}s",
           f"runtime findings {rf['total']} ({rf['confirmed']} confirmed, {rf['open']} open)", ""]
    cols = ["slice", "status", "cost", "tok in/out", "cache", "iters", "to green", "build 1st", "char", "equiv",
            "unknown", "masks", "human", "comments", "model s", "exec s"]
    rows = []
    for r in m["slices"]:
        rows.append([r["slice_id"], r["status"], _fmt(r["cost_usd"], "usd"), f"{r['tokens_in']}/{r['tokens_out']}",
                     _fmt(r["cache_hit_rate"], "rate"), str(r["iterations"]), _fmt(r["iterations_to_green"]),
                     _fmt(r["build_first_try"]), _fmt(r["characterization"]["rate"], "rate"),
                     _fmt(r["equivalence"]["pct"], "pct"), _fmt(r["equivalence"]["unknown_diff_rate"], "rate"),
                     str(r["mask_additions"]), _fmt(r["human_edit_rate"], "rate"), str(r["comments"]),
                     str(r["wall_clock_s"]["model"]), str(r["wall_clock_s"]["exec"])])
    widths = [max(len(c), *(len(x[i]) for x in rows)) if rows else len(c) for i, c in enumerate(cols)]
    line = lambda r: "  ".join(x.ljust(w) for x, w in zip(r, widths)).rstrip()  # noqa: E731
    out += [line(cols), line(["-" * w for w in widths]), *map(line, rows)]
    if not rows:
        out.append("(no slices: run `migration-agent plan PROJECT`)")
    return "\n".join(out) + "\n"


def register(groups) -> None:
    @groups.main.command("metrics")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
    def metrics_cmd(project: Path, fmt: str) -> None:
        """Phase 2 metrics from the audit log, slice files and target_code git history."""
        from ..project import Project
        try:
            p = Project.load(project)
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))
        m = compute_metrics(p)
        click.echo(json.dumps(m, indent=2) if fmt == "json" else render_table(m), nl=fmt == "json")
