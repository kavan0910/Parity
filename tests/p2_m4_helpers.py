"""Shared builders for the M4 generation-loop tests: an approved, planned express-crud project with a
target.yaml, a passing characterization suite stub, and LLM doubles."""
from __future__ import annotations

import json
from pathlib import Path

from migration_agent.llm.base import LLMResponse
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.planner import plan_project

from phase2_helpers import approved_project

TARGET_YAML = ("kind: framework_move\nstrategy: strangler\nsource: {language: javascript}\n"
               "target: {language: typescript, build: npm}\n"
               "libraries: {allow: ['express', 'fastify*'], deny: ['left-pad']}\n")


def m4_project(tmp_path: Path, tmp_path_factory, target_yaml: str = TARGET_YAML):
    p = approved_project(tmp_path, tmp_path_factory)
    plan_project(p)                       # target-agnostic plan (no fit report needed)
    lay = Phase2Layout(p)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text(target_yaml)
    return p


def plan(p):
    from migration_agent.migrate.casestore import load_plan
    return load_plan(p)


def ready_slice(p):
    """A foundation slice without dependencies or blockers (needs no recorded cases)."""
    return next(s for s in sorted(plan(p).slices, key=lambda s: s.order)
                if not s.depends_on and not s.blockers and s.kind == "foundation")


def passing_suite(project, slice_, cases, *, backend):
    from migration_agent.migrate.slice_runner import SuiteResult
    return SuiteResult(status="passed", totals={"pass": len(cases)})


def reply(path: str, content: str = "x = 1\n", op: str = "create", **extra) -> str:
    f = {"path": path, "op": op}
    if op == "edit":
        f["edits"] = extra.pop("edits")
    else:
        f["content"] = content
    return json.dumps({"files": [f], "notes": extra.get("notes", "n"), "covers": [], "assumptions": [],
                       "need": []})


class ScriptLLM:
    """codegen replies from `codegen` (list consumed in order, last repeats, or callable(prompt, n));
    code_review replies from `review`. Records calls; `cost` per call."""

    def __init__(self, codegen=None, review='{"findings": []}', cost: float = 0.0):
        self.codegen = codegen
        self.review = review
        self.cost = cost
        self.calls: list[dict] = []

    def complete(self, *, task, system, prompt, json_mode=True, prompt_version="v1"):
        self.calls.append({"task": task, "prompt": prompt, "system": system, "prompt_version": prompt_version})
        if task == "code_review":
            text = self.review(prompt) if callable(self.review) else self.review
        else:
            n = sum(1 for c in self.calls if c["task"] == "codegen")
            if callable(self.codegen):
                text = self.codegen(prompt, n)
            else:
                seq = self.codegen or []
                text = seq[min(n - 1, len(seq) - 1)]
        return LLMResponse(text=text, model="script", tokens_in=len(prompt) // 4, tokens_out=len(text) // 4,
                           cost_usd=self.cost)

    def n(self, task: str) -> int:
        return sum(1 for c in self.calls if c["task"] == task)


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out
