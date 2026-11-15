# What is built, what is verified, and what is left

The product is a legacy-app migration: it takes an existing application and migrates it to the newest version of its
stack or to a different technology, and proves the new app behaves like the old one. This page measures how far each
part of that is built and verified. Understanding the legacy app is step 1 of the migration; the migration itself is
steps 3 to 5.

Last updated 2026-11-15. This page compares the code with every plan that defines the product:

* the design grill, `.lavish/migration-agent-grill.html` (Q1 to Q18, Phase 1: understand and verify);
* the implementation plan, `.lavish/migration-agent-impl-plan.html` (phases A to D, deliverables A-01 to D-08);
* the Phase 2 plan, [phase2-plan.md](phase2-plan.md) (milestones M0 to M7) and the Phase 2 grill outcome in its section 14;
* the product roadmap, [product-architecture.md](product-architecture.md) section 11 (hosted product, phases 0 to 3);
* the open security findings, [security/pentest-2026-11-09.md](security/pentest-2026-11-09.md).

Words are literal. **Built** means the code exists and offline tests pass. **Verified live** means it also ran for
real: a real Docker daemon, a real service. **Unmeasured** means it runs but nobody has scored its quality. Nothing
here has run with a real model for Phase 2, and nothing has been hand-scored on a large real repository.

## At a glance

| Layer | Built | Verified live | Main gap |
|---|---|---|---|
| Phase 1: understand, verify, approve (grill Q1 to Q18; plan A to D) | all 4 phases, A-01 to D-08 | ran on 3 small open-source repos with a real model | accuracy on real repositories is unmeasured |
| Phase 2: migrate and prove equivalence (M0 to M7) | all 8 milestones, plus the additions below | the sandbox, build, record, equivalence, fuzz, composite and 3 codemods ran on a real Docker daemon on the three eval fixtures | no stage has ever run with a real model |
| Hosted product (backend, web, deploy; roadmap phases 1 and 2) | API, worker, web UI, compose stack | never run end to end | Docker stack, Postgres paths, CI never exercised |
| Commercial (roadmap phase 3) | billing seam only | no | pricing, billing, SSO, SOC 2, legal |

The hosted web app and its landing page cover Phase 1 only. Approval (`decide`, `status`, `approve`) and all of Phase 2 are
command-line features; the landing pages say so.

## Phase 1: plan against code

Every grill decision (Q1 to Q18) and every implementation-plan deliverable has code and tests.

| Plan item | Status |
|---|---|
| Phase A: scaffolding, ingestion with zip-bomb and secret handling, tree-sitter, manifests, config, inventory, audit log, graph schema with stable IDs, sandbox baseline, walking skeleton, eval harness (A-01 to A-11) | built |
| Phase B: LLM layer with tiers, cache, retries and budget guard; chunk planner; module analyzer; merger with checkpoints; cross-reference; coverage checker; gap filler; business rules; renderer; evidence verifier; graph visualizer; `analyze` (B-01 to B-12) | built |
| Phase C: blind Verifier, finding classes, resolutions, targeted re-analysis, comments, review UI and server, quote verification, changelog (C-01 to C-09) | built |
| Phase D: extra route and schema extractors (Spring, Flask, FastAPI, Django, Rails, ASP.NET, Gin; Flyway, Alembic, Prisma, TypeORM, OpenAPI, proto), large-codebase handling, Docker packaging, incremental re-analysis, error recovery, threat model and red team, end-to-end tests, docs (D-01 to D-08) | built |
| Approval gate between the phases (`decide`, `status`, `approve`, migration contract) | built (added for Phase 2) |

Not done, and not code:

* **Accuracy is unmeasured.** The plan's own Phase 0 gate (5 to 10 real repositories of 50k to 500k lines,
  hand-scored precision, evidence validity and coverage, cost per 1k lines) has not run. The only real runs are three
  repositories of 1.8k to 4.3k lines, each in one chunk, on two different models, with nothing hand-scored. No node
  reached `high` confidence. This is the largest open question for the whole product.
* **Scale is tested on synthetic repositories only** (D-02 asks for a real 2,000-file repository).
* **The Verifier has not run against real documentation.**
* The signed, versioned release (D-03) needs CI, and CI has never run (see the platform section).

## Phase 2: plan against code

M0 to M7 are built. What changed in the pass that produced this page:

| Item | Before | Now |
|---|---|---|
| Real Docker daemon | never run | the express-crud fixture ran end to end (below); django-lite and spring-lite build and start |
| Cut-over in the review UI | CLI only | a Cut-over tab: every readiness check, the approval and whether its hashes still match, and an approve button |
| Reviewer precision | always `n/a` | humans accept or dismiss each reviewer finding (`slice review-finding`, or the Slice tab); precision is accepted over marked |
| Latency comparison | not built | every equivalence run records per-case timings and flags cases much slower on the new service (advisory) |
| Differential fuzzing | not built | `equivalence fuzz` mutates recorded HTTP cases and compares legacy with new (advisory) |
| Supply chain | warning only | every build records lockfile hashes and a CycloneDX SBOM; lockfiles generated by the build instead of committed are flagged |
| Authenticated services | every case answered 401 | a service declares `request_headers` (e.g. `X-API-Token: "{secret:API_TOKEN}"`); the runner injects the per-run synthetic secret into every request, and a case can override or remove a header |
| Seeding in-memory databases | declared in the spec, never executed | `seed_requests` are replayed after every `harness up` (legacy, new, and both services of the composite); a seed that fails stops `up` |
| Comparing from the same state | state skew between a used legacy and a fresh new service | `equivalence` and `equivalence fuzz` restart both harness services first (`--no-reset` to keep state) |
| Timestamps | rows seeded at different moments always differed | two ISO-8601 timestamps within 5 minutes are equal everywhere (recording, equivalence, fuzz, composite); hours apart, or zoned against naive, is still a difference |
| Codemods | OpenRewrite Maven only; jscodeshift stub; nothing had run | OpenRewrite for Maven and Gradle, django-upgrade with pyupgrade, and lebab for JavaScript; Maven, Python and JavaScript ran live |

What the live runs found. Every one of these passed all offline tests before it was fixed:

1. The allowlisting proxy (Squid) crashed on start, so every build that used a package mirror failed with `EAI_AGAIN`.
2. Its log directory was not writable by Squid's own user.
3. Its access log was unreadable, so the egress audit file was empty.
4. Squid's ICMP helper logged a `FATAL` on every start (disabled).
5. The strangler router never started: a tmpfs over the job's working directory hid the image's own `/app/proxy.py`.
   The schema probe had the same bug. The backend now refuses any job whose tmpfs would mask a path its command uses.
6. Codemod jobs had no writable `HOME` or package cache, `/tmp` is mounted `noexec` (tool binaries must be started
   through their interpreter), Maven ignores `HTTPS_PROXY`, and `js-codemod` on npm ships none of the transforms the
   adapter named. A tool crash was also indistinguishable from "no changes".
7. The spring-lite fixture did not compile (`Member.getId()` was missing); Phase 1 only ever read it.
8. Authenticated services could not be tested at all: the sandbox generated a random API token per run but nothing
   sent it, and the documented `seed_requests` replay did not exist. express-crud's 24 recorded cases were nearly all
   `401`. Header injection, seed replay, restart-before-compare and the timestamp rule fix this: the cases now record
   14 `200`s, 8 `400`s, a `404` and a `204`, and the identity port is 24 of 24 equivalent with zero fuzz divergences.

### What the live runs covered

| Fixture | What ran on a real daemon |
|---|---|
| express-crud | build in the sandbox with npm through the proxy; start; API seeds replayed with the synthetic token; 24 authenticated cases recorded twice; an identity port built and started as the new service; equivalence 24 of 24; latency recorded; 90 fuzz variants with no divergence; composite with the router: flip a slice, 15 cases pass through the router, roll back; then a seeded 404-to-410 change was caught (1 regression) |
| django-lite | build with pip through the proxy; start (the offline graph has no HTTP cases, so nothing to record) |
| spring-lite | build with Maven through the proxy; start Spring Boot with H2; 6 cases recorded, 2 runtime findings |
| codemods | django-upgrade and pyupgrade rewrote `force_text` and `class A(object)`; lebab rewrote `var`, concatenation and property shorthand; OpenRewrite changed the Java version in a `pom.xml` |

`MA_LIVE_E2E=1 .venv/bin/python -m pytest -q tests/test_p2_live_e2e.py` repeats the express-crud run (about 2
minutes; needs three images locally and access to registry.npmjs.org). 35 Docker backend tests, including live
ones for the sandbox, the proxy and the masking guard, run whenever a daemon is available.

### Still not done in Phase 2

| Gap | Why it is still open |
|---|---|
| **No stage has run with a real model**: target-fit model layer, test generation, code generation, review, equivalence triage, slice naming | needs an API key and spend; the offline proposer only produces shallow cases (mostly 401 and 400 answers). Everything above used `--offline` |
| Quality of generated code is unmeasured | follows from the line above; the eval baselines are hand-written reference runs, not agent output |
| **Subtle behaviour changes are only caught if a case reaches them** | a second seeded change (`price <= 0` became `price < 0`, so a zero price is accepted) was **not** caught: the offline-proposed cases never send a price, and the fuzzer only mutates fields already present in recorded bodies. Boundary cases are what the model proposer is for, and it has never run |
| The eval's hidden reference suite does not agree with the live legacy service | checked live: 0 of 9 of `eval/targets/express-crud__fastify-ts/expected_cases` pass. They assume a pre-seeded database and case ordering, some are meant to be unauthenticated, and they were derived from source, not from a recording. Not reconciled yet (a case can now drop the injected token with an empty header value) |
| No nightly real-model eval run | same |
| `eval/targets/`: the `django-lite__fastapi` and `express-crud__go-chi` pairs | only more scorer fixtures; no Go toolchain here and no libmap table for Django to FastAPI |
| gRPC and message characterization cases return `not_implemented` | needs gRPC clients and broker stubs in the runner image |
| gVisor (`runsc`) never tried | not installed on the machine used (the backend uses it automatically when Docker lists it) |
| OpenRewrite through Gradle never ran | Maven did; the Gradle init-script route is untested |
| In-memory sqlite or no database: each service keeps a private database in the composite | inherent: it cannot be shared. Use postgres or mysql to test a shared database |
| Images referenced by tag, not digest | each run records the digest it used in `runs/RUN-*/meta.json`; pinning is a deployment step |
| Lockfile *hash verification against the mirror* | hashes and an SBOM are recorded; refusing a package whose hash differs needs a mirror that publishes them |
| CI never ran (`startup_failure`), so `eval-phase2` and the Docker tests are not exercised in CI | a GitHub-side problem, not fixable from the code |
| Phase 2 on the hosted platform | decided against (P2-Q13): it executes code, so it is CLI and self-hosted only |

## Hosted product: what remains

Source: product-architecture section 11 and the pentest. The backend, worker, web app and compose stack exist and are
tested on SQLite with a fake engine; the Docker stack and the Postgres paths have never run.

| Area | Open item |
|---|---|
| Security | **F5 (High):** the worker holds database credentials and a model key where hostile input is parsed; design documented, not built. **F4 (High):** Postgres row-level security written but never executed. Postgres-side audit immutability unverified |
| Tenant-safe core (roadmap phase 1) | GitHub App for private repositories; S3 object storage wired; ingress with TLS; secret manager; monitoring, alerting and error tracking; retention auto-purge; fair scheduler and per-provider concurrency limits; multi-provider failover |
| Reviewable product (roadmap phase 2) | user-docs upload and Verifier screens in the web app; changelog page; exports (Markdown, PDF, JSON); audit-log UI; onboarding; status page; docs site; server-side graph indexing for graphs over 400 nodes; pending-invitation UI; OIDC login in the web app; browser end-to-end tests |
| Operations | the full stack built and run once; backups restore-tested; CI green on the three workflows |
| Accounts | e-mail verification, password reset, MFA, account deletion |

## Commercial (roadmap phase 3)

Not started, and gated on real cost numbers from the Phase 0 runs: pricing, a billing provider behind the existing
`BillingProvider` seam, plans, limits and invoices; a public security page; data-processing terms and subprocessor
agreements; SOC 2 Type I preparation (a readiness checklist exists); SSO. Later in the roadmap: a self-hosted
bring-your-own-key tier, more languages, CI integration (analyse on merge, drift alerts), and an API with webhooks.

## Recommended order

1. **Run the plan's Phase 0 gate with a real model**: 5 to 10 real repositories, hand-score a sample. Until this exists
   every claim about quality and every price is a guess, and it unblocks everything below.
2. **Run Phase 2 with a real model on the three fixtures** (the live test already covers everything except the model),
   then on one real service you can build. Read the generated code; record the human-edit rate and cost per slice.
3. Get CI green, so the Docker tests, the Phase 2 eval gate and the signed release run on every change.
4. Close F5 (worker isolation) and run the hosted stack once end to end, including Postgres and RLS (F4).
5. Finish the web screens that the CLI already has (approval, Phase 1 gate, user-docs upload, exports).
6. Only then decide pricing and billing.
