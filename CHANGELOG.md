# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).
The version lives in `pyproject.toml` only.

## [Unreleased] - 0.3.0

### Added: hosted platform scaffolding
- `deploy/` compose stack (postgres, API, hardened worker behind an allowlisting egress proxy, web, optional MinIO),
  `Dockerfile.backend`, `Dockerfile.web`, `.env.example`, `smoke.sh` (see `docs/platform-contract.md`).
- CI workflows for backend, web and security (pip-audit, npm audit, Trivy, gitleaks, compose validation).
- `docs/security/` (data handling and retention draft, subprocessor template, incident response outline, secure SDLC,
  hosted threat model, SOC 2 readiness checklist, customer security overview draft), `SECURITY.md`,
  `docs/operations-runbook.md`.

### Added: Phase 1 exit gate (approval)
- `migration-agent decide PROJECT ITEM_ID (--preserve|--fix) [--note]`: migration decision per quirk / suspected bug
  in `feedback/decisions.json` (default preserve); the document is re-rendered offline and section 12 shows
  `Migration decision: ...`.
- `deferred` resolution status for Verifier findings (`resolve --status deferred`, review UI): no re-analysis, does
  not block approval, listed in the contract.
- `migration-agent status PROJECT` (readiness blockers and warnings, approval state) and
  `migration-agent approve PROJECT --by NAME [--note]`: appends to `feedback/approvals.json`, writes the read-only
  `docs/understanding.vN.approved.md` and `docs/migration_contract.json` (`MigrationContract`). The approval becomes
  stale when the graph snapshot or the document changes.
- `MigrationContract` schema 2 adds `source_hash` (from `ingest_report.json`; empty plus a warning when missing).
- Review server: `/api/state` includes readiness, approval and decisions; token-protected `POST /api/decisions` and
  `POST /api/approve`; Approval tab.
- `analyze` persists gap-fill coverage exclusions in `facts/coverage_exclusions.json`; readiness, approval, the
  offline re-render and targeted re-analysis use them, so all compute the coverage `analyze` rendered.

### Added: Phase 2, migrate and prove equivalence (`src/migration_agent/migrate/`, M0 to M7)
- M0 foundation: shared models, ID allocator (`SLICE-`, `CT-`, `RISK-`, `EQ-`, `RUN-`), folder layout v2 (project
  schema 2, Phase 2 folders created lazily), slice state machine, cumulative cost ledger across sessions,
  `contract check PROJECT [--pin-contract] [--accept-unmapped]`, `contract schema`, `doctor PROJECT`.
- M1 exec sandbox: `DockerBackend` (fixed-argv `docker run`, `--network none` for tests, mirror-only builds through a
  per-run Squid proxy, cap-drop ALL, non-root, read-only rootfs, limits, images never pulled, gVisor when installed;
  `harness/mirror.json`, `MA_MIRROR_HOSTS`, `MA_EXEC_RUNTIME`), job and argv policy checks (`exec/policy.py`),
  `harness import-source|build|up|down|status`, runnable fixtures in `eval/repos/*/harness/`, red-team tests.
- M2 target fit: `target init|show|fit|resolve`, libmap tables (Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5,
  Express to Fastify, JS to Go), model risks kept only with verified evidence.
- M3 characterization tests: `tests generate|record|run|coverage|mark-untestable|approve-spec`, language-neutral
  runner (http and job cases; origin jail, JSON-semantic compare, masks), stub server, double recording with noise
  masks, runtime findings, in-network runs against the harness.
- M4 planner and generation loop: `plan` (ordered slices, every in-scope node in exactly one slice),
  `generate [--slice|--all] [--dry-run] [--from-comments] [--offline]` with validated edits (path jail, build-file
  allowlist, exact apply, secret scan, dependency allow/deny), `target_code/` as a hardened git repo, fresh-session
  reviewer plus static deny checks, codemod adapters (OpenRewrite for Maven and Gradle, django-upgrade + pyupgrade, lebab for
  JavaScript), per-run and per-slice budget caps, resume after a crash.
- M5 equivalence and approvals: `equivalence [run] (--slice|--all)`, `equivalence approve-mask`, optional triage with
  verified quotes, fix requests back into the loop, `approve-slice`, `reject-slice`,
  `slice comment|decide|accept-observed`, human-commit detection; review UI Target fit, Plan, Slice and Comments tabs
  and `/api/p2/*` routes.
- M6 config, data and strangler router: `config map|set|schema-check|schema-blockers` (secrets as references only),
  declared expand/contract schema changes on synthetic data, `harness build|up|down --new|--composite`, stdlib router,
  `harness flip|unflip|routes`, `equivalence composite`.
- M7 cut-over and eval: `cutover [--approve]` (readiness gate, deterministic `cutover/plan.md`, approval with hashes),
  `metrics`, `service.run_stage` stages `target_fit`, `plan`, `tests`, `generate`, `equivalence`, `cutover` plus
  `service.metrics()`, `evalharness.score_migration` / `compare_migration`, `eval/targets/` (three pairs),
  `scripts/eval_phase2.py` and the CI job `eval-phase2`.
- Docs: `docs/phase2-plan.md` (with implementation status), `docs/phase2-architecture.md`, user guide section 9,
  architecture, API reference, platform contract, threat model section 9 (T19 to T37), eval, deployment and runbook.

### Added: Phase 2 completion pass
- Review UI Cut-over tab: every readiness check, the approval and whether its hashes still match, and a token-protected
  approve action (`GET /api/p2/cutover`, `POST /api/p2/cutover-approve`).
- Reviewer precision: `slice review-finding PROJECT SLICE K:I --accept|--dismiss --by NAME` (and Accept/Dismiss buttons in
  the Slice tab, `POST /api/p2/review-mark`) store a human's verdict in `slices/SLICE-NNNN/review_marks.json`; `metrics`
  reports precision as accepted over marked.
- Latency comparison (`migrate/differential.py`): every equivalence run records each side's response time per case and a
  p50/p95 summary, and flags cases where the new service is more than x2 and 50 ms slower (advisory, in
  `equivalence/report.md` and as cut-over warnings).
- Differential fuzzing: `equivalence fuzz PROJECT (--slice|--all) [--max-variants N]` applies deterministic mutations
  (query values, JSON fields, body shape, unknown field or parameter) to recorded HTTP cases, asks legacy twice to mask its
  own noise, and reports where the new service answers differently (`equivalence/fuzz/SLICE-NNNN.json`; exit 1 on a
  high-severity divergence). Advisory: never blocks `approve-slice`.
- Supply chain (`migrate/supplychain.py`): every successful `harness build` writes `runs/RUN-NNNN/supply_chain.json` and
  `sbom.cdx.json` (CycloneDX 1.5) with lockfile sha256 hashes; warns about manifests with no lockfile, lockfiles the build
  generated instead of the source containing them, and unpinned dependencies; `cutover` lists them as warnings.
- Codemods: OpenRewrite for Gradle (init script, `gradle:8.10-jdk21`), a shared package cache mount for codemod jobs,
  and a real JavaScript adapter (lebab: `let`, `template`, `obj-shorthand`) replacing the jscodeshift stub
  (`JscodeshiftAdapter` remains as an alias). A crashed tool now fails the job instead of reading as "no changes".
- Authenticated and stateful services: a service may declare `request_headers` (values may use `{secret:NAME}` for a
  synthetic secret generated at `harness up`), which the in-network runner adds to every request; a case header with
  an empty value removes an injected one. `seed_requests` (HTTP seeds for in-memory databases) are now replayed after
  `harness up` for legacy, new and both composite services; a failing seed stops `up` and tears the harness down.
  `equivalence` and `equivalence fuzz` restart both harness services first (`--no-reset` keeps state), so both start
  from the same seeded data. Two ISO-8601 timestamps no more than 5 minutes apart compare equal everywhere.
- `MA_LIVE_E2E=1` live end-to-end test (`tests/test_p2_live_e2e.py`) and live tests for the mirror proxy and the
  workdir-masking guard.
- Docs: `docs/remaining-work.md` (what is built, verified and left, against every plan).

### Changed: landing pages
- Both landing pages (`landing/` and the web app's `/`) describe the whole product: understand and approve (Phase 1), then
  migrate with proof (Phase 2, a command-line beta), what runs where (the hosted app runs Phase 1 only), the sandbox
  trial and the change it missed, and that no migration stage has run with a real model.

### Fixed: found by the first runs on a real Docker daemon
All of these passed every offline test.
- The allowlisting build proxy (Squid) died on start (`FATAL: Cannot open '/dev/stdout'`), so every build through a package
  mirror failed with `EAI_AGAIN`. Its log tmpfs was not writable by Squid's user, its access log was unreadable (the egress
  audit file was empty), and its ICMP helper logged a `FATAL` on every start.
- The strangler router never started and neither did the schema probe: a tmpfs at the job's working directory (`/app`)
  hid the image's own script. Both use `/tmp` now, and the Docker backend refuses a job whose workdir tmpfs would mask a
  path in its command.
- Codemods in the sandbox: no writable `HOME`, no package cache, `/tmp` is `noexec` (tools are started through their
  interpreter), Maven ignores `HTTPS_PROXY` (proxy settings are generated), OpenRewrite resolves POMs with its own HTTP
  client (`~/.m2/settings.xml` and JVM proxy properties are provided).
- The spring-lite eval fixture did not compile (`Member.getId()` missing).
- Authenticated services recorded only `401`s: nothing sent the synthetic API token, and the documented `seed_requests`
  replay was never implemented. State skew between a used legacy service and a fresh new one, and creation timestamps of
  rows seeded at different moments, produced false regressions and false fuzz divergences; both are handled now.

### Verified on a real Docker daemon (Docker Desktop 29.9, linux/arm64, runc, no gVisor, no model)
- express-crud: legacy build and start, API seeds replayed with the synthetic token, 24 authenticated cases recorded
  twice, identity port as the new service, equivalence 24/24, 90 fuzz variants with no divergence, composite with the
  router (flip, 15 cases through the router, rollback), and a seeded status-code regression caught. A second seeded
  change (a price boundary) was not caught by the offline cases or the fuzzer. django-lite and spring-lite build and start; spring-lite recorded 6 cases.
- django-upgrade, pyupgrade, lebab and OpenRewrite (Maven) transformed real code in the sandbox.

### Known gaps
- Platform: no certification or audit of any kind. Stack and images not yet built or run end to end; no TLS ingress;
  worker is long-lived and holds database credentials; no billing by design. Repository Actions runs end in
  `startup_failure`, so no CI job (including `eval-phase2`) is verified.
- **No Phase 2 stage has run with a real model** (target fit, test generation, code generation, review, triage, slice
  naming); the live runs above used `--offline`, whose cases are shallow and miss boundary changes. The eval's hidden
  reference suite (`eval/targets/*/expected_cases`) disagrees with the live legacy service (0 of 9 express-crud cases
  pass: it assumes pre-seeded state and ordering) and has not been reconciled. Eval baselines are hand-written reference runs, not agent output; no
  nightly real-model run. The `django-lite__fastapi` and `express-crud__go-chi` eval pairs are not built.
- gRPC and message characterization cases return `not_implemented`. gVisor and OpenRewrite through Gradle have not run.
  Mirror resolution of new dependencies is a hook only: lockfile hashes and an SBOM are recorded, but a package whose hash
  differs is not refused.
- The composite with `sqlite` or `none` DB engines gives each service a private database (an in-memory database cannot be
  shared); use postgres or mysql to test a shared database.
- Images are referenced by tag; each run records the digest it used.
- Phase 2 is CLI/self-hosted only (P2-Q13); the hosted worker must not run it.
- Phase 0 accuracy on large real repositories is unmeasured.

## [0.2.0] - 2026-11-09

### Added
- Production Docker image (`docker/Dockerfile`): multi-stage, digest-pinnable base, non-root, read-only rootfs,
  OCI labels, project folder mount at `/project`.
- `MA_*` environment configuration (`migration_agent.config_env`): per-task models, custom endpoint, retries, budget, log level.
- Compose: `MA_*` pass-through, configurable model endpoint host for the egress proxy, `PROJECT_DIR` mount.
- Release tooling (`scripts/release.py`), CI workflow (tests, wheel check, image build, keyless cosign signing and SBOM on tags).
- Documentation: user guide, architecture, API reference, deployment guide.
- `migration_agent.__version__` from package metadata.
- `migration-agent --version`, `MA_*` config applied in the CLI, and `MA_LLM_API_BASE` honoured by the LLM client.
- Framework/schema/spec extractors, incremental runs and error recovery; review patches persist across analyze; stable edge IDs.

### Changed
- Package metadata (readme, classifiers); sdist now includes docs and docker files.

### Known gaps
- Image is built and smoke-tested locally (read-only rootfs, no network) but not yet digest-pinned, scanned or signed; CI does that on tag.

## [0.1.0]
- Phases A to C: ingest, extraction, analysis pipeline, Verifier, review server, resolutions, changelog.
