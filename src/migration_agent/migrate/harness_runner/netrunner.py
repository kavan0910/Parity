"""In-network runner script: STANDARD LIBRARY ONLY (runs in the pinned `python:3.12.7-alpine3.20` image, no
pydantic, no migration_agent package). `migrate.harness.run_cases_in_network` copies this file and wire.py into a
run workspace that is mounted read-only at /runner, writes the cases to /cases/cases.json (read-only mount) and
runs, on the role's internal sandbox network:

    python /runner/netrunner.py --base-url http://legacy:3000 --cases /cases/cases.json \
        --out /reports/results.json [--config /cases/config.json]

cases.json: [CharacterizationCase as JSON, ...]. config.json: wire config (timeout_s, job_endpoint,
follow_redirects, max_redirects, max_body_bytes, extra_headers); base_url always comes from --base-url.
results.json: {"schema_version": 1, "base_url": ..., "results": [wire.observe_raw(...) per case, in order]}.
Observations only: comparison with expectations happens on the host (harness_runner.runner.result_from_raw).
Exit 0 when every case produced an observation, 1 when any case errored, 2 on usage errors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:                                   # inside the package (tests, in-process use)
    from . import wire                 # type: ignore[import-not-found]
except ImportError:                    # staged next to wire.py in /runner (container)
    import wire                        # type: ignore[no-redef]

MAX_CASES = 10_000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="netrunner")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)
    try:
        wire.parse_base_url(a.base_url)
        with open(a.cases, encoding="utf-8") as f:
            cases = json.load(f)
        cfg = {}
        if a.config:
            with open(a.config, encoding="utf-8") as f:
                cfg = json.load(f)
        if not isinstance(cases, list) or len(cases) > MAX_CASES or not isinstance(cfg, dict):
            raise ValueError("cases must be a JSON list and config a JSON object")
    except (OSError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    cfg = {**cfg, "base_url": a.base_url}
    results = [wire.observe_raw(c if isinstance(c, dict) else {}, cfg) for c in cases]
    out_dir = os.path.dirname(a.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "base_url": a.base_url, "results": results}, f, default=str)
    os.replace(tmp, a.out)
    errors = sum(r["outcome"] == "error" for r in results)
    print(f"{len(results)} case(s), {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
