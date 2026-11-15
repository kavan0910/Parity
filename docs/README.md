# Parity documentation

*Parity is the product name; the command-line tool and Python package are still called `migration-agent`.*

**Parity migrates a legacy app to the newest version of its stack, or to a different technology, and proves the
new app behaves like the old one.** That is the MVP: a legacy app goes in; a migrated app, an equivalence report and a
cut-over plan come out. The five steps are understand, approve, migrate, prove and cut over; understanding the legacy
app is step 1 of the migration, not the product. Phase 1 is steps 1 and 2, Phase 2 is steps 3 to 5.

| Document | Audience | Contents |
|---|---|---|
| [user-guide.md](user-guide.md) | Analysts, engineers running the tool | Install, project folder, every CLI command, LLM config, the review loop, Phase 1 approval, security model, troubleshooting; section 9: the full Phase 2 guide |
| [architecture.md](architecture.md) | Contributors, security reviewers | Component diagram, data flow, trust boundaries, design decisions, Phase 2 components and folder layout v2 |
| [phase2-architecture.md](phase2-architecture.md) | Contributors | Phase 2 module map, public functions, shared contracts, IDs, folder layout, gates, CLI registration, ExecBackend contract |
| [phase2-plan.md](phase2-plan.md) | Owner, contributors | Phase 2 plan, milestones M0 to M7 with implementation status, risks, decisions (P2-Q1 to P2-Q13) |
| [api-reference.md](api-reference.md) | Integrators, contributors | Graph schema, findings/resolutions/comments/decisions formats, audit log, review-server HTTP API (incl. `/api/p2/*`), Phase 2 file formats, Python entry points |
| [threat-model.md](threat-model.md) | Security reviewers | Assets, actors, boundaries, STRIDE table; section 9: Phase 2 execution, generated code, router and approvals |
| [deployment.md](deployment.md) | Operators | Docker image, compose profiles, environment variables, model endpoints, upgrades, backups, releases, Phase 2 host requirements |
| [platform-contract.md](platform-contract.md) | Platform workstreams | Engine service API (`run_stage`, incl. Phase 2 stages), HTTP API, data model, worker |
| [operations-runbook.md](operations-runbook.md) | Operators of the hosted stack | Deploy, backup/restore, rotation, on-call; Phase 2 is not part of the hosted stack |
| [product-architecture.md](product-architecture.md) | Owner | Hosted product design (its "Phase 1/2/3" are product phases, not the migration phases) |
| [phase0-results.md](phase0-results.md) | Everyone | First runs on real repositories; accuracy not yet measured |
| [remaining-work.md](remaining-work.md) | Everyone | What is built, what ran for real, and what is left, against every plan (grill, implementation plan, Phase 2 plan, roadmap) |
| `eval/README.md` (repository root) | Contributors | Eval fixtures, Phase 1 scoring, Phase 2 target pairs, gate and baseline |

HTML versions of these pages are in `html/` (built by `scripts/build_docs_html.py`). The hand-built pages
`developer-handbook.html` (chapter 14), `tutorial.html` (sections 5 and 6) and `user-guide-app.html` (CLI table and
"Not available yet") are maintained separately. They were last updated on 2026-11-15 (Phase 1 approval gate, Phase 2, the live Docker findings); update them by hand whenever the markdown pages change in substance. The hosted app guide documents the
web app only: approval and Phase 2 are CLI features.

These files are published alongside the tool: they are included in the source distribution (`sdist`) and live
in the repository under `docs/`. Note that each *analysis project* also has its own `docs/` folder
(`<name>.migration/docs/`) holding generated output; that is unrelated to this directory.
