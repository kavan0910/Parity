"""Offline Phase 2 eval gate (docs/phase2-plan.md section 11): score every eval/targets/<repo>__<target>/ pair from
its recorded run and compare with eval/targets/baseline.json (fails on any drop of hidden-suite pass rate, rule
coverage, equivalence % or fix-decision correctness, and on any canary hit, test-phase outbound connection, secret in
generated code or failed build). No model, no network, no Docker.

usage: PYTHONPATH=src python scripts/eval_phase2.py [--targets DIR] [--baseline FILE] [--run PAIR=DIR ...]
                                                    [--update-baseline] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from migration_agent.evalharness import EVAL_TARGETS, MigrationReport, run_phase2_offline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--targets", type=Path, default=EVAL_TARGETS)
    ap.add_argument("--baseline", type=Path, default=None, help="default: <targets>/baseline.json")
    ap.add_argument("--run", action="append", default=[], metavar="PAIR=DIR",
                    help="score DIR (recorded artifacts or a Phase 2 project folder) instead of <pair>/recorded")
    ap.add_argument("--update-baseline", action="store_true", help="write the current scores as the new baseline")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    runs = {}
    for item in a.run:
        pair, sep, d = item.partition("=")
        if not sep:
            ap.error(f"--run needs PAIR=DIR, got {item!r}")
        runs[pair] = Path(d)
    baseline = a.baseline or a.targets / "baseline.json"
    res = run_phase2_offline(a.targets, baseline, runs)
    if a.update_baseline:
        if res["invalid"]:
            print("refusing to update the baseline: invalid pairs", file=sys.stderr)
            return 2
        baseline.write_text(json.dumps({"schema_version": 1, "pairs": res["reports"]}, indent=2, sort_keys=True)
                            + "\n")
        print(f"baseline written: {baseline}")
        return 0
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for name, d in sorted(res["reports"].items()):
            r = MigrationReport.from_dict(d)
            eq = "n/a" if r.equivalence_pct is None else f"{r.equivalence_pct:.2f}%"
            print(f"{name:32} build={'ok' if r.build_ok else 'FAIL'} hidden={r.hidden_passed}/{r.hidden_total} "
                  f"rules={r.rules_covered}/{r.rules_total} equiv={eq} fix={r.fix_correct}/{r.fix_total} "
                  f"canaries={len(r.canary_hits)} egress={len(r.outbound_connections)} secrets={len(r.secret_hits)} "
                  f"cost=${r.cost_usd:.4f} wall={r.wall_clock_s:.1f}s ({r.run_kind or 'run'})")
        for name, errs in sorted(res["invalid"].items()):
            for e in errs:
                print(f"INVALID {name}: {e}")
        for name, probs in sorted(res["problems"].items()):
            for p in probs:
                print(f"FAIL {name}: {p}")
        print("phase 2 eval: " + ("OK" if res["ok"] else "FAILED"))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
