# Evaluation harness fixtures

`eval/repos/` holds hand-built reference repositories, each with a hand-labelled
`expected.json`. They are scored by `migration_agent.evalharness.score`.

| repo | files | stand-in for |
|------|-------|--------------|
| `express-crud` | 18 | small Express.js CRUD app (includes a prompt-injection fixture) |
| `spring-lite`  | 11 | scaled-down stand-in for the plan's ~100 file Spring repo |
| `django-lite`  | 8  | scaled-down stand-in for the plan's ~500 file Django repo |

The spring-lite and django-lite repos are deliberately small; they exercise the
same shapes (controllers/services/entities, views/models/urls) but not the scale.

## expected.json shape

Plain JSON, no dependency on the graph models. Nodes are
`{type, name, module, qualified_symbol}`; identity key is
`type|module|qualified_symbol`. Categories: `components`, `endpoints`,
`entities`, `rules` (rules also carry `text` and `evidence`), and `edges`
(`{type, source, target}` where source/target are identity keys).
`rules[].evidence` is `[{file, start_line, end_line}]`, verified against the repo.
`must_not_appear` (express-crud) lists strings that must not occur in any actual
node or rule text (prompt-injection canaries in `src/vendor/legacy-notes.js`).

An actual graph is `{"nodes": [...], "edges": [...]}`; nodes of type `component`,
`endpoint`, `entity`, `rule`. Edge endpoints may be identity keys or node `id`s.

## Gates

* evidence validity must be 100% (every cited file/line range exists in the repo)
* no `must_not_appear` string may appear in any actual node/edge text

`compare(old, new)` fails if any category F1 drops.

# Phase 2 evaluation

Phase 2 is scored on **target pairs** in `eval/targets/<repo>__<target>/` (details in
[targets/README.md](targets/README.md); plan: `docs/phase2-plan.md` section 11).

## Runnable fixtures

Each repo has `eval/repos/<repo>/harness/` (Dockerfile, `harness.yaml.in`, seed data) so the legacy service can be
built and started in the exec sandbox. Harness files that Phase 1 would otherwise analyse carry a `.in` suffix, so
adding them does not change the Phase 1 graphs or the scores above. These fixtures have not been built in Docker yet.

## Target pairs

| pair | kind | strategy |
|------|------|----------|
| `spring-lite__java21-boot3` | version upgrade (Java 17 / Boot 3.2 -> Java 21 / Boot 3.3) | in_place |
| `django-lite__django5` | version upgrade (Python 3.9 / Django 3.2 -> Python 3.12 / Django 5.0) | in_place |
| `express-crud__fastify-ts` | framework move (Express 4 JS -> Fastify 4 TS) | strangler |

Each pair has `target.yaml`, a hidden reference suite (`expected_cases/`, the agent never sees it) covering every
labelled rule, deliberate quirks and one suspected bug with the expected preserve/fix behaviour (`quirks.json`),
codegen canaries (`canaries.json`) and a recorded run (`recorded/`).

## Scoring (`migration_agent.evalharness.score_migration`)

Build success, hidden-suite pass rate on the generated service, agent-suite coverage of labelled rules,
equivalence %, preserve/fix decision correctness, canary hits in generated code, outbound connections in the test
phase, secrets in generated code, cost and wall-clock.

## Gate and baseline

```
PYTHONPATH=src python scripts/eval_phase2.py                    # compare with eval/targets/baseline.json
PYTHONPATH=src python scripts/eval_phase2.py --update-baseline  # after an intended change
PYTHONPATH=src python scripts/eval_phase2.py --run PAIR=DIR     # score a recorded-artifacts folder or a project folder
```

`compare_migration(baseline, new)` fails on any drop of a rate and on any failing gate (build, canaries, egress,
secrets); cost and wall-clock are reported, not gated. The CI job `eval-phase2` (`.github/workflows/ci.yml`) runs the
script offline: no model, no network, no Docker. Current result: all three pairs pass with 100% on every rate.

## Honest caveat

The baseline is not a measurement of the agent. The `recorded/` runs are hand-written reference migrations with
hand-written results (`run_kind: reference`). They exercise the scorer and the regression gate. The hidden
expectations were derived from the legacy source and have not been checked against a live legacy recording. The
canaries are not planted in `eval/repos/` (that would change the Phase 1 graphs); a live runner must overlay them.
No real-model run of Phase 2 exists yet, nightly or otherwise, and the CI runs of this repository have so far ended
in `startup_failure` (see `docs/operations-runbook.md` section 10).
