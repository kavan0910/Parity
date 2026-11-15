"""Phase 2 CLI commands, attached to the existing `parity` click group by ONE line in cli.py:

    from .migrate.cli import register as _register_phase2; _register_phase2(main)

REGISTRATION PATTERN (so parallel work never edits a shared line):

* `register(main)` creates the shared click groups once and passes them to every submodule listed in
  SUBMODULES that exists. Groups: `contract`, `target`, `tests`, `harness`, `config` (sub-commands), plus
  `main` itself for top-level verbs (`plan`, `generate`, `equivalence`, `approve-slice`, `cutover`, `metrics`).
* A submodule (e.g. migrate/fit.py) adds its commands by defining

      def register(groups: "Groups") -> None:
          @groups.target.command("fit")
          @click.argument("project", type=click.Path(exists=True, path_type=Path))
          def fit_cmd(project): ...

  Keep heavy imports inside the command functions: every module in SUBMODULES is imported at CLI start-up.
* SUBMODULES already lists the planned module names (docs/phase2-plan.md section 12). A module that does not
  exist yet is skipped; an ImportError raised INSIDE an existing module is not swallowed.
* Commands that write Phase 2 state call `migrate.contract.require_contract(project, ...)` first (entry gate).
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import click

# planned Phase 2 modules that may contribute commands, in registration order
SUBMODULES: tuple[str, ...] = (
    "target", "fit", "planner", "harness", "testgen", "recorder", "slice_runner", "codegen", "equivalence",
    "approvals", "configmap", "schemacompat", "cutover", "metrics",
)
GROUP_NAMES: tuple[str, ...] = ("contract", "target", "tests", "harness", "config")


@dataclass
class Groups:
    main: click.Group
    contract: click.Group
    target: click.Group
    tests: click.Group
    harness: click.Group
    config: click.Group
    registered: list[str] = field(default_factory=list)   # submodules that registered commands

    def __getitem__(self, name: str) -> click.Group:
        return getattr(self, name)


_HELP = {
    "contract": "Phase 2 entry gate: check the approved migration contract.",
    "target": "Target specification and target-fit analysis.",
    "tests": "Characterization tests: generate, record, coverage.",
    "harness": "Sandbox harness for the legacy and new services.",
    "config": "Configuration mapping to the target.",
}


def _make_groups(main: click.Group) -> Groups:
    made = {}
    for name in GROUP_NAMES:
        existing = main.commands.get(name)
        if isinstance(existing, click.Group):
            made[name] = existing
            continue
        if existing is not None:
            raise RuntimeError(f"`{name}` already exists as a command; cannot create the Phase 2 group")
        grp = click.Group(name=name, help=_HELP[name])
        main.add_command(grp)
        made[name] = grp
    return Groups(main=main, **made)


def register(main: click.Group, submodules: tuple[str, ...] = SUBMODULES) -> Groups:
    groups = _make_groups(main)
    _register_core(groups)
    for name in submodules:
        full = f"{__package__}.{name}"
        try:
            mod = importlib.import_module(full)
        except ModuleNotFoundError as e:
            if e.name == full:
                continue
            raise
        fn = getattr(mod, "register", None)
        if callable(fn):
            fn(groups)
            groups.registered.append(name)
    return groups


# ------------------------------------------------------------------ M0 commands
def _project(path: Path):
    from ..project import Project
    try:
        return Project.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _register_core(groups: Groups) -> None:
    @groups.contract.command("check")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--pin-contract", "pin", is_flag=True,
                  help="Migrate from the approved snapshot even if the graph was re-analysed later (warns).")
    @click.option("--accept-unmapped", is_flag=True,
                  help="Accept unmapped inventory items in the approved coverage (logged in the audit).")
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def contract_check(project: Path, pin: bool, accept_unmapped: bool, fmt: str) -> None:
        """Check docs/migration_contract.json against the project (exit 1 on any error)."""
        from .contract import check_contract
        chk = check_contract(_project(project), pin=pin, accept_unmapped=accept_unmapped)
        if fmt == "json":
            click.echo(chk.model_dump_json(indent=2))
        else:
            click.echo(f"contract: {'OK' if chk.ok else 'FAILED'}"
                       + (f" (graph v{chk.graph_version})" if chk.graph_version is not None else ""))
            for e in chk.errors:
                click.echo(f"  error: {e}")
            for w in chk.warnings:
                click.echo(f"  warning: {w}")
            for b in chk.blockers:
                click.echo(f"  blocker: {b.finding_id} -> {', '.join(b.item_ids) or '(no items)'}")
        if not chk.ok:
            raise SystemExit(1)

    @groups.contract.command("schema")
    @click.option("--out", type=click.Path(path_type=Path), default=None, help="Write the schema to this file.")
    def contract_schema(out: Path | None) -> None:
        """Print (or write) the JSON Schema of the migration contract."""
        from .contract import contract_json_schema
        schema = contract_json_schema(out)
        if out is None:
            click.echo(json.dumps(schema, indent=2))
        else:
            click.echo(f"schema written: {out}")

    @groups.main.command("doctor")
    @click.argument("project", type=click.Path(exists=True, path_type=Path))
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def doctor(project: Path, fmt: str) -> None:
        """Check Phase 2 prerequisites: contract, exec sandbox, git, remaining budget (exit 1 if any fails)."""
        checks = run_doctor(_project(project))
        if fmt == "json":
            click.echo(json.dumps(checks, indent=2))
        else:
            for c in checks:
                click.echo(f"{c['status'].upper():5} {c['name']}: {c['detail']}")
        if any(c["status"] == "fail" for c in checks):
            raise SystemExit(1)


def _git_version() -> tuple[bool, str]:
    import shutil
    import subprocess
    exe = shutil.which("git")
    if exe is None:
        return False, "git not found on PATH (target_code/ is a git repository)"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git not runnable: {type(e).__name__}"
    return (r.returncode == 0, (r.stdout or r.stderr).strip()[:200])


def run_doctor(project) -> list[dict]:
    """[{name, status: ok|warn|fail, detail}] for contract, exec backend, git and budget."""
    from . import exec as _exec
    from .contract import check_contract
    from .ledger import project_budget_usd, remaining, spent

    out: list[dict] = []
    chk = check_contract(project)
    detail = "OK" if chk.ok else "; ".join(chk.errors[:3])
    if chk.ok and chk.warnings:
        detail += f" ({len(chk.warnings)} warning(s); run `contract check`)"
    out.append({"name": "contract", "status": "ok" if chk.ok else "fail", "detail": detail})

    backend = _exec.get_backend(project)
    ok, reason = backend.available()
    out.append({"name": "exec sandbox", "status": "ok" if ok else "fail",
                "detail": f"{backend.name}: {'available' if ok else reason}"})

    gok, gdetail = _git_version()
    out.append({"name": "git", "status": "ok" if gok else "fail", "detail": gdetail})

    budget = project_budget_usd(project)
    led = spent(project)
    if budget is None:
        out.append({"name": "budget", "status": "warn",
                    "detail": f"no project budget (target/target.yaml budget_usd); spent so far ${led.usd:.4f}"})
    else:
        rem = remaining(project, budget)
        out.append({"name": "budget", "status": "ok" if rem > 0 else "fail",
                    "detail": f"${rem:.4f} remaining of ${budget:.4f} (spent ${led.usd:.4f})"})
    return out
