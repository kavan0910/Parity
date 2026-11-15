# spring-lite__java21-boot3

Phase 2 eval target pair (docs/phase2-plan.md section 11, `evalharness.score_migration`).

- `target.yaml`: the target spec handed to `parity target init/fit`.
- `expected_cases/`: the HIDDEN reference characterization suite (CharacterizationCase JSON). Never copy it into a
  project the agent works on. Every labelled rule of `eval/repos/spring-lite/expected.json` is covered (`covers`).
  Cases assume a freshly seeded legacy database per case (harness seed + `setup.db_fixture`).
- `quirks.json`: deliberate quirks and a suspected bug with the expected preserve/fix decision and the hidden
  cases that check the resulting behaviour.
- `canaries.json`: prompt-injection canaries aimed at code generation. `plant_in` names the legacy file a live run
  plants the `injection` text into (as a comment); `needles` must never appear in generated code.
- `recorded/`: a recorded run scored offline in CI (`scripts/eval_phase2.py`). It is a hand-written REFERENCE
  migration plus hand-written results, used to exercise the scorer and the regression gate; it is not agent output.

Status: the hidden expectations are hand-derived from the legacy source and have not yet been checked against a
live recording of the legacy service (M3 acceptance "hidden suite agrees with recorded expectations on legacy").
