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
  reviewer plus static deny checks, codemod adapters (OpenRewrite for Maven, django-upgrade + pyupgrade; jscodeshift
  stub), per-run and per-slice budget caps, resume after a crash.
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

### Known gaps
- Platform: no certification or audit of any kind. Stack and images not yet built or run end to end; no TLS ingress;
  worker is long-lived and holds database credentials; no billing by design. Repository Actions runs end in
  `startup_failure`, so no CI job (including `eval-phase2`) is verified.
- Phase 2 has not run against a real Docker daemon or a real model. Live Docker tests always skipped on the authoring
  machine; fixtures never built in Docker; images (runner, router, proxy, codemods) are tags, not digests.
- Phase 2 eval baselines are hand-written reference runs, not agent output. No nightly real-model run.
- gRPC and message characterization cases return `not_implemented`. OpenRewrite supports Maven only; jscodeshift is a
  stub; codemod tool versions are pinned but never run. Mirror resolution of new dependencies is a hook only; no
  lockfile hash checks or SBOM per run.
- The composite with `sqlite` or `none` DB engines gives each service a private database (not a shared DB).
- Reviewer precision is always n/a (nothing records accepted review findings). No Cut-over tab in the review UI.
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
