"""M4 reviewer (docs/phase2-plan.md 5.4, threat model "Model writes malicious code").

Fresh-session `code_review` task (prompt prompts/code_review.md): the model sees ONLY the slice spec (nodes,
rules, decisions, fit resolutions) and the slice diff, never the legacy code or the generator's reasoning.
Findings {kind, item_ids, file, line, quote, detail, blocking}; a finding whose quote is not verbatim in the
diff (C-09 pattern) is dropped and counted (`dropped_unverified`); unknown item ids are removed from findings;
`style` and `dead_code` findings are never blocking (they go to the human review page).

Deterministic static deny checks on ADDED lines of code files (`static_checks`), each a blocking `security`
finding with the offending line as quote:
  outbound_http   a string URL whose host is not declared (loopback, harness service names, declared hosts)
  dynamic_exec    eval/exec/compile-and-run, Function(), script engines
  shell_spawn     subprocess/os.system/popen, child_process, Runtime.exec/ProcessBuilder, exec.Command
  env_secret      reading an environment variable whose name looks like a credential

Own models: ReviewFinding, ReviewResult (written to slices/SLICE-NNNN/iterations/K/review.json and
slices/SLICE-NNNN/review.json by the slice runner).
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_FILE = Path(__file__).parent / "prompts" / "code_review.md"
FindingKindR = Literal["missing_rule", "behaviour_change", "style", "security", "dead_code"]
NEVER_BLOCKING = frozenset({"style", "dead_code"})
MAX_DIFF_TOKENS = 60_000
MAX_FINDINGS = 30
CODE_EXTS = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts", ".go", ".cs",
                       ".rb", ".php", ".scala", ".groovy", ".rs", ".sh", ".bash"})
DEFAULT_DECLARED_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "legacy", "new", "db",
                                    "router", "example.com", "example.org", "example.net"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewFinding(_Strict):
    kind: FindingKindR
    item_ids: list[str] = Field(default_factory=list)
    file: str = ""
    line: int | None = None
    quote: str = ""
    detail: str = ""
    blocking: bool = False
    source: Literal["model", "static"] = "model"
    rule: str = ""                     # static check id (outbound_http, dynamic_exec, shell_spawn, env_secret)


class ReviewResult(_Strict):
    schema_version: Literal[1] = 1
    slice_id: str
    iteration: int = 0
    prompt_version: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)
    dropped_unverified: int = 0
    invalid_findings: int = 0
    diff_files: list[str] = Field(default_factory=list)
    diff_truncated: bool = False
    model_error: str = ""
    created: str = ""

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.blocking]


# ------------------------------------------------------------------ diff parsing
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(diff: str) -> list[tuple[str, int | None, str, str]]:
    """[(file, new_line or None, sign '+'|'-'|' ', text)] from a unified git diff."""
    out = []
    cur = ""
    new_no = 0
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            cur = ""
            continue
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = "" if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            continue
        if ln.startswith("--- "):
            if not cur:
                p = ln[4:].strip()
                cur = "" if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            continue
        m = _HUNK.match(ln)
        if m:
            new_no = int(m.group(1))
            continue
        if not ln or ln[0] not in "+- " or ln.startswith("\\"):
            continue
        if ln[0] == "+":
            out.append((cur, new_no, "+", ln[1:]))
            new_no += 1
        elif ln[0] == " ":
            out.append((cur, new_no, " ", ln[1:]))
            new_no += 1
        else:
            out.append((cur, None, "-", ln[1:]))
    return out


def diff_files(diff: str) -> list[str]:
    return sorted({f for f, _, _, _ in parse_diff(diff) if f})


def quote_in_diff(quote: str, diff: str) -> bool:
    q = quote.strip()
    if len(q) < 3:
        return False
    if q in diff:
        return True
    body = "\n".join(t for _, _, _, t in parse_diff(diff))
    return q in body


# ------------------------------------------------------------------ static deny checks
_URL = re.compile(r"""["'`](?:https?|wss?|ftp)://([A-Za-z0-9._\-\[\]:]+)""")
_EXEC = re.compile(r"(?<![\w.])(?:eval|exec)\s*\(|\bnew\s+Function\s*\(|(?<![\w.])Function\s*\(\s*['\"]|"
                   r"\bScriptEngineManager\b|\bcompile\s*\([^)]*['\"]exec['\"]|\b__import__\s*\(|"
                   r"\bvm\.run\w*\s*\(|\bGroovyShell\b")
_SHELL = re.compile(r"\bsubprocess\.\w+|\bos\.(?:system|popen|exec\w*|spawn\w*)\s*\(|\bpty\.spawn\b|"
                    r"\bchild_process\b|\bexecSync\s*\(|\bspawnSync\s*\(|\bexecFile\w*\s*\(|"
                    r"Runtime\.getRuntime\(\)\s*\.exec|\bnew\s+ProcessBuilder\b|\bexec\.Command(?:Context)?\s*\(|"
                    r"\bProcess\.Start\s*\(|\bshell_exec\s*\(|\bpopen\s*\(|`[^`]*\$\(")
_ENV_READ = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|process\.env\s*\[\s*|"""
                       r"""System\.getenv\s*\(\s*|os\.Getenv\s*\(\s*|Environment\.GetEnvironmentVariable\s*\(\s*|"""
                       r"""ENV\s*\[\s*)['"]([A-Za-z_][A-Za-z0-9_]*)['"]|process\.env\.([A-Za-z_][A-Za-z0-9_]*)""")


def _host(raw: str) -> str:
    h = raw.strip().lower()
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.split(":", 1)[0].split("/", 1)[0]


def _host_declared(host: str, declared: set[str]) -> bool:
    if host in declared or host.endswith((".invalid", ".test", ".localhost", ".example")):
        return True
    return any(host.endswith("." + d) for d in declared if "." in d)


def _comment_line(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(("#", "//", "/*", "*", "--", "<!--"))


def _secret_env_name(name: str) -> bool:
    from .exec.policy import FORBIDDEN_ENV_PREFIXES, SECRET_NAME
    return bool(SECRET_NAME.search(name)) or name.upper().startswith(FORBIDDEN_ENV_PREFIXES)


def static_checks(diff: str, declared_hosts: set[str] | None = None) -> list[ReviewFinding]:
    declared = set(DEFAULT_DECLARED_HOSTS) | {h.lower() for h in (declared_hosts or set())}
    out: list[ReviewFinding] = []
    for f, line, sign, text in parse_diff(diff):
        if sign != "+" or not f or PurePosixPath(f).suffix.lower() not in CODE_EXTS or _comment_line(text):
            continue
        quote = text.strip()[:300]

        def add(rule: str, detail: str) -> None:
            out.append(ReviewFinding(kind="security", file=f, line=line, quote=quote, detail=detail, blocking=True,
                                     source="static", rule=rule))
        for m in _URL.finditer(text):
            host = _host(m.group(1))
            if host and not _host_declared(host, declared):
                add("outbound_http", f"outbound connection target {host!r} is not a declared host")
                break
        if _EXEC.search(text):
            add("dynamic_exec", "dynamic code execution (eval/exec/Function/script engine)")
        if _SHELL.search(text):
            add("shell_spawn", "spawns a process or shell")
        for m in _ENV_READ.finditer(text):
            name = m.group(1) or m.group(2) or ""
            if _secret_env_name(name):
                add("env_secret", f"reads credential-looking environment variable {name}")
                break
    return out


# ------------------------------------------------------------------ model review
def load_prompt() -> tuple[str, str, str]:
    from .codegen import load_prompt as _lp
    return _lp(PROMPT_FILE)


def validate_findings(raw, diff: str, known_ids: set[str]) -> tuple[list[ReviewFinding], int, int]:
    """(kept findings, dropped_unverified, invalid) from the model reply object."""
    items = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], 0, 1
    files = set(diff_files(diff))
    kept, dropped, invalid = [], 0, 0
    for it in items[:MAX_FINDINGS]:
        if not isinstance(it, dict):
            invalid += 1
            continue
        try:
            f = ReviewFinding.model_validate({k: it.get(k) for k in ("kind", "item_ids", "file", "line", "quote",
                                                                    "detail", "blocking") if it.get(k) is not None})
        except Exception:  # noqa: BLE001
            invalid += 1
            continue
        if not quote_in_diff(f.quote, diff) or (f.file and files and f.file not in files):
            dropped += 1
            continue
        upd = {"item_ids": [i for i in f.item_ids if i in known_ids], "detail": f.detail[:1000],
               "quote": f.quote.strip()[:300]}
        if f.kind in NEVER_BLOCKING:
            upd["blocking"] = False
        kept.append(f.model_copy(update=upd))
    return kept, dropped, invalid


def review(llm, *, slice_id: str, iteration: int, spec_text: str, diff: str, known_ids: set[str],
           declared_hosts: set[str] | None = None) -> ReviewResult:
    """Static checks + one fresh model call. A model failure is recorded (`model_error`), not raised,
    except BudgetExceeded which stops the run."""
    from ..analyze.module_analyzer import render
    from ..ingest.secrets import redact_string
    from ..llm.base import BudgetExceeded, parse_json
    from .codegen import cap_text
    from .state import now_iso
    diff = redact_string(diff)
    shown, truncated = cap_text(diff, MAX_DIFF_TOKENS)
    version, system, template = load_prompt()
    res = ReviewResult(slice_id=slice_id, iteration=iteration, prompt_version=version, created=now_iso(),
                       diff_files=diff_files(diff), diff_truncated=truncated)
    res.findings.extend(static_checks(diff, declared_hosts))
    if llm is None:
        return res
    prompt = render(template, SLICE_ID=slice_id, SPEC=spec_text, DIFF=shown or "(empty diff)")
    try:
        resp = llm.complete(task="code_review", system=system, prompt=prompt, json_mode=True,
                            prompt_version=version)
    except BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001
        res.model_error = f"{type(e).__name__}: {str(e)[:300]}"
        return res
    try:
        raw = parse_json(resp.text)
    except (ValueError, json.JSONDecodeError):
        res.model_error = "review reply is not JSON"
        return res
    kept, dropped, invalid = validate_findings(raw, shown, known_ids)
    res.findings.extend(kept)
    res.dropped_unverified = dropped
    res.invalid_findings = invalid
    return res
