# Phase 2 eval target pairs

One folder per `<repo>__<target>` (docs/phase2-plan.md section 11). Scored by
`parity_migrate.evalharness.score_migration` and gated in CI by `scripts/eval_phase2.py` (offline: recorded
artifacts only, no model, no network, no Docker).

| pair | kind | quirks (preserve) | suspected bug (fix) |
|------|------|-------------------|---------------------|
| `spring-lite__java21-boot3` | version upgrade | Q-1 book JSON has only `copiesAvailable`; Q-3 rule violations answer 500 | Q-2 successful checkout answers 500 |
| `django-lite__django5` | version upgrade | Q-1 duplicate check is case-sensitive | Q-2 unknown workshop answers 500 |
| `express-crud__fastify-ts` | framework move | Q-1 unrounded float discount | Q-2 deleting an unknown product answers 204 |

## Pair folder

| file | content |
|------|---------|
| `target.yaml` | `TargetSpec` |
| `expected_cases/*.json` | hidden reference suite (`CharacterizationCase`); covers every labelled rule of `eval/repos/<repo>/expected.json` by name (`covers: ["RULE-..."]`); fix cases carry an approved specified expectation and the legacy observation |
| `fixtures/` | DB fixtures referenced by `setup.db_fixture` |
| `quirks.json` | `{quirks: [{id, kind: quirk\|suspected_bug, title, evidence, expected_decision, case_ids, expected_behaviour}]}` |
| `canaries.json` | `{canaries: [{id, aimed_at, injection, needles, plant_in}]}`: injection text a live run plants into `plant_in`; `needles` must never appear in generated code |
| `recorded/` | recorded run scored in CI (layout below) |

`validate_pair(dir)` checks all of it (models, every labelled rule covered, quirks point at cases with the matching
decision, at least one quirk and one suspected bug, canaries carry needles).

## Run folder (recorded artifacts)

| file | meaning |
|------|---------|
| `run.json` | `{kind, build_ok, cost_usd, wall_clock_s, decisions: {quirk id: preserve\|fix}, rule_map: {graph id: rule name}}` |
| `hidden_results.json` | `{results: [{case_id, outcome}]}` of the hidden suite against the generated service |
| `agent_cases/*.json` | the agent's own suite (rule coverage) |
| `equivalence/*.json` | `EquivalenceReport`s |
| `egress.json` | `{connections: [{phase, dest}]}` observed by the sandbox; `runs/RUN-*/meta.json` test jobs must have network `none` |
| `target_code/` | generated code (canary and secret scan) |

A Phase 2 project folder (has `project.json`) can be scored directly: `--run <pair>=<project>`; cases, equivalence
reports, contract decisions (matched to quirks by evidence overlap), build results and cost come from the project,
`hidden_results.json` must be placed in the project root.

## Scores and gate

build success, hidden-suite pass rate, agent-suite rule coverage, equivalence %, preserve/fix decision correctness
(decision matches and the quirk's hidden cases pass), canary hits, test-phase outbound connections, secrets in
generated code (ingest secret patterns), cost, wall-clock. `compare_migration(baseline, new)` fails on any drop of a
rate and on any failing gate (build, canaries, egress, secrets); cost and wall-clock are reported, not gated.

```
PYTHONPATH=src python scripts/eval_phase2.py                    # gate against eval/targets/baseline.json
PYTHONPATH=src python scripts/eval_phase2.py --update-baseline  # after an intended change
```

## Honest status

The `recorded/` runs are hand-written reference migrations with hand-written results: they exercise the scorer and
the regression gate, they are not agent output. The canaries are not planted in `eval/repos/` (that would change the
Phase 1 graphs and eval scores); a live runner must overlay them. No nightly real-model run exists yet.

The hidden expectations were derived from the legacy source and, when finally checked against the live legacy service
(2026-11-15), **do not agree with it**: 0 of the 9 `express-crud__fastify-ts` cases pass. They assume a pre-seeded
database and a case order, some are written to be unauthenticated, and the runner did not send the synthetic API token
at all until `request_headers` was added (a case can now drop an injected header with an empty value). They have not
been reconciled. The `django-lite__fastapi` and `express-crud__go-chi` pairs from the plan are not built.
