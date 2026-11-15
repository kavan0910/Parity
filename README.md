# migration-agent

**Migrate a legacy app to the newest version of its stack, or to a different technology, and prove that the new app
behaves like the old one.**

> **The MVP.** Point it at an existing legacy application, choose the target, and get a migrated application plus
> evidence that it behaves like the original.
>
> * **In:** your legacy app (a source folder, an archive or a git URL). It is copied; your repository is never touched.
> * **Choose:** the target, a newer version of the same stack or a different technology. The target-fit tables cover
>   Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5, Express to Fastify and JavaScript to Go.
> * **Out:** the migrated app in its own folder, an equivalence report against the running legacy app, and a cut-over
>   plan you approve. Nothing is deployed for you.

The migration has five steps. Understanding the legacy app is step 1, not the goal: you cannot migrate safely what
you cannot describe, and every later step points back to that approved map.

| Step | What happens | Phase | Where it runs today |
|---|---|---|---|
| 1. Understand | Read the legacy app; every claim cites a file and line | 1 | CLI and the hosted web app |
| 2. Approve | Sign off the map; decide per quirk whether to keep or fix it | 1 | CLI |
| 3. Migrate | Generate the new app slice by slice in a Docker sandbox | 2 | CLI (beta, needs Docker) |
| 4. Prove | Compare the new app with the running legacy app, case by case | 2 | CLI (beta) |
| 5. Cut over | Route traffic slice by slice, with a plan you approve | 2 | CLI (beta) |

**Status:** beta. Every step is built and tested; the sandbox has run for real on three sample apps; no migration stage
has run with a real model yet, so the quality of model-written code is unmeasured (see Status below).

## How it works

**Phase 1 (steps 1 and 2): understand, verify, approve.** It ingests a codebase, extracts static facts, and uses an LLM to build an
**evidence-backed knowledge graph and understanding document** (every claim cites file, lines and a verbatim
quote). A **blind Verifier** compares the document with your existing documentation. Humans resolve the findings
(targeted re-analysis, changelog), decide per quirk whether to preserve or fix it, and **approve** the result. The
approval writes `docs/migration_contract.json`, the input of Phase 2.

**Phase 2 (steps 3 to 5): migrate and prove equivalence.** Starting from the approved contract it:

1. checks the target fit (`target init`, `target fit`): library and API risks from mapping tables plus verified
   model findings;
2. plans ordered migration slices from the approved graph (`plan`);
3. generates characterization tests and **records the expected outputs from the running legacy service** in a
   sandbox, twice, with noise masks (`tests generate`, `tests record`);
4. runs a generation loop per slice: model edits, validation, build and tests in the sandbox, a fresh review
   (`generate`);
5. compares legacy and new case by case (`equivalence`) and feeds regressions back into the loop;
6. asks a human to approve each slice (`approve-slice`);
7. in strangler mode, routes approved slices to the new service through a sandbox router (`harness flip`) and re-runs
   the suite against the composite;
8. maps config and checks the schema (`config map`, `config schema-check`), then gates the final cut-over
   (`cutover --approve`).

Safety properties:

* Phase 1 never executes or modifies the analysed code; model access is limited to path-jailed read-only tools.
* Phase 2 builds and runs code only inside disposable Docker containers: no network in tests, mirror-only builds,
  no secrets, non-root, read-only rootfs, images never pulled. The original unredacted source (`legacy_src/`) is
  mounted only into those containers; the model keeps seeing only the redacted copy.
* Secrets are withheld or redacted at ingest. Generated code and config are scanned for secrets before they are
  written.
* One project folder holds all state; cost is bounded with `--max-usd` and a project budget, cached and resumable.

## Status

* **Phase 1** is implemented and has run on three small open-source repos ([docs/phase0-results.md](docs/phase0-results.md)).
  Its accuracy on large real repositories is **unmeasured**: nothing has been hand-scored yet.
* **Phase 2** is implemented (milestones M0 to M7) and covered by offline unit and integration tests: the full suite
  is **1195 passed, 2 skipped** (`.venv/bin/python -m pytest -q`; the 2 skips are the opt-in live end-to-end
  test and a release test that needs the `build` package). It has **run on a real Docker daemon** but **never with a real model**:
  the sandbox built and started all three legacy fixtures; for express-crud it also recorded 24 cases from the
  running service, ran an identity port as the new service through equivalence, fuzzing and the strangler router (and
  caught a seeded regression); and three codemods rewrote real code. The first live runs found and fixed seven bugs
  that every offline test had missed (see [CHANGELOG.md](CHANGELOG.md)). Repeat it with `MA_LIVE_E2E=1 .venv/bin/python -m pytest -q tests/test_p2_live_e2e.py`.
* The Phase 2 eval baselines (`eval/targets/baseline.json`) score **hand-written reference runs**, not agent output.
  They test the scorer and the regression gate, not migration quality.
* Known gaps (full list with reasons: [docs/remaining-work.md](docs/remaining-work.md)):
  * No Phase 2 stage has run with a real model, so the quality of generated code is unmeasured.
  * gRPC and message characterization cases return `not_implemented`.
  * gVisor and OpenRewrite through Gradle have not run; images are referenced by tag (each run records its digest).
  * Lockfile hashes and an SBOM are recorded per build, but a package whose hash differs is not refused.
  * In the composite (strangler) harness, in-memory `sqlite` or `none` databases are private per service.
  * Two eval pairs from the plan (`django-lite__fastapi`, `express-crud__go-chi`) are not built.
* Phase 2 is CLI/self-hosted only. The hosted platform below runs Phase 1 stages only.
* CI runs of this repository have so far ended in `startup_failure`; no CI job is verified.

Details: [docs/phase2-plan.md](docs/phase2-plan.md) ("Implementation status"), [docs/remaining-work.md](docs/remaining-work.md) (what is left), [CHANGELOG.md](CHANGELOG.md).

## Quick start: Phase 1

```bash
pip install ".[llm]"
cp .env.example .env            # add ANTHROPIC_API_KEY or GEMINI_API_KEY
migration-agent init ./my-service
migration-agent ingest  my-service.migration
migration-agent extract my-service.migration
migration-agent analyze my-service.migration --dry-run         # plan + cost estimate
migration-agent analyze my-service.migration --max-usd 5
migration-agent verify  my-service.migration                   # needs docs in my-service.migration/user_docs/
migration-agent review  my-service.migration                   # resolve findings, decide quirks
migration-agent status  my-service.migration                   # blockers and warnings
migration-agent approve my-service.migration --by "Jane Doe"   # writes docs/migration_contract.json
```
No key yet? `migration-agent analyze my-service.migration --offline` runs the pipeline with a stand-in model.

## Quick start: Phase 2

Needs Docker, the images named in your harness present locally (Phase 2 never pulls), and optionally a package
mirror (`harness/mirror.json` or `MA_MIRROR_HOSTS`). See [user guide section 9](docs/user-guide.md).

```bash
P=my-service.migration
migration-agent contract check $P && migration-agent doctor $P
migration-agent target init $P --runtime 21 --framework spring-boot@3.3 --build maven --budget-usd 25
migration-agent target fit  $P --max-usd 2
migration-agent plan        $P
migration-agent harness import-source $P --from-original       # original source, checked against source_hash
migration-agent harness build $P && migration-agent harness up $P
migration-agent tests generate $P && migration-agent tests record $P --harness
migration-agent tests coverage $P --fail-on-gaps
migration-agent generate    $P --all --max-usd 20
migration-agent equivalence $P --all
migration-agent review      $P                                 # Plan and Slice tabs: diffs, blockers, approve
migration-agent approve-slice $P SLICE-0001 --by "Jane Doe"
migration-agent config map  $P && migration-agent config schema-check $P
migration-agent cutover     $P                                 # readiness + cutover/plan.md
migration-agent cutover     $P --approve --by "Jane Doe"
migration-agent metrics     $P
```
For strangler projects add `harness build|up $P --composite`, `harness flip $P SLICE-NNNN` and
`equivalence composite $P` between slice approval and cut-over. `target fit`, `plan`, `tests generate`, `generate`
and `equivalence` accept `--offline` to run without a model: deterministic layers only, and `generate` writes
placeholder edits, so that is for trying the flow, not for a real migration.

## Hosted platform (in development, 0.3.0)

Beyond the CLI, the repo is growing a multi-tenant web product: a FastAPI backend and worker, a Next.js UI and a
docker-compose stack with a hardened, egress-restricted worker. Nothing here is a released or audited service, and there
is no billing. It runs Phase 1 only; Phase 2 stays on the CLI (decision P2-Q13). Design:
[docs/product-architecture.md](docs/product-architecture.md); contract between the parts:
[docs/platform-contract.md](docs/platform-contract.md).

Run the stack (requires Docker and the backend and web directories):

```bash
cp deploy/.env.example deploy/.env      # set POSTGRES_PASSWORD, MA_OWNER_PASSWORD, MA_APP_PASSWORD, JWT_SECRET and a model key
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
deploy/smoke.sh                         # health, signup, sample project, key endpoints
```
Web on http://localhost:3000, API on http://localhost:8000. See the [runbook](docs/operations-runbook.md).

## Repo map

| Path | What |
|---|---|
| `src/migration_agent/` | Engine and CLI (pip-installable) |
| `src/migration_agent/migrate/` | Phase 2: contract gate, target fit, planner, tests, exec sandbox, harness, generation loop, equivalence, approvals, config/schema, router, cut-over, metrics |
| `src/migration_agent/service.py` | Library API (`run_stage`) for the backend and other hosts |
| `eval/repos/` | Hand-labelled fixture repos (Phase 1 scoring) with runnable `harness/` folders |
| `eval/targets/` | Phase 2 target pairs: target spec, hidden reference suite, quirks, canaries, recorded reference run, baseline |
| `scripts/eval_phase2.py` | Offline Phase 2 eval gate (CI job `eval-phase2`) |
| `backend/` | API and worker (FastAPI) |
| `web/` | Web UI (Next.js) |
| `deploy/` | Compose stack, Dockerfiles, `.env.example`, `smoke.sh` |
| `docker/` | Hardened single-run engine image and compose profiles |
| `docs/security/`, `SECURITY.md` | Data handling, threat model, SOC 2 readiness (not certified), disclosure policy |
| `docs/operations-runbook.md` | Deploy, backup/restore, rotation, on-call |

## Documentation

[docs/](docs/README.md): [user guide](docs/user-guide.md), [architecture](docs/architecture.md),
[Phase 2 architecture](docs/phase2-architecture.md), [Phase 2 plan](docs/phase2-plan.md),
[API reference](docs/api-reference.md), [threat model](docs/threat-model.md),
[deployment guide](docs/deployment.md). Eval: [eval/README.md](eval/README.md). Release history:
[CHANGELOG.md](CHANGELOG.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,llm]"
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/eval_phase2.py      # Phase 2 eval gate (offline)
```
Docker: see `docker/` and the deployment guide. Releases: `python scripts/release.py vX.Y.Z`.
