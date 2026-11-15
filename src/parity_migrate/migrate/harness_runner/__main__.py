"""In-sandbox entry point: run case files against a base URL and write JSON results.

    python -m parity_migrate.migrate.harness_runner --base-url http://legacy:8080 \
        --cases /work/cases --out /work/reports/results.json

`--cases` is a folder searched recursively for CT-*.json (or a single file). Exit code 0 when every case
passed, 1 when any failed or errored, 2 on usage errors. Intended to run inside an exec job on the harness
network, so the host CLI only needs the ExecBackend; the host-side commands (`tests record|run`) call the
same functions in-process.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..models import CharacterizationCase
from .models import RunnerConfig
from .runner import HostNotAllowed, run_cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harness_runner")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--cases", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    files = [a.cases] if a.cases.is_file() else sorted(a.cases.rglob("CT-*.json"))
    cases = [CharacterizationCase.model_validate_json(f.read_text()) for f in files]
    try:
        results = run_cases(cases, RunnerConfig(base_url=a.base_url, timeout_s=a.timeout))
    except HostNotAllowed as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    payload = json.dumps([r.model_dump(mode="json") for r in results], indent=2)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(payload + "\n")
    else:
        print(payload)
    return 0 if all(r.outcome == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
