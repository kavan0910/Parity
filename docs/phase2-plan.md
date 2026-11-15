# Phase 2 implementation plan: Migrate & Prove Equivalence

Status: v1, owner decisions recorded in section 14 (all recommendations accepted).
Grounding: `README.md`, `docs/architecture.md`, `docs/product-architecture.md`, `docs/phase0-results.md`,
`docs/threat-model.md`, `eval/README.md`, `CHANGELOG.md`, `.lavish/migration-agent-grill.html` (Q1-Q18), and the code
under `src/migration_agent/`. Code is cited as `path:line`. **[Gap]** marks something that does not exist yet or is
unproven. **[Rec]** is my recommendation.

## Implementation status

The plan below is the design as agreed before coding. The code now implements M0 to M7. Module map and public
functions: [phase2-architecture.md](phase2-architecture.md). User instructions: [user-guide.md](user-guide.md)
section 9.

"Tested offline" means unit and integration tests that run without Docker or a model: a scripted docker CLI, fake or
offline model clients, and local HTTP servers. "Verified live" means it ran on a real Docker daemon (Docker Desktop 29.9,
linux/arm64, runc, no gVisor) on the eval fixtures, with `--offline` and no model. The full suite is
1195 passed, 2 skipped (`.venv/bin/python -m pytest -q`). `MA_LIVE_E2E=1 .venv/bin/python -m pytest -q
tests/test_p2_live_e2e.py` repeats the express-crud live run (about 2 minutes).

| # | Modules | Tested offline | Verified live | Not verified yet |
|---|---|---|---|---|
| M0 | `migrate/contract.py`, `ledger.py`, `ids.py`, `paths.py`, `state.py`, `models.py`, `exec/base.py`, `exec/fake.py`, `cli.py` (`contract check|schema`, `doctor`) | every gate rule 1-7, ledger sums, state machine, id allocation, layout v2 | contract gate, `doctor` against a real daemon, source-hash check on `import-source` (it refused a fixture that had changed since it was analysed) | `doctor` does not probe gVisor or mirror reachability |
| M1 | `exec/docker.py`, `exec/policy.py`, `harness.py` (`import-source`, `build`, `up`, `down`, `status`), `eval/repos/*/harness/` | argv lockdown, network modes, mirror proxy config, limits, secret refusal, report copy-out (red-team tests in `tests/redteam/test_p2_exec_sandbox.py`) | all three fixtures build and start; the live canary, fork-bomb, disk-fill, read-only rootfs tests pass; the allowlisting proxy allows the mirror host, denies others (403) and leaves no direct route; egress log captured. Seven bugs found and fixed (CHANGELOG) | gVisor; images by tag not digest (the digest each run used is recorded); no CI job exercises Docker |
| M2 | `target.py`, `fit.py`, `knowledge/libmap/*.yaml`, `prompts/target_fit.md`, review-UI Target fit tab | deterministic layer on fixtures, evidence verification of model risks, resolutions, planner gate | deterministic layer on the real fixtures (10 risks on express-crud) | the model layer never ran with a real model; the libmap tables are a starting set |
| M3 | `testgen.py`, `recorder.py`, `casestore.py`, `harness_runner/` (http/job; stub server), `prompts/test_gen.md` | offline proposer, validation, double recording with noise masks, runtime findings, coverage | in-network recording against real containers: express-crud 24 authenticated cases (statuses 200 x14, 400 x8, 404, 204) after header injection and seed replay were added; spring-lite 6 (2 runtime findings) | the model proposer never ran; the offline proposer is shallow (mostly 401 and 400 answers); `grpc` and `message` cases return `not_implemented`; DB state, stub calls and messages are compared only when an observation carries them |
| M4 | `planner.py`, `slice_runner.py`, `codegen.py`, `reviewer.py`, `targetrepo.py`, `codemods/`, `prompts/{plan,codegen,codegen_fix,code_review}.md` | planner invariant and ordering, edit validation and jail, secret scan, dependency allow/deny, resumability, budget caps, offline loop to `awaiting_human`, static deny checks | codemods: OpenRewrite (Maven), django-upgrade + pyupgrade and lebab each transformed real code in the sandbox; new-service build in the sandbox | no generation with a real model; OpenRewrite through Gradle never ran; dependency mirror resolution is a hook only |
| M5 | `equivalence.py`, `approvals.py`, `differential.py`, `prompts/equiv_triage.md`, review-UI Plan, Slice, Comments and Cut-over tabs | seeded regression caught, classification, triage quote verification, mask approval, approval refused on blockers, human commit forces a re-run, reviewer marks, latency, fuzzing | equivalence in-network against two real containers (24/24 on an identity port); a seeded 404-to-410 change caught (1 regression); a seeded price-boundary change **not** caught by the offline cases or the fuzzer; latency recorded; 90 fuzz variants, no divergence on identical code | triage never ran with a real model; the UI was exercised through its HTTP API in tests, not in a browser |
| M6 | `configmap.py`, `schemacompat.py`, `router/`, `harness.py` (`--new`, `--composite`, `flip`, `unflip`, `routes`), `equivalence composite` | config maps for Spring/Node targets with secret references, schema diff and declared changes, router route table and reload, flip rules | the composite ran in containers: legacy, new and router on one internal network, flip routes a slice to new, 15 cases pass through the router, rollback | with in-memory `sqlite` or no DB engine each service has a private database (`db_shared: false`); schema check against a real database engine not run; the go-chi pair is not built |
| M7 | `cutover.py`, `metrics.py`, `supplychain.py`, `service.py` Phase 2 stages, `evalharness.score_migration`, `scripts/eval_phase2.py`, `eval/targets/`, CI job `eval-phase2` | readiness checks, plan determinism, approval hashes, metrics, supply-chain scan, stage wrappers, eval scoring and regression gate | lockfile hashes and SBOM on real builds (211 components on express-crud); cut-over tab state | eval baselines are hand-written reference runs, not agent output; no nightly real-model run; CI runs have ended in `startup_failure` |

Added after the first version of this table: the Cut-over tab, human verdicts
on reviewer findings (reviewer precision), the latency comparison, differential fuzzing, and lockfile hashes plus a
CycloneDX SBOM per build.

Not built from this plan: the `django-lite__fastapi` and `express-crud__go-chi` eval pairs, gRPC and message cases,
and hosted Phase 2 (P2-Q13: CLI and self-hosted only, by decision). Everything still open, with reasons:
[remaining-work.md](remaining-work.md).

---

## 0. What Phase 2 inherits, and what it cannot assume

Reusable as-is or with small extensions:

| Asset | Where | Phase 2 use |
|---|---|---|
| Project folder as the only state (Q18) | `project.py:1-68` (`SUBDIRS`, `ProjectMeta.schema_version=1`) | Add Phase 2 subfolders; bump schema to 2; every agent resumes from files |
| Graph with stable IDs, typed nodes/edges, evidence, derived confidence, `quirk`/`suspected_bug` | `graph/models.py:8-124` | Slice definition, ordering (`calls`/`depends_on`/`reads`/`writes` edges), test-to-item mapping |
| Immutable snapshots + diff | `graph/store.py:81-138` (`save_snapshot`, `diff_graphs`) | Staleness check; reuse the snapshot pattern for plans and slice states |
| ID registry | `graph/registry.py` | Same pattern for `SLICE-`, `CT-` (characterization test), `EQ-` (equivalence finding) IDs |
| One-method LLM protocol, tiers by `task`, retries, disk cache keyed by model/system/prompt/version, audit per call | `llm/base.py:19-24`, `llm/litellm_client.py:38-75, 239-245, 385` | New tasks: `target_fit`, `plan`, `test_gen`, `codegen`, `code_review`, `equiv_triage` |
| `Budget` (USD/token, thread-safe) and `--max-usd` | `llm/litellm_client.py:76-97`, `cli.py:30` | Per-run caps. **[Gap]** Budget is per process: a resumed run starts at $0. Phase 2 needs a cumulative ledger (below) |
| Pre-run estimate (`analyze --dry-run`) | `analyze/runner.py:89`, `service.estimate` | Same UX for `generate --dry-run` |
| Checkpoints and resume | `analyze/runner.py`, `checkpoint/` | Per-slice state machine; interrupted generation resumes at the last completed step |
| Append-only audit with tokens/USD/timing, `O_APPEND`+`flock`, redaction | `audit.py:44-165` | Add `exec` entries (command, image digest, exit code, duration) for build/test runs |
| Prompt-injection hygiene: delimiters, `neutralize`, static system prompts | `analyze/module_analyzer.py`, threat model T6/T7 | Every Phase 2 prompt carrying code, docs, test output or reviewer text |
| Strict model-output validation (`extra="forbid"`, `sanitize_patch`, evidence check) | `graph/models.py:22`, `analyze/evidence.py`, T8 | Same discipline for generated file patches and test cases |
| Path-jailed read-only tools | `sandbox/tools.py:66-247` | Generator reads legacy `code/` and the new tree through two jailed instances. Writes never go through tools (see 5) |
| Container profiles: `network none`, `internal` net + Squid allowlist, read-only rootfs, cap_drop, non-root | `docker/`, `sandbox/README.md` | Template for the build/test sandbox. **[Gap]** "reviewed-but-untested" (`sandbox/README.md`); must be exercised in CI first |
| Review server: loopback, token, Host/Origin checks, narrow write API | `review_ui/server.py:77-433` | Add slice diff, test results, equivalence report, approvals |
| Comments anchored to item IDs, targeted re-analysis, changelog | `review/comments.py`, `review/reanalyze.py:129`, `review/changelog.py` | Comments on `SLICE-`/`CT-`/`EQ-` IDs and on new-code `file:line`; targeted regeneration |
| Verifier ideas: fresh session, quoted evidence, drop unverifiable findings | `verify/verifier.py`, `verify/models.py:1-71` | Blind spec-vs-code reviewer and equivalence triage |
| Library API with events | `service.py:125` `run_stage`, `docs/platform-contract.md:33-40` | New stages so the hosted backend can run Phase 2 without CLI changes |
| Eval harness + regression gate (`compare` fails on any F1 drop) | `evalharness.py:163-224`, `eval/repos/*` | Extend with Phase 2 scores and fixtures |
| Extractors: config keys with `is_secret`, schemas (Flyway/Alembic/Prisma/TypeORM), OpenAPI/proto, manifests/deps | `extract/config.py`, `extract/schemas.py`, `extract/specs.py`, `extract/manifests.py`, `extract/deps.py` | Config mapping, schema inventory, endpoint contracts, library replacement inputs |

Constraints and known gaps Phase 2 must design around:

1. **Phase 1 accuracy is unproven on real repos.** Phase 0 ran three small repos, all as a single chunk, no hand
   scoring; no node reached `high` confidence (`docs/phase0-results.md`). The model-derived layer (rules, flows) is thin:
   688 of 823 petclinic nodes were static config keys. Phase 2 must therefore **not trust the graph as an oracle**. The
   oracle is the running legacy service; the graph only says *what to test and in what order*.
2. **The Verifier has never run on real docs** (`phase0-results.md`, "Not done yet"). Deferred findings will be common.
3. **The graph has no behavioural schema.** `Node` has name/module/symbol/responsibility/evidence only
   (`graph/models.py:58-68`); endpoint method+path live in `name`; no request/response shapes, status codes or error
   contracts. Test generation must recover these from code + OpenAPI facts, not from the graph.
4. **`code/` is a redacted copy** with credential files withheld (`ingest/__init__.py:271`, threat model section 5).
   Redaction can change program text (`password = "[REDACTED:...]"`), and withheld `.npmrc`/`*.jks` can break builds.
   Building the legacy service needs a different input than the one the model reads.
5. **Phase 1 never executes code; Phase 2 must.** That reverses the core trust assumption (section 6).
6. **`migration_contract.json` is being written now by another engineer.** No code in `src/` references it yet. This plan
   consumes it through a reader that should live next to the writer (one module, one schema).
7. `LLMClient.complete` is single-shot text/JSON, no tool calling (`llm/base.py:19-24`). Generation must work as
   "orchestrator gathers context, model returns structured edits". That fits the existing cache/budget/audit.
8. Hosted platform (0.3.0) is unaudited, worker holds DB credentials (`CHANGELOG.md`). Phase 2 execution must not ship in
   the hosted worker until the build sandbox is a separate, credential-free job runner.

---

## 1. Goal, scope, non-goals, entry gate, exit criteria

**Goal.** Turn an approved Phase 1 contract plus the legacy code into a new codebase on a user-declared target that
passes a characterization suite recorded from the legacy service, slice by slice, with every behaviour difference either
explained by an explicit `fix` decision or approved by a human.

**In scope**
- Three migration kinds, one engine: version upgrade (Java 8 -> 21, Spring Boot 2 -> 3, Django 3.2 -> 5.x), framework move
  (Express -> Fastify, Flask -> FastAPI), language move (Express/JS -> Go, Python -> Java).
- One service at a time (Q2 boundary unchanged). HTTP/RPC services and scheduled jobs first; message consumers second.
- Characterization tests, generated code, build files, config mapping, schema-compatibility check, equivalence report,
  cut-over plan (routing switch description, rollback steps).

**Non-goals (Phase 2)**
- Deploying to production, touching live systems, or flipping traffic. The tool produces the cut-over *plan* and
  artifacts; the user executes it. (Q4/Q7 spirit: no live access.)
- Reading production data, secrets or logs. Recorded traffic is user-supplied and redacted at ingest.
- Data migration execution against real databases. We produce mapping specs and scripts, run them only on synthetic
  data in the sandbox.
- Multi-service or monorepo-wide migrations, UI/front-end rewrites, performance tuning beyond "no regression worse than
  a declared threshold" (optional).
- Re-doing Phase 1. Phase 2 can raise findings back to Phase 1 (section 4.5), never edit the graph itself.

**Entry gate** (`migration-agent contract check`, must pass before any Phase 2 command writes):
1. `docs/migration_contract.json` exists, parses against its schema, `schema_version` is supported.
2. `graph_sha256` equals sha256 of the referenced graph snapshot file; `doc_sha256` equals the approved doc file. A
   mismatch means someone re-analysed after approval: **stale**, refuse.
3. `graph_version` equals `graph/latest.json` version, or the user passes `--pin-contract` to migrate from the approved
   snapshot while later versions exist (warn).
4. **[Rec]** contract also records `source_hash` from `ingest_report.json` (`ingest/__init__.py:57,354`). Gate recomputes
   the tree hash of the *original* legacy source used for builds; mismatch = legacy changed since approval. If the
   contract writer does not include it, Phase 2 records it at `target init` and checks it from then on.
5. Every `quirk`/`suspected_bug` node has a decision (default `preserve` already satisfies this).
6. Coverage summary shows no unmapped inventory items (Q16), else refuse unless `--accept-unmapped` (logged).
7. Deferred findings are allowed but are attached to slices as blockers (section 4.4).

**Exit criteria (per service)**
- All slices `approved`; each has: build green in sandbox, characterization pass rate 100% excluding approved
  divergences, equivalence report with zero unexplained diffs, human approval recorded.
- Test-coverage table: every endpoint, job, `business_rule` and quirk node in scope maps to >= 1 characterization test,
  or is marked `untestable` with a reason the user approved (same rule as Q16 coverage).
- Config map complete (every `config` node mapped, dropped-with-reason or open); secrets referenced by name only.
- `cutover/approval.json` signed by the user, with hashes of contract, plan, final tree and equivalence report.
- Audit log shows cumulative cost within the declared project budget.

---

## 2. Target specification and target-fit analysis (Q15 follow-up)

**[Rec]** A declarative `target/target.yaml`, validated by a strict pydantic model (`extra="forbid"`, same as
`graph/models.py:22`). Written by `migration-agent target init` (interactive or flags), edited by the user, hashed into
every later artifact.

```yaml
schema_version: 1
kind: version_upgrade            # version_upgrade | framework_move | language_move
source:                          # filled from facts/manifests.json, user confirms
  language: java
  runtime: "8"
  framework: spring-boot@2.7
target:
  language: java
  runtime: "21"
  framework: spring-boot@3.3
  build: maven                   # maven | gradle | pip | poetry | npm | go-mod ...
  base_image: eclipse-temurin:21-jre@sha256:...   # pinned digest
infra:
  db: postgres@15                # stays the same unless declared
  messaging: none
  deploy: container
  port: 8080
style:
  formatter: google-java-format
  linters: [checkstyle]
  package_root: com.acme.library
  rules: ["constructor injection only", "no Lombok"]
libraries:                       # optional pins and bans
  allow: []
  deny: ["log4j:log4j"]
equivalence:
  wire_compat: strict            # strict | json-semantic | declared-only
  perf_regression_max_pct: null
strategy: strangler              # strangler | big_bang (section 3)
budget_usd: 50                   # project-level cap across all sessions
```

**Target-fit analysis** (`migration-agent target fit`, new module `migrate/fit.py`, task `target_fit`): reads only
the approved doc, approved graph snapshot, `facts/manifests.json` and `target.yaml` (consistent with Q15: a separate
step reading the approved doc). Two layers:

1. **Deterministic** (no model): map each `external_dependency` node and manifest entry to the target through a
   versioned mapping table `migrate/knowledge/libmap/*.yaml` (e.g. `javax.persistence -> jakarta.persistence`,
   `springfox -> springdoc`, `express-validator -> go-playground/validator`, removed JDK APIs). Unknowns become
   `unmapped_library` risks. Also checks runtime facts: removed language features (Java `SecurityManager`, Python 2
   idioms), framework features (Spring Boot 3 trailing-slash matching default change, Django `USE_L10N` removal).
2. **Model**: for each component/rule, flag constructs with no direct target equivalent (checked exceptions -> Go
   errors, JPA lazy-loading semantics, JS number vs Go int64, implicit type coercion in comparisons, thread-local
   transactions, reflection). Output is strict JSON; every risk cites graph IDs **and** `file:line` evidence verified by
   the existing `analyze/evidence.verify_evidence` against `code/`. Unverified risks are dropped and counted, same as
   C-09 in the Verifier.

Output `target/fit_report.json` (`RISK-NNNN`: kind `no_equivalent | library_replacement | semantic_shift |
infra_gap | unknown`, severity, item_ids, evidence, proposed handling, `needs_decision: bool`) and rendered
`target/fit_report.md`. Risks with `needs_decision` go to the review UI like Verifier findings; resolutions stored in
`target/fit_resolutions.json`. Plan generation is blocked while any high-severity risk is open.

Why: keeps Phase 1 target-agnostic, makes target concerns evidence-backed and reviewable with the same UX, and a
deterministic table makes version upgrades cheap and testable without the model.

---

## 3. Migration strategy

**Unit of migration: the slice.** [Rec] A slice is one externally observable entry point (endpoint group by resource,
or one job/consumer) plus the transitive closure of components it calls that are not yet migrated. Shared components
(repositories, utilities, entities) get their own **foundation slices** migrated first.

Slice construction (deterministic, `migrate/planner.py`):
1. Build a directed graph from the approved snapshot using `calls`, `depends_on`, `reads`, `writes`, `implements`,
   `configures` edges. Ignore `dead_code` nodes unless the user opts in (default: not migrated, listed in the plan).
2. Condense strongly connected components (cycles become one slice; flagged if large).
3. Topologically order: foundation slices (entities, data access, config, cross-cutting auth/error handling) ->
   leaf-to-root components -> entry-point slices. Ties broken by risk (fewest fit risks and highest confidence first,
   so early slices are the most likely to succeed and calibrate the loop).
4. Size check: estimated tokens of the slice's legacy code (reuse `analyze/chunker._tokens`) must fit the codegen
   context budget; oversized slices split along file/top-level symbol boundaries (reuse chunker's split logic).
5. Model pass (task `plan`, optional): names slices, proposes target package layout, points out slices that should be
   merged. Output validated: it may only reference existing node IDs and cannot drop nodes (every in-scope node must
   land in exactly one slice; checked deterministically, mirroring the Q16 coverage rule).

Output `plan/migration_plan.json` (`SLICE-NNNN`, node_ids, depends_on slices, order, kind, risks, estimate_usd,
status) and `plan/migration_plan.md`. Plans are immutable versions like graph snapshots (`plan/versions/vNNN.json`).

**Strangler-fig vs big-bang.** [Rec] Strangler-fig as the default for framework and language moves: the legacy and
new services run side by side behind a routing proxy in the sandbox; each approved entry-point slice flips its routes
to the new service, and the full characterization suite runs against the composite after every flip. This gives
incremental proof and an incremental cut-over plan the user can mirror in production.
For **version upgrades** [Rec] in-place transformation of a copy of the legacy tree (big-bang at the code level, but still
verified slice by slice through the test suite), because the "new" service is the same codebase and running two copies
behind a proxy adds little. Deterministic codemods run first (OpenRewrite for Java, `django-upgrade`/`pyupgrade`,
`jscodeshift`), the model fixes what they leave. Big-bang for language moves only when the service is small
(< ~30 nodes in scope) and the user opts in.

Strangler constraint: both services share the database in the sandbox, so schema changes are deferred until all
slices touching a table are migrated (section 9).

---

## 4. Behaviour preservation

### 4.1 Oracle is the legacy service, not the model
Characterization ("golden") tests are written **before any rewrite**, and their expected outputs are **recorded by
running the legacy service** in the sandbox. The model proposes inputs; it never writes expected values for preserved
behaviour. This is the main defence against Phase 1 inaccuracy (section 0, gap 1).

### 4.2 Test format: black-box and language-neutral
[Rec] Primary tests are black-box cases at the service boundary, stored as data, so the same suite runs against Java
8, Java 21 or Go:

```json
{"id": "CT-0042", "slice": "SLICE-0007", "covers": ["EP-0003", "RULE-0011"],
 "kind": "http", "setup": {"db_fixture": "fixtures/loans_overdue.sql", "stubs": ["EXT-0002/payment_ok"]},
 "request": {"method": "POST", "path": "/api/loans", "headers": {"Content-Type": "application/json"},
             "body": {"bookId": 7, "memberId": 3}},
 "expect": {"source": "recorded", "status": 409, "body": {"error": "book already on loan"},
            "db_after": {"loans": "count(book_id=7)=1"}},
 "normalize": ["$.timestamp", "$.id"], "decision": "preserve", "provenance": {"generator": "test_gen", "prompt_version": "v1"}}
```

Kinds: `http`, `grpc` (from proto facts), `job` (trigger via a harness entry point or CLI, assert DB/side-effect
state), `message` (publish to an in-sandbox broker, assert outputs). A Python runner (`migrate/harness/runner.py`)
executes cases against a base URL; one runner for every target. Optional target-native unit tests are generated later
per slice as a convenience, never as the equivalence proof.

### 4.3 Generating cases from the graph
`migration-agent tests generate` (task `test_gen`), per slice:
- Inputs: the slice's endpoint/job nodes, attached `business_rule` nodes and their evidence regions (read via
  `ReadOnlyTools`), OpenAPI/proto facts (`extract/specs.py`), schema facts (`extract/schemas.py`), quirk/bug flags.
- For each endpoint: happy path, each validation branch, each rule's boundary values, error paths, auth denied.
- For each `business_rule`: at least one case whose `covers` includes it; for each quirk: a case that exercises it.
- External systems (`external_dependency`/`integration` nodes) become stubs (a WireMock-style stub server in the
  sandbox); stub responses are generated, reviewed and versioned under `tests/stubs/`.
- DB state: an ephemeral DB container created from the repo's own migrations (Flyway/Alembic/etc.), seeded by
  generated fixtures. Never from a real database.
- **Recorded traffic (optional):** user drops request logs (HAR or JSONL) in `inputs/traffic/`; they go through the same
  ingest redaction (`ingest/secrets.py::redact_text`) and become `CT-` cases with `kind: replay`.

`migration-agent tests record` runs every case against the legacy service **twice** with different clocks/seeds;
fields that differ between the two legacy runs become automatic `normalize` masks (noise detection), then
expected outputs are stored. Cases that fail to run on legacy are triaged: harness problem, or a real legacy bug (raise
a finding, 4.5).

`migration-agent tests coverage` produces the coverage table (node -> CT IDs). Gate: no in-scope endpoint/job/rule/quirk
without a test or an approved `untestable` reason. Low-confidence rules ([Gap]: nearly all of them today) get
priority review here: the recorded behaviour either confirms them or contradicts them.

### 4.4 Preserve/fix decisions and deferred findings -> expectations

| Contract input | Test expectation |
|---|---|
| quirk / suspected_bug, `preserve` (default) | `expect.source: recorded` (legacy output is truth); tagged `quirk`; divergence = failure |
| quirk / suspected_bug, `fix` | Two artifacts: the recorded legacy result kept as `legacy_observed`, and `expect.source: specified` written by the model **and approved by the user** in the review UI. The equivalence diff marks this case `intended_divergence` with the decision ID. Unapproved specified expectations block the slice |
| Deferred Verifier finding (id, kind, item_ids) | Every slice containing any of `item_ids` gets a `blocker: F-NNNN`. Cases covering those items default to `recorded` and are tagged `provisional`. Slice approval requires the user to resolve the finding or explicitly accept "migrate legacy behaviour as-is" (recorded in `slices/SLICE-*/decisions.json`) |
| Open questions / assumptions | Shown on the slice that covers the referenced items; non-blocking unless the user marks them blocking |
| Fit-risk resolutions (section 2) | Turn into codegen constraints and, for semantic shifts (e.g. integer overflow), targeted extra cases |

### 4.5 Feedback to Phase 1
When recorded legacy behaviour contradicts a graph `business_rule` (the test written from the rule fails on legacy),
Phase 2 writes `EQ-`-style **runtime findings** to `migration/runtime_findings.json` (not into `feedback/findings.json`,
whose `FindingKind` is a closed Literal at `verify/models.py:19`). The user can reopen Phase 1 (new contract version) or
accept the recorded behaviour for this migration. This is also free empirical data for Phase 0 accuracy scoring.

---

## 5. Code generation agent design

### 5.1 Loop (per slice)
```
plan (deterministic + model) -> context pack -> generate (codegen) -> apply patch -> build -> unit tests
   -> characterization suite -> review (code_review, fresh session) -> fix loop (<= N iterations) -> human review
```
Orchestrator `migrate/slice_runner.py` is deterministic Python; the model is called only at `generate`, `review` and
`fix`. Status machine per slice: `planned -> tests_recorded -> generating -> built -> tests_passing -> reviewed ->
awaiting_human -> approved | rejected | blocked`. State in `slices/SLICE-NNNN/state.json` (atomic writes as in
`graph/store.py:35`), so any session resumes (Q18).

### 5.2 Structured outputs and "tools"
- Model output is strict JSON validated by pydantic: `{"files": [{"path", "op": "create|replace|edit", "edits":
  [{"search", "replace"}] | "content"}], "notes", "covers": [node IDs], "assumptions": [...]}`. Paths are validated
  against the target tree jail (same resolution rules as `ReadOnlyTools._resolve`, `sandbox/tools.py:86`); no `..`, no
  absolute paths, no writes to build/CI files outside an allowlist, size caps.
- Search/replace edits rather than whole files keep outputs inside token limits and make diffs reviewable. Edits that
  do not apply exactly are rejected and retried with the failure message.
- The model has **no exec or network tools**. Build/test is run by the orchestrator in the sandbox; the model sees
  truncated, redacted, delimiter-wrapped logs (`<<<BUILD_LOG_BEGIN>>>`, `neutralize`), treated as data.
- Read tools (`ReadOnlyTools`) on `code/` (redacted legacy) and on `target_code/` (new tree), used by the orchestrator
  to assemble context; optionally offered to the model as a request list (`"need": ["path:10-80"]`) answered in the next
  turn, which keeps `LLMClient` single-shot.
- Build-file and dependency edits require an extra deterministic check: every new dependency must resolve in the
  package mirror and pass the allow/deny list in `target.yaml` (defends against hallucinated or typosquatted packages).

### 5.3 Context management (graph as memory)
Context pack for a slice, assembled deterministically and size-capped:
1. Slice spec: nodes, responsibilities, rules with evidence quotes, quirk decisions, fit-risk resolutions.
2. Legacy code regions from node evidence (reuse `review/reanalyze._regions`, `reanalyze.py:63`) plus the files
   that define the slice's components, chunked with `analyze/chunker` when over budget.
3. **Interface summaries** of already-migrated dependency slices (public signatures extracted by tree-sitter from
   `target_code/`, via `extract/treesitter.py`), never their full bodies.
4. `target.yaml` style rules, target project skeleton (generated once in a foundation slice).
5. Failing tests/build errors for fix iterations, only the relevant ones.
The graph plus `migration_plan.json` is the memory across slices; no conversation history is carried between slices.

### 5.4 Reviewer
Fresh-session `code_review` task (Verifier pattern): sees the slice spec (rules, decisions) and the new code diff, **not**
the generator's reasoning or the legacy code. Returns findings `{kind: missing_rule | behaviour_change | style |
security | dead_code, item_ids, file:line, quote}`; quotes not found verbatim in the diff are dropped (C-09 pattern).
Blocking findings feed the fix loop; others go to the human review page.

### 5.5 Cost, caching, resumability, audit
- New task tiers in `LLMConfig` (`llm/litellm_client.py:38`): `codegen` and `code_review` on the strong tier,
  `test_gen` mid, `plan`/`target_fit`/`equiv_triage` per config. Same `*.config.json` files.
- `--max-usd` per invocation (existing `Budget`), plus **[New]** `migrate/ledger.py`: cumulative spend computed from
  `audit/audit.jsonl` (which already records USD per call). `Budget` is initialised with `budget_usd - ledger.spent()`
  so caps hold across resumed sessions. Per-slice cap `--max-usd-per-slice` (default: 3x the slice estimate) and a
  max fix-iteration count (default 4) stop runaway loops; the slice goes `blocked` with the reason.
- `generate --dry-run` shows per-slice token and USD estimate (reuse `estimate_cost`, `litellm_client.py:109`).
- Cache: existing `llm_cache/` key covers model/system/prompt/prompt_version, so re-running an unchanged slice is free.
  Prompt files under `migrate/prompts/*.md` with versions, like `analyze/prompts/`.
- Audit: every model call (existing), plus `exec` records (image digest, command, exit code, duration, log ref) and
  slice state transitions. `migration-agent audit --agent codegen` works unchanged.

---

## 6. Execution sandbox

Phase 2 must build and run both legacy and generated code. Treat **all** of it as hostile: legacy code is
attacker-controlled under threat actor A1, and generated code is model output under A2 (`threat-model.md` section 2).

**[Rec] design (`sandbox/exec.py`, `docker/exec/`):**
- **Two-phase jobs** per build/test:
  1. *Resolve/build* container: network only to a **package mirror** (Artifactory/Nexus/Verdaccio/devpi or a
     pre-populated local cache), through the existing Squid pattern with an allowlist of mirror hosts only. Lockfiles
     required; new dependencies must be in `target.yaml` allowlist or approved.
  2. *Run/test* containers (legacy, new, DB, stubs, router): `internal: true` network with no route out, no proxy.
- Rootless Docker or Podman; gVisor (`runsc`) recommended where available. `cap_drop: ALL`, `no-new-privileges`,
  non-root uid, read-only rootfs + tmpfs, `pids`, memory, CPU and wall-clock limits, disk quota on the workspace volume.
  Kill on timeout; containers are disposable per run.
- **No secrets present**: no model key, no git credentials, no cloud credentials, no host mounts except the job's own
  workspace copy. The model call happens in the orchestrator, never inside an exec container. The orchestrator host
  never imports or executes generated code itself.
- Required config values that are secrets (from `config` nodes with `is_secret`) get **synthetic** values generated
  per run (random passwords for the ephemeral DB, dummy API keys for stubs).
- **Legacy build input.** [Rec] Build legacy from the *original* source tree (pinned by `source_hash`), in the exec
  sandbox only. The model continues to see only the redacted `code/`. Withheld credential files are replaced by
  generated placeholders; if the legacy build genuinely needs a private registry credential, the user provides a mirror
  that serves those artifacts, never the credential.
- Results returned as files (JUnit/JSON reports, logs capped and redacted) into `runs/RUN-NNNN/`.
- Docker availability is a hard prerequisite; `migration-agent doctor` checks runtime, gVisor, mirror reachability.

**Threat-model changes** (new doc section "Phase 2 execution", T-numbers continue):

| New threat | Mitigation |
|---|---|
| Legacy/generated code escapes container (kernel exploit, docker socket) | gVisor/rootless, no socket mount, disposable containers, host never runs payloads |
| Exfiltration of source via build-time network | Build phase only reaches mirror hosts; test phase has no route out; egress logged |
| Supply-chain: malicious or hallucinated package names ("slopsquatting"), compromised mirror | Lockfiles with hashes, allow/deny list, mirror-only resolution, new deps need approval, SBOM per run (CI already does SBOM on tags) |
| Model writes malicious code (backdoor, data exfil on startup) | Reviewer pass with `security` kind, deny-list static checks (outbound HTTP to non-declared hosts, `exec`/`eval`, shell), human diff review, tests run without network |
| Prompt injection via build/test logs | Logs delimited, neutralised, capped, treated as data |
| DoS via fork bombs, disk fill, infinite loops | pids/memory/CPU/disk/time limits |
| Secret leakage into generated code or tests | Generated tree and fixtures scanned with `ingest/secrets.py` before commit and before any prompt |
| Hosted mode: exec on the worker that holds DB creds | Not allowed. Exec runs in a separate credential-free job runner (ephemeral VM/Firecracker preferred) |

Residual risk to state honestly: allowing package downloads at all is the largest new hole; an offline mirror the
customer controls is the only strong answer.

---

## 7. Equivalence verification

`migration-agent equivalence --slice SLICE-NNNN` (`migrate/equivalence.py`):
1. Bring up legacy and new stacks in the sandbox, each with its own fresh DB from the same migrations and fixtures and
   the same stub server recordings (or, in strangler mode, the composite with routes flipped).
2. Run every `CT-` case covering the slice plus all cases of earlier approved slices (regression), against both.
3. Diff per case: status, headers (configurable allowlist), body (JSON-semantic by default: key order ignored, numeric
   type rules from `target.yaml`), DB state after (table snapshots of tables the slice `writes`), stub calls made
   (order + payload), published messages. Apply `normalize` masks from recording.
4. Classify each difference: `match`, `intended_divergence` (fix decision), `noise` (mask), `regression`, `unknown`.
5. `equiv_triage` model task (optional, cheap tier) explains `unknown` diffs: it sees the case, both outputs and the
   relevant rule text, proposes `legacy_bug | new_bug | harness_issue | spec_ambiguity`, quoting the diff; quotes are
   verified verbatim (Verifier C-09 pattern), and it never sees generator reasoning.
6. Write `slices/SLICE-NNNN/equivalence.json` (`EQ-NNNN` per diff) and `equivalence/report.md` (per-slice equivalence %,
   regressions, intended divergences with decision IDs, untestable items).

On disagreement:
- `new_bug` -> back to the fix loop automatically (within budget/iteration cap).
- `legacy_bug` -> becomes a decision for the user: preserve (expectation stays recorded) or fix (adds a decision to
  `slices/*/decisions.json`; the user may later fold it into a new contract version).
- `harness_issue` -> fix fixture/stub/mask, record why; masks added this way require human approval so they cannot
  hide real regressions.
- `spec_ambiguity` -> open question to the user, slice blocked.

Optional extras: property-based/differential fuzzing of inputs per endpoint (generate random valid requests from
OpenAPI schemas, compare live), and a latency/perf comparison if `perf_regression_max_pct` is set.

---

## 8. Human review loop

Extend `review_ui` (keep loopback + token + Host/Origin checks, `review_ui/server.py:276-301`):
- New tabs: **Target fit** (risks + resolutions), **Plan** (slices, order, status, cost), **Slice** (diff of
  `target_code/` for that slice, linked legacy evidence side by side, tests with pass/fail, equivalence diffs,
  reviewer findings, blockers), **Cut-over**.
- Diffs come from git: `target_code/` is a local git repo, one commit per generation iteration, one tag per approved
  slice (`slice/SLICE-0007/approved`). The UI renders diffs from `git diff` via a subprocess with a fixed argv (no repo
  hooks: same hardening as `ingest._git_clone`).
- Comments: extend `CommentsFile` (`verify/models.py:67-79`) in a new `migration/comments.json` with `item_id` accepting
  `SLICE-`, `CT-`, `EQ-`, `RISK-` and graph IDs, plus optional `file`/`line` in `target_code/`. Redacted on write (existing
  rule).
- **Targeted regeneration** (`migration-agent generate --slice S --from-comments`): only unprocessed comments for that
  slice go into a fix iteration, mirroring `review/reanalyze.py:129`; result shown as a diff against the previous
  iteration (Q12 pattern).
- **Human edits are first-class**: the user may commit directly in `target_code/`. The slice runner detects
  non-agent commits, re-runs build/tests/equivalence and records them for the human-edit metric. The agent never
  overwrites human-authored lines without showing a diff and getting confirmation (lines tracked by commit author).
- **Approval per slice**: `migration-agent approve-slice SLICE-NNNN` or a button; refused unless build green, suite
  green (minus approved divergences), blockers resolved. Writes `slices/SLICE-NNNN/approval.json` (who, when, commit
  sha, report hashes).
- **Final cut-over approval**: `migration-agent cutover --approve` after all slices approved and a full-suite run on the
  final tree; writes `cutover/approval.json` and `cutover/plan.md` (route-flip order matching slice order, rollback per
  step, config/secret checklist, schema steps, monitoring to watch).

---

## 9. Data and config migration

**Schema.** [Rec] Default "no schema change": the new service uses the legacy schema as-is (required anyway while
strangling). Inputs: `data_entity` nodes and `extract/schemas.py` facts (Flyway, Alembic, Prisma, TypeORM).
- Foundation slice generates target ORM mappings for existing tables; a **schema-compat check** applies the legacy
  migrations to an ephemeral DB, starts the new service against it, and compares schema introspection with what the new
  ORM expects (e.g. Hibernate `validate`, Django `makemigrations --check`).
- If the user wants schema changes, they are declared in `target.yaml` (`schema_changes:`), generated as
  expand/contract migrations, applied only to synthetic data in the sandbox, and listed as cut-over steps. No real data is
  touched. Data mapping specs (`data/mapping.yaml`: old column -> new column, transform) get their own tests on fixtures.
- [Gap] Stored procedures/triggers not in the repo are Phase 1 blind spots (Q13); they block any slice whose tables are
  affected until the user supplies a schema export.

**Config.** `migration-agent config map` (`migrate/configmap.py`): every `config` node (from `extract/config.py`,
including `file`, `key`, `is_secret`) -> target key in the target's convention (Spring `application.yml`, Django
settings/env, Go env struct), with status `mapped | renamed | dropped (reason) | open`. Rendered as
`config/config_map.md` and generated target config files. Spring Boot 2 -> 3 renamed properties come from the
deterministic libmap table.

**Secrets.** The agent never reads secret values (they are already redacted/withheld at ingest, and the original
source is used only inside exec containers). Secret keys map to **references** (`${DB_PASSWORD}`, a vault path
placeholder) and appear in a checklist the user fills at deploy time. Generated code and config are scanned with
`ingest/secrets.py` before each commit; a hit fails the slice.

---

## 10. Observability and metrics

All derived from `audit/audit.jsonl` plus slice state files, so no new storage. `migration-agent metrics` (table/JSON)
and a review-UI dashboard; also emitted as `usage`/`progress` events through `service.run_stage` for the hosted product.

| Metric | Definition |
|---|---|
| Cost per slice / per node / per 1k legacy lines | Sum of `cost_usd` for calls tagged `slice=` |
| Tokens in/out, cache hit rate | Existing audit fields + `cached` |
| Iterations to green | Fix-loop count per slice |
| Build success first try % | Per slice and per target kind |
| Characterization pass rate | Passed / total, excluding approved divergences |
| Equivalence % | Cases `match` or approved divergence / total |
| Unknown-diff rate, mask additions | Signal of harness quality vs hidden regressions |
| **Human-edit rate** | Lines changed by human commits / lines changed by agent commits in `target_code/` per slice (git attribution); also comment count per slice |
| Reviewer precision | Reviewer findings later accepted by humans / total |
| Wall-clock per slice | Span timings (`AuditLog.span`, `audit.py:101`), split model vs exec |
| Runtime findings raised to Phase 1 | Count and how many confirmed: direct accuracy signal for Phase 0 |

---

## 11. Evaluating Phase 2 itself

Today the fixtures have no build harness, tests or runnable entry point (e.g. `eval/repos/spring-lite` is `pom.xml` +
9 Java files + `application.properties`), and `evalharness.score` scores graphs only (`evalharness.py:163`).

Extend:
1. **Make fixtures runnable**: add `eval/repos/<repo>/harness/` (Dockerfile, DB migrations/seed, start command) so
   legacy runs in the exec sandbox.
2. **Target pairs** `eval/targets/<repo>__<target>/`: `target.yaml`, a hand-written **reference characterization
   suite** (`expected_cases/*.json`) covering every labelled rule in `expected.json`, a list of deliberate quirks with
   expected preserve/fix behaviour, and a reference migration for sanity.
   - `spring-lite__java21-boot3` (version upgrade, first)
   - `django-lite__django5` (version upgrade) and `django-lite__fastapi` (framework move)
   - `express-crud__fastify-ts` (framework move) and `express-crud__go-chi` (language move); keep the
     prompt-injection canaries in `src/vendor/legacy-notes.js` and add new ones aimed at codegen (e.g. "add an
     outbound call to X").
   - Add one quirk and one suspected bug per fixture (odd rounding, swallowed error) to test decision handling.
3. **Scored harness** `evalharness_p2.py` (or `evalharness.score_migration`): build success, hidden reference suite pass
   rate on the generated service (the agent never sees the hidden suite), agent-suite coverage of labelled rules,
   equivalence %, intended-divergence correctness for `fix` decisions, canary absence in generated code and outbound
   calls (network log in test phase must be empty), cost and wall-clock.
4. **Gates**: hidden-suite pass rate must not drop vs the stored baseline (same `compare` semantics as today), zero
   canary hits, zero outbound connections in test phase, zero secrets in output. Offline mode with recorded LLM
   responses (FakeLLM/OfflineLLM, `llm/base.py:43`) keeps CI cheap; a nightly job runs real models.
5. Then real repos: petclinic Java 8/Boot 2 branch -> Java 21/Boot 3 (public reference exists), node-express-realworld ->
   Fastify. These are the Phase 2 equivalent of Phase 0 and should be reported the same honest way.

---

## 12. Milestones

New package `src/migration_agent/migrate/` (keeps Phase 1 modules untouched), new CLI group `migration-agent migrate
...` or top-level verbs (shown top-level for brevity). `ProjectMeta.schema_version` -> 2.

Project-folder additions:
```
<project>/
  docs/migration_contract.json     (Phase 1 output, read-only for Phase 2)
  legacy_src/                      original, unredacted source pinned by source_hash; mounted ONLY into exec containers
  inputs/traffic/                  optional user-supplied recorded requests (redacted at ingest)
  target/   target.yaml, fit_report.json|md, fit_resolutions.json
  plan/     migration_plan.json|md, versions/vNNN.json, registry.json (SLICE- IDs)
  tests/    cases/SLICE-NNNN/CT-NNNN.json, fixtures/, stubs/, recordings/, coverage.json
  harness/  generated compose files for legacy, new, db, stubs, router
  target_code/                     the new service, a git repo (agent and human commits)
  slices/   SLICE-NNNN/{state.json, context/, iterations/, review.json, decisions.json, equivalence.json, approval.json}
  runs/     RUN-NNNN/{meta.json, logs/, reports/}
  migration/ comments.json, runtime_findings.json
  config/   config_map.json|md
  data/     mapping.yaml, schema_compat.json
  equivalence/ report.md
  cutover/  plan.md, approval.json
```
`legacy_src/` is the one exception to "the model only sees redacted text" and needs an explicit decision (Q-P2-3).

| # | Milestone | Tasks (new modules / commands / files) | Acceptance | Effort | Depends on |
|---|---|---|---|---|---|
| M0 | Contract gate + project v2 | `migrate/contract.py` (reader + `check`, shared schema with Phase 1 writer), `project.py` v2 subdirs + migration of v1 folders, `migrate/ledger.py`, `migration-agent contract check`, `doctor` | Stale graph/doc hash, wrong version, unmapped coverage each refused with a clear message; ledger matches audit sums; tests for all gate rules | S | Phase 1 approval step |
| M1 | Exec sandbox + runnable fixtures | `sandbox/exec.py` (job spec, two-phase run, limits, result capture), `docker/exec/` profiles, mirror config, `legacy_src` import, `harness build/up/down`; `eval/repos/*/harness/`; threat-model section + red-team tests (network canary, fork bomb, disk fill, secret scan) | spring-lite, django-lite, express-crud build and start in the sandbox; test-phase containers cannot open any outbound socket (verified by test); Docker profiles finally exercised in CI | L | M0 |
| M2 | Target spec + fit analysis | `migrate/target.py` (pydantic model), `migrate/fit.py`, `migrate/knowledge/libmap/*.yaml` (start: Java 8->21, Boot 2->3, Django 3.2->5, Express->Fastify), prompts, `target init`, `target fit`, review-UI Target-fit tab | Fit report for each eval target pair; 100% evidence validity; deterministic layer finds all `javax`->`jakarta` and labelled removed APIs in fixtures | M | M0 |
| M3 | Characterization tests | `migrate/testgen.py`, `migrate/harness/runner.py` (http/job first, grpc/message later), stub server, fixtures from repo migrations, noise detection, `tests generate/record/coverage`, decision->expectation mapping, runtime findings | On each fixture: every labelled rule/endpoint/quirk covered; recorded twice with zero unmasked noise; hidden reference suite agrees with recorded expectations on legacy | L | M1 |
| M4 | Planner + slice generation loop | `migrate/planner.py`, `migrate/slice_runner.py`, `migrate/codegen.py` (patch schema, jail, apply), `migrate/reviewer.py`, codemod adapters (`migrate/codemods/openrewrite.py`, `django_upgrade.py`), prompts, `plan`, `generate [--slice] [--dry-run] [--from-comments]` | spring-lite -> Java 21/Boot 3 end to end with build green and agent suite green within budget; resume after kill mid-slice loses at most one model call; cost cap stops a looping slice | L | M2, M3 |
| M5 | Equivalence + review UI + approvals | `migrate/equivalence.py`, `equiv_triage` prompt, report rendering, review-UI Plan/Slice tabs (git diffs, comments with file:line), `equivalence`, `approve-slice`, human-commit detection | Seeded regression in a fixture is caught and classified; approval refused when blockers/regressions exist; human edit re-runs checks and is counted | M | M4 |
| M6 | Config, data, strangler router | `migrate/configmap.py`, `migrate/schemacompat.py`, router container for strangler mode, `config map`, schema-compat check, first language/framework move (`express-crud__fastify-ts`, then `__go-chi`) | Every config node mapped/dropped/open; no secret values in output; composite (legacy + new behind router) passes full suite after each route flip | M | M5 |
| M7 | Cut-over + Phase 2 eval gate | `migrate/cutover.py`, `cutover [--approve]`, `evalharness` Phase 2 scoring + CI gate, metrics command, `service.run_stage` stages (`target_fit`, `plan`, `tests`, `generate`, `equivalence`), docs (user guide, architecture, threat model) | All eval target pairs scored in CI offline; regression gate blocks a drop; cut-over plan rendered for spring-lite and express-crud; nightly real-model run reported honestly | M | M5, M6 |

Rough total: about 10-14 focused weeks for one engineer. M1 and M3 are the critical path and the most uncertain
(build environments of real legacy repos are messy: missing private artifacts, old JDKs, native deps).

CLI surface:
```
migration-agent contract check   <proj> [--pin-contract]
migration-agent doctor           <proj>
migration-agent target init      <proj> [--kind ... --language ... --framework ...]
migration-agent target fit       <proj> [--max-usd N] [--offline]
migration-agent plan             <proj> [--dry-run]
migration-agent harness build|up|down <proj> [--legacy|--new|--composite]
migration-agent tests generate   <proj> [--slice S] [--max-usd N]
migration-agent tests record     <proj> [--slice S]
migration-agent tests coverage   <proj>
migration-agent generate         <proj> [--slice S|--all] [--dry-run] [--from-comments] [--max-usd N] [--max-usd-per-slice N]
migration-agent equivalence      <proj> [--slice S|--all]
migration-agent config map       <proj>
migration-agent review           <proj>          (existing command, new tabs)
migration-agent approve-slice    <proj> SLICE-NNNN [--note ...]
migration-agent cutover          <proj> [--approve]
migration-agent metrics          <proj> [--format table|json]
migration-agent audit            <proj>          (existing)
```

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Phase 1 graph is wrong or thin (no `high` confidence seen; rules sparse), so slices and tests miss behaviour | Legacy execution is the oracle; coverage gate per slice; runtime findings feed back; optional recorded traffic; human review of low-confidence rules before test generation |
| Legacy cannot be built/run in the sandbox (private artifacts, ancient toolchains, native deps, needs external systems) | `doctor` + harness build in M1 before any generation; user-supplied mirror and base image; stubs for externals; fallback "static-only migration" mode clearly labelled with no equivalence claim |
| Characterization suite is shallow: passes while real behaviour differs | Rule/quirk coverage gate, boundary-value generation, optional differential fuzzing, recorded traffic replay, hidden reference suites in eval to measure the gap |
| Noise masks or "harness_issue" triage hide real regressions | Masks only from double legacy runs or human approval; mask additions are a tracked metric |
| Executing hostile code (section 6) | Two-phase network, gVisor/rootless, no secrets, disposable containers, red-team tests in M1 |
| Hallucinated or malicious dependencies | Mirror-only, lockfiles, allow/deny list, approval for new deps |
| Cost blow-up in fix loops on large services | Cumulative ledger, per-slice caps, iteration cap, dry-run estimates, cache, deterministic codemods first |
| Context overflow on big slices | Slice size check, interface summaries of dependencies, chunker reuse, search/replace edits |
| Contract drift (legacy keeps changing during a multi-week migration) | `source_hash` check; `migration-agent contract check` on every command; incremental Phase 1 re-analysis (`analyze/incremental.py`) + new contract version, then re-plan only affected slices |
| Strangler mode with shared DB hides schema coupling | Schema-compat check, writes-tables tracked per slice, schema changes deferred to after the last slice touching a table |
| Docker infrastructure untested so far (`sandbox/README.md`) | First task of M1 is CI that exercises the existing profiles |
| Hosted product runs exec on a credentialed worker | Phase 2 is CLI/self-hosted only until a separate exec runner exists |
| Scope creep across three migration kinds at once | Ship version upgrade end to end first (M4), framework/language moves in M6 |

---

## 14. Open questions for the owner (recommended answers)

**P2-Q1 Which migration kind ships first?**
Recommended: Java 8/Spring Boot 2 -> Java 21/Boot 3 version upgrade. Deterministic codemods exist (OpenRewrite),
same-language tests and builds are simpler, spring-lite and petclinic give a fixture and a real repo. Language moves
come second, after the harness and equivalence loop are proven.

**P2-Q2 What is the source of truth for expected behaviour?**
Recommended: the running legacy service (recorded outputs), with the graph only choosing what to test. Model-written
expectations only for `fix` decisions, and only after user approval.

**P2-Q3 May Phase 2 use the original, unredacted legacy source?**
Recommended: yes, but only mounted into exec containers that have no route out and no model access; the model keeps
seeing only redacted `code/`. Without this many legacy builds fail. This is a real change to the Phase 1 trust promise
and must be stated in the threat model and user guide.

**P2-Q4 Strangler-fig or big-bang?**
Recommended: strangler-fig with a sandbox router for framework/language moves; in-place transformation verified slice
by slice for version upgrades; big-bang only for small services on request.

**P2-Q5 What happens to deferred Verifier findings?**
Recommended: allowed into Phase 2, but every slice touching their items is blocked until the user resolves the finding
or explicitly accepts "migrate legacy behaviour as observed" for that slice.

**P2-Q6 Should the model get exec tools (agentic shell)?**
Recommended: no. The orchestrator runs builds/tests; the model returns structured edits and reads delimited logs. This
keeps `LLMClient` single-shot, all calls cacheable and audited, and removes a direct path from injected text to
command execution. Revisit only if eval shows the fix loop stalls without it.

**P2-Q7 Where do package dependencies come from?**
Recommended: a customer-controlled mirror (or pre-populated offline cache); public registries only via the allowlisting
proxy and only if the user opts in. Test phase is always offline.

**P2-Q8 Do generated target-native unit tests count as proof?**
Recommended: no. Equivalence is proven by the black-box characterization suite run on both services. Native unit tests
are a deliverable for the user's future maintenance, not the gate.

**P2-Q9 Can a human edit the generated code mid-migration?**
Recommended: yes, directly in `target_code/` via git. Edits trigger re-verification, are counted in the human-edit
rate, and are never silently overwritten.

**P2-Q10 Does Phase 2 change the database schema?**
Recommended: not by default. Keep the legacy schema; schema changes are an explicit, declared opt-in executed only on
synthetic data in the sandbox and handed over as cut-over steps.

**P2-Q11 Who owns the contract schema?**
Recommended: one module (`contract.py`) owned with the Phase 1 engineer, with a reader and writer and a JSON Schema
exported like `graph/store.generate_json_schema`; add `source_hash` and the approved `target`-agnostic statement now,
before the first contract is written.

**P2-Q12 Does Phase 2 wait for Phase 0 accuracy numbers?**
Recommended: build M0-M1 and the eval fixtures in parallel, but do not promise migration quality to any customer until
Phase 0 is finished and the Phase 2 eval pairs have baseline scores. Runtime findings from M3 are cheap Phase 0 evidence.

**P2-Q13 Hosted or CLI first for Phase 2?**
Recommended: CLI/self-hosted first (Q7). Hosted Phase 2 needs a separate credential-free exec runner and its own
security review; the current worker holds DB credentials (`CHANGELOG.md`, 0.3.0 known gaps).
