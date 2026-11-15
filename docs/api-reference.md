# API reference

Covers the on-disk formats (the stable contract between components), the review server's HTTP API and the Python
entry points meant for integrators. Schemas are defined by pydantic models; the authoritative sources are named
in each section. Formats carry a `schema_version`; additive changes keep the version.

## 1. Graph (`graph/models.py`)

`graph/snapshots/vNNN.json` and `graph/latest.json` contain a `Graph`:

```json
{"version": 4,
 "nodes": [{"id": "RULE-0003", "type": "business_rule", "name": "Bulk discount",
            "module": "src/services/orderService.js", "qualified_symbol": "applyDiscount",
            "responsibility": "10% off when total > 500",
            "evidence": [{"file": "src/services/orderService.js", "line_start": 20, "line_end": 24,
                          "symbol": "applyDiscount", "quote": "if (total > 500) total *= 0.9"}],
            "inventory_refs": ["INV-00012"], "quirk": false, "suspected_bug": false,
            "confidence": "high"}],
 "edges": [{"id": "EDGE-0007", "source": "COMP-0002", "target": "ENT-0001", "type": "reads",
            "evidence": [], "confidence": "none"}]}
```

All models forbid unknown fields. `confidence` is written for convenience but **derived** (never read back as input).

**Node.** `id` (`<PREFIX>-NNNN`, prefix must match `type`), `type`, `name` (non-empty), `module`, `qualified_symbol`
(with `module` forms the identity), `responsibility`, `evidence[]`, `inventory_refs[]` (INV ids from
`inventory.json`), `quirk` (behaviour deliberately preserved), `suspected_bug`.

| `type` | ID prefix |
|---|---|
| component | COMP |
| endpoint | EP |
| data_entity | ENT |
| external_dependency | EXT |
| business_rule | RULE |
| config | CFG |
| job | JOB |
| integration | INT |
| dead_code | DEAD |

**Edge.** `id` (`EDGE-NNNN`), `source`, `target` (node ids that must exist), `type` in
`calls, reads, writes, depends_on, publishes, consumes, implements, configures`, `evidence[]`.

**Evidence.** `file` (required, relative to `code/`), `line_start`, `line_end` (>= start; end requires start; 1-based),
`symbol`, `quote` (verbatim text). An evidence item is *strong* if it has a non-empty quote and a line or symbol.

**Confidence.** none (no evidence) / low / medium / high, from: 2+ strong -> high; 1 strong or 2+ any -> medium; else low.

**Integrity** (validated on load): unique node ids, unique edge ids, edges reference existing nodes.
IDs are allocated by `graph/registry.json` and never reused.

## 2. Inventory (`pipeline.py`)

`inventory.json`: `{"items": [{"id": "INV-00001", "kind": "source_file|route|config_key|dependency", "name": "...", "file": "...", "line": 12}], "counts": {"route": 9, ...}}`.

## 3. Findings, resolutions, comments (`verify/models.py`)

`feedback/findings.json` (written by `verify`):
```json
{"schema_version": 1, "graph_version": 4, "dropped_unverifiable": 2, "claims_extracted": 41,
 "findings": [{"id": "F-0001", "kind": "contradiction", "severity": "high",
               "title": "Conflict in bulk discount logic", "detail": "...",
               "item_ids": ["RULE-0003"], "area": "",
               "quotes": [{"source": "understanding", "text": "...", "anchor": "", "verified": true},
                          {"source": "requirements.md", "text": "...", "anchor": "", "verified": true}]}]}
```
`kind`: `contradiction | doc_only | code_only | doc_vs_doc`. `severity`: `low | medium | high`. A quote's `source` is
`"understanding"` or a path relative to `user_docs/`. `area` groups `code_only` findings by module.

`feedback/resolutions.json` (one entry per finding; re-recording replaces):
```json
{"schema_version": 1, "resolutions": [{"finding_id": "F-0001", "status": "accepted_doc",
   "note": "25% from 10 items", "affected_node_ids": ["RULE-0003"], "created": "2026-11-06T23:40:00+00:00"}]}
```
`status`: `open | accepted_doc | accepted_code | dismissed | needs_reanalysis | resolved | deferred`. `accepted_doc`
and `needs_reanalysis` are pending for re-analysis; the engine sets `resolved` afterwards. `deferred` postpones the
finding to the migration phase (no re-analysis; listed in the contract's `deferred_findings`). `affected_node_ids` is copied from the finding.

`feedback/comments.json`:
```json
{"schema_version": 1, "comments": [{"id": "CM-0001", "item_id": "RULE-0009", "text": "...", "author": "user",
   "created": "...", "processed": false}]}
```
`item_id` must be an existing node or edge id. `processed` becomes true once re-analysis consumed it.
`feedback/reanalysis_log.json` records each re-analysis run.

`feedback/decisions.json` (written by `decide` and `POST /api/decisions`): migration decision per quirk / suspected
bug, `{"schema_version": 1, "decisions": [{"item_id", "decision": "preserve"|"fix", "note", "created"}]}`; items without an
entry default to preserve. `feedback/approvals.json`: approval history, the last entry is current.
`docs/migration_contract.json`: `MigrationContract`, fields listed in [user-guide.md](user-guide.md) (`approve`);
schema 2 adds `source_hash`. `migration-agent contract schema` prints its JSON Schema.

## 4. Other files

* `docs/changelog.md`: one section per graph version: `## vN (from vM)`, `Triggered by: ...`, then Added / Changed / Removed with evidence and the *why*.
* `audit/audit.jsonl`: one JSON object per line: `seq` (monotonic), `ts` (UTC ISO), `agent`, `action`, `command`,
  `duration_ms`, `tool_calls`, `model`, `tokens_in`, `tokens_out`, `cost_usd`, `prompt_version`, `input_hash`,
  `trace_ref`, `status` (`ok|error`), `error`. Append-only, safe across processes.
* `ingest_report.json`: `source_type`, `source_hash` (sha256), `commit`, `files_copied`, `bytes_copied`, `files_skipped`,
  `exclusions[]` (`path`, `reason`, `detail`), `secret_findings[]` (`file`, `line`, `kind`, redacted `preview`), `files_with_secrets[]`.

## 5. Review server HTTP API (`review_ui/server.py`)

Base URL `http://127.0.0.1:<port>/<token>/` (printed by `review`). The token may instead be sent in the
`review_token` cookie (set when the root page loads). Requests with a wrong `Host` get 403, as does a POST whose
`Origin` is not the server itself. Methods other than GET/POST get 405.

| Route | Method | Description |
|---|---|---|
| `/` | GET | HTML UI (sets the cookie) |
| `/api/state` | GET | Project name, `readiness` (blockers, warnings), `approval` (state `none|approved|stale`), quirk `decisions`, findings, resolutions, comments, graph `versions`, document `sections`, coverage, `known_ids`, `p2` (below), non-fatal `errors` |
| `/api/diff?a=N&b=M` | GET | Graph diff between versions N and M (`added/removed/changed` nodes and edges) |
| `/graph[?v=N&prev=M]` | GET | Interactive graph page for a version, optionally highlighting changes since `prev` |
| `/api/comments` | POST | Body `{"item_id": str(1..64), "text": str(1..5000), "author": str(<=64, default "user")}` -> 201 with the comment |
| `/api/resolutions` | POST | Body `{"finding_id": str, "status": <status>, "note": str(<=5000)}` -> 201 with the resolution |
| `/api/decisions` | POST | Body `{"item_id": str(1..64), "decision": "preserve"|"fix", "note": str(<=5000)}` -> 201 with the decision; re-renders the document offline. 404 unknown item, 400 not a quirk/bug |
| `/api/approve` | POST | Body `{"approved_by": str(1..64), "note": str(<=5000)}` -> 201 with the approval record; 400 with the blocker list when not ready |

Phase 2 routes. `p2` in `/api/state` is `null` for a Phase 1-only project, else `{present, fit, plan, comments,
known_ids, errors}` (`plan.slices[]` carries `status`, `iteration`, `equivalence_pct`, `unexplained`, `approved`).
Phase 2 problems are reported in `p2.errors` and never break the Phase 1 view.

| Route | Method | Description |
|---|---|---|
| `/api/p2/slice?id=SLICE-NNNN` | GET | Slice detail: `slice`, `state`, `iterations`, latest `build`/`tests`, `review` findings, `diff` (git), `equivalence` report, `run` meta, `cases`, `blockers`, `decisions`, `fix_requests`, `masks`, `approval`, `human_commits`, `comments`. 400 bad id, 404 unknown slice |
| `/api/p2/comments` | POST | Body `{"item_id": str(1..64), "text": str(1..5000), "author": str(<=64), "file": str|null (target_code-relative), "line": int>=1|null}` -> 201; text redacted; 404 unknown item or file |
| `/api/p2/approve-slice` | POST | Body `{"slice_id", "approved_by", "note"}` -> 201 with the `SliceApproval`; 409 with the blocker list when refused |
| `/api/p2/reject-slice` | POST | Body `{"slice_id", "by", "reason" (required)}` -> 201; 409 when the slice cannot be rejected |
| `/api/p2/resolve-risk` | POST | Body `{"risk_id", "status": "accepted"|"mitigate"|"wont_fix", "note" (required), "by"}` -> 201 with the `FitResolution` |
| `/api/p2/approve-mask` | POST | Body `{"eq_id", "by" (required), "reason"}` -> 201 with the approved mask; 404 when the id is not a diff |

POST requires `Content-Type: application/json` (415 otherwise), `Content-Length` (411), body at most 64 KB (413).
Validation failures return 400 with a short message. The server records feedback and approvals only; re-analysis
is triggered by `migration-agent resolve PROJECT`, generation and equivalence by the CLI.

## 6. Python entry points

* `migration_agent.__version__`: installed package version.
* `migration_agent.llm.litellm_client.LLMConfig`, `LiteLLMClient`, `Budget`; `migration_agent.llm.base.LLMClient` (protocol:
  `complete(*, task, system, prompt, json_mode=True, prompt_version="v1") -> LLMResponse`), `BudgetExceeded`, `FakeLLM`.
* `migration_agent.graph`: `Graph`, `Node`, `Edge`, `Evidence`, `Registry`, `save_snapshot`, `load_latest`, `load_snapshot`, `diff_graphs`.
* `migration_agent.config_env`:
  * `load_llm_config_from_env(env=None) -> LLMConfig`: defaults, then the `MA_LLM_CONFIG` file, then `MA_LLM_*` overrides. Raises `EnvConfigError` (a `ValueError`) naming the bad variable.
  * `load_settings_from_env(env=None) -> EnvSettings(api_base, max_usd, log_level, config_path)`.
  * `configure_logging(env=None)`: applies `MA_LOG_LEVEL`.
  Variables are listed in [deployment.md](deployment.md#environment-variables).

```python
from migration_agent.config_env import load_llm_config_from_env, load_settings_from_env
cfg, st = load_llm_config_from_env(), load_settings_from_env()
```

* `migration_agent.service`: `run_stage` (Phase 1 and Phase 2 stages), `metrics`, `estimate`, `review_state` and the
  other functions in [platform-contract.md](platform-contract.md) section 2.
* Phase 2 library functions (`migration_agent.migrate.*`): see [phase2-architecture.md](phase2-architecture.md).

## 7. Phase 2 files

Formats are pydantic models in `migration_agent.migrate.models` (shared) or in the module that writes the file.
All are strict (`extra="forbid"`) and carry `schema_version` where they are top-level files.

| File | Model | Module |
|---|---|---|
| `target/target.yaml` | `TargetSpec` | `migrate/models.py` |
| `target/fit_report.json` | `FitReport` of `Risk` | `migrate/models.py` |
| `target/fit_resolutions.json` | `FitResolutionsFile` | `migrate/fit.py` |
| `plan/migration_plan.json` | `MigrationPlan` of `Slice` | `migrate/models.py` |
| `plan/planner_report.json` | `PlannerReport` | `migrate/planner.py` |
| `characterization/cases/*/CT-NNNN.json` | `CharacterizationCase` | `migrate/models.py` |
| `characterization/coverage.json` | `CoverageReport` | `migrate/recorder.py` |
| `slices/SLICE-NNNN/state.json` | `SliceState` | `migrate/models.py` (written by `migrate/state.py`) |
| `slices/SLICE-NNNN/equivalence.json` | `EquivalenceReport` | `migrate/models.py` |
| `slices/SLICE-NNNN/fix_requests.json` | `FixRequestsFile` | `migrate/equivalence.py` |
| `slices/SLICE-NNNN/decisions.json` | `SliceDecisionsFile` | `migrate/approvals.py` |
| `slices/SLICE-NNNN/approval.json` | `SliceApproval` | `migrate/models.py` |
| `migration/comments.json` | `P2CommentsFile` | `migrate/approvals.py` |
| `migration/runtime_findings.json` | `RuntimeFindingsFile` | `migrate/models.py` |
| `runs/RUN-NNNN/meta.json` | `{"job": ExecJob, "result": ExecResult}` | `migrate/models.py` |
| `equivalence/masks.json` | `MasksFile` | `migrate/equivalence.py` |
| `equivalence/composite.json` | `CompositeReport` | `migrate/equivalence.py` |
| `config/config_map.json` | `ConfigMap` | `migrate/configmap.py` |
| `data/schema_compat.json` | `SchemaCompatReport` | `migrate/schemacompat.py` |
| `cutover/approval.json` | `CutoverApproval` | `migrate/models.py` |

Phase 2 audit records include the actions `exec_run` (agent `exec`), `slice_transition` (agent `slice_runner`) and
`generate_step` spans; model calls made for a slice carry `slice=SLICE-NNNN`.
