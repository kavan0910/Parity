# User guide

**migration-agent migrates a legacy app to the newest version of its stack, or to a different technology, and proves the
new app behaves like the old one.** The MVP is: legacy app in; migrated app, equivalence report and cut-over plan out.
The migration has five steps: understand, approve, migrate, prove, cut over. This guide covers all of them: sections 1
to 8 are steps 1 and 2 (understand and approve), section 9 is steps 3 to 5 (migrate, prove, cut over).

To start, migration-agent reads a legacy service's source code and produces an **evidence-backed understanding document**
(`docs/understanding.latest.md`) plus a **knowledge graph** (components, endpoints, data entities, business rules,
...). Every claim cites file and line range with a verbatim quote. A **blind Verifier** then compares the document
with the documentation your team already has and reports contradictions, so humans can resolve them. A human then
approves the understanding, which writes the **migration contract** (`docs/migration_contract.json`).

That is **Phase 1** (sections 1 to 8). Phase 1 never executes the code it analyses and never modifies the source.

**Phase 2** (section 9) starts from the approved contract and migrates the service slice by slice: target-fit
analysis, a slice plan, characterization tests recorded from the running legacy service, a sandboxed generation loop,
equivalence checks legacy vs new, per-slice approvals, an optional strangler router, and a final cut-over approval.
Phase 2 does build and run code, but only inside network-isolated Docker containers (section 9.1). Phase 2 is
implemented and tested offline, and has run on a real Docker daemon on the eval fixtures; it has not yet run with a real
model (section 9).

## 1. Install

Requires Python 3.11 or newer.

```bash
python -m venv .venv && . .venv/bin/activate
pip install ".[llm]"          # from a checkout; "[llm]" adds LiteLLM, needed for real model calls
migration-agent --help
```

Without the `llm` extra everything works except real model calls (`analyze --offline`, `extract`, `render`,
`ingest`, `audit`, `changelog` and `review` need no model). Add `pytest` with `pip install -e ".[dev,llm]"` for development.
For containers see [deployment.md](deployment.md).

### API keys and `.env`

Model access is configured through environment variables understood by LiteLLM:
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `OPENAI_API_KEY`. The CLI refuses to start a real
analysis if none of them is set.

On start-up the CLI loads a `.env` file (copy `.env.example`): from the checkout root (the directory containing
`pyproject.toml`) or, when installed elsewhere, from the current directory. Rules: `KEY=VALUE` lines, optional `export `, quotes
stripped, `#` comments; **variables already in the environment are never overridden**. `.env` is git-ignored;
never commit it.

## 2. The project folder

All state lives in one folder, `<source-name>.migration/` by default. It is safe to copy, back up or commit
(except `llm_cache/`, see below).

```
svc.migration/
  project.json          name, source, created, schema_version
  code/                 normalised copy of the service (secrets redacted)   <- ingest
  ingest_report.json    what was copied/skipped, secret findings (no values) <- ingest
  facts/                ast.json, manifests.json, config.json               <- extract
  inventory.json        coverage inventory (INV-nnnnn items)                <- extract
  graph/                snapshots/vNNN.json, latest.json, registry.json     <- analyze/resolve
  checkpoint/           per-chunk analysis patches (lets analyze resume)
  llm_cache/            cached model responses keyed by prompt hash
  docs/                 understanding.latest.md, graph.html, changelog.md,
                        understanding.vN.approved.md, migration_contract.json   <- approve
  user_docs/            YOUR existing docs (.md .txt .html .docx .pdf), for the Verifier only
  feedback/             findings.json, resolutions.json, comments.json, reanalysis_log.json,
                        decisions.json, approvals.json
  audit/                audit.jsonl (append-only)
```

Phase 2 adds more folders (`target/`, `plan/`, `characterization/`, `harness/`, `legacy_src/`, `target_code/`,
`slices/`, `runs/`, `migration/`, `config/`, `data/`, `equivalence/`, `cutover/`) the first time a Phase 2 command
writes; section 9.4 lists them. A project that never runs Phase 2 keeps exactly this layout.

`graph/registry.json` keeps IDs (`COMP-0001`, `RULE-0003`...) stable across re-analyses so references in your
reviews and docs do not break. `llm_cache/` contains prompts' responses derived from your code: treat it as
sensitive and exclude it from sharing.

## 3. Quick start

```bash
migration-agent init ./legacy-orders                 # creates ./legacy-orders.migration
migration-agent ingest legacy-orders.migration
migration-agent extract legacy-orders.migration
cp requirements.md legacy-orders.migration/user_docs/   # optional: docs for the Verifier
migration-agent analyze legacy-orders.migration --dry-run          # plan + cost estimate
migration-agent analyze legacy-orders.migration --max-usd 5
migration-agent verify  legacy-orders.migration
migration-agent review  legacy-orders.migration      # opens the local review UI
migration-agent status  legacy-orders.migration      # Phase 1 readiness: blockers and warnings
migration-agent approve legacy-orders.migration --by "Jane Doe"   # writes docs/migration_contract.json
```

Try the pipeline without any model: `migration-agent analyze PROJECT --offline` (deterministic stand-in; produces
the facts-level graph and documents, not a real analysis).

After approval, Phase 2 continues in section 9.

## 4. Commands

All commands take the project folder as the first argument. Every command appends to `audit/audit.jsonl`.
Run `migration-agent COMMAND --help` for the authoritative option list.

### `init SOURCE [--project PATH] [--name TEXT]`
Creates the project folder. `SOURCE` is a directory, `.zip`, tarball or git URL. Nothing is copied yet.
```bash
migration-agent init https://example.com/team/orders.git --project ./orders.migration --name "Orders service"
```

### `ingest PROJECT`
Copies the source into `code/`. Safe by construction: archive entries with absolute paths, `..`, NUL bytes are
rejected; size limits (500 MB, 200,000 files, compression ratio 100) apply; git clones run with hooks disabled;
symlinks, binaries, files over 1 MB and vendor/build directories (`.git`, `node_modules`, `vendor`, `build`,
`dist`, `target`, virtualenvs) are skipped. Credential files (`.env`, `*.pem`, `id_rsa`, `.npmrc`, ...) are
**withheld entirely**; secrets found in other files are replaced with `[REDACTED:<kind>]` in `code/`. Writes
`ingest_report.json`.

### `extract PROJECT`
Static extraction with tree-sitter: AST facts, dependency manifests, configuration keys, routes, import
edges. Writes `facts/` and `inventory.json`, the list of everything the analysis must account for.
Prints inventory counts by kind (source_file, route, config_key, dependency).

### `analyze PROJECT [--offline] [--dry-run] [--config PATH] [--max-usd FLOAT]`
Runs (and, if needed, first runs `extract`) the full pipeline: chunk plan, per-chunk model extraction, merge,
cross-reference resolution, evidence verification, business-rules pass, gap fill for unmapped inventory items,
then renders `docs/understanding.latest.md` and `docs/graph.html` and saves a new graph snapshot.

| Option | Meaning |
|---|---|
| `--dry-run` | Print `plan: N chunk(s), estimated cost $X` and exit. No key needed. |
| `--max-usd 5` | Hard budget. The run **stops gracefully** and keeps what it has; re-run to continue (cache and checkpoints avoid paying twice). |
| `--config FILE` | LLM configuration (section 5). Default: built-in Anthropic tiers. |
| `--offline` | No model; deterministic stand-in. |

```bash
migration-agent analyze orders.migration --config gemini-lite.config.json --dry-run
migration-agent analyze orders.migration --config gemini.config.json --max-usd 3
```

### `render PROJECT`
Builds a **facts-only** graph from the inventory (no model) and renders the document. Useful to check
extraction. Note it creates a new snapshot from facts only; run `analyze` for the real result.

### `verify PROJECT [--config PATH] [--max-usd FLOAT]`
The blind Verifier compares `docs/understanding.latest.md` with the files in `user_docs/` and writes
`feedback/findings.json`. Requires an existing understanding doc. It sees only those two inputs, never the code.
Each finding quotes both sides verbatim; findings whose quotes cannot be string-matched in the source text are
dropped (`dropped_unverifiable`). Kinds: `contradiction`, `doc_only` (documented but not in code), `code_only`
(in code but undocumented), `doc_vs_doc`. Prints one line per finding.

### `review PROJECT [--port N] [--no-browser]`
Starts the local review UI (section 6). Prints a URL containing a one-time token.

### `resolve PROJECT [FINDING_ID] [--status S] [--note TEXT] [--comment ITEM_ID] [--reanalyze/--no-reanalyze] [--config PATH] [--max-usd FLOAT]`
Records a decision, then runs **targeted re-analysis** of only the affected nodes and regenerates the changelog.
Statuses: `accepted_doc` (the document is right, the code differs: re-analyse), `accepted_code` (code is the truth),
`dismissed`, `needs_reanalysis`, `deferred` (acknowledged, to be handled during the migration: no re-analysis, it does
not block approval, and it is listed under `deferred_findings` in the migration contract).
```bash
migration-agent resolve orders.migration F-0001 --status accepted_doc --note "Discount is 25% from 10 items"
migration-agent resolve orders.migration --comment RULE-0003 --note "Check the VIP exception in pricing.py"
migration-agent resolve orders.migration F-0004 --status dismissed --no-reanalyze
migration-agent resolve orders.migration F-0005 --status deferred --note "Decide with the payments team"
```
Your note is passed to the model as untrusted *data*; every resulting claim still needs verbatim code evidence.
If the budget stops a re-analysis, nothing is written.

### `decide PROJECT ITEM_ID (--preserve | --fix) [--note TEXT]`
Records the migration decision for a quirk or suspected bug (section 12 of the document) in
`feedback/decisions.json`, then re-renders `docs/understanding.latest.md` from the latest graph without any model
call. Items without a decision default to **preserve**, because silently changing behaviour is the main migration
risk; the document shows `Migration decision: preserve (default)`, `preserve`, or `fix - <note>`. Only nodes flagged
as quirk or suspected bug in the latest graph accept a decision. Decisions are keyed by stable ID and survive
re-analysis.
```bash
migration-agent decide orders.migration RULE-0007 --fix --note "Refund window is off by one day"
migration-agent decide orders.migration COMP-0012 --preserve --note "Clients depend on the trailing slash"
```

### `status PROJECT`
Prints Phase 1 readiness and the approval state (`none`, `approved`, `stale`). Always exits 0.
Blockers (approval is refused while any exists): no graph or no understanding doc; coverage incomplete (unmapped
inventory items); a Verifier finding with no resolution or with status `open`; a resolution waiting for
re-analysis (`accepted_doc`, `needs_reanalysis`); a comment not yet used by re-analysis; a document on disk that
does not match a fresh offline render of the latest graph. Warnings (shown, recorded in the contract, not
blocking): deferred findings; Verifier never run; open questions; quirks/bugs still on the default decision;
low or no confidence nodes.

### `approve PROJECT --by NAME [--note TEXT]`
The Phase 1 exit gate. Exits non-zero and lists the blockers when the project is not ready (there is no
`--force`). On success it:

* appends the approval to `feedback/approvals.json` (history; the last entry is current);
* writes a read-only copy `docs/understanding.vN.approved.md` (N = graph version; `.rK` is added when the same graph
  version is approved again) with a header giving approver, time, graph version and both sha256 values;
* writes `docs/migration_contract.json` (below), the input of the migration phase;
* records an `approve` event in the audit log.

The approval pins the graph (sha256 of `graph/snapshots/vNNN.json`) and the document (sha256 of its text). Any
later snapshot (analyze, resolve, render) or document change (for example a changed decision) makes the approval
`stale`; run `status` and `approve` again.

`docs/migration_contract.json` (`MigrationContract` in `migration_agent.verify.models`):

| Field | Meaning |
|---|---|
| `schema_version` | 1 |
| `project_name`, `project_source` | from `project.json` |
| `graph_version`, `graph_sha256` | approved snapshot and the sha256 of its file bytes |
| `doc_sha256` | sha256 of the approved document text |
| `approved_by`, `approved_at`, `note` | who, when (ISO UTC), why |
| `approved_doc`, `graph_snapshot` | project-relative paths of the approved copy and the snapshot |
| `decisions` | every quirk/bug: `item_id`, `name`, `quirk`, `suspected_bug`, `decision`, `note`, `explicit` (false = default preserve) |
| `deferred_findings` | `id`, `kind`, `title`, `item_ids`, `note` of findings resolved as `deferred` |
| `open_questions`, `assumptions` | from the document (section 14) |
| `coverage` | `mapped`, `unmapped`, `excluded`, `complete` |
| `warnings` | readiness warnings at approval time |

### `changelog PROJECT`
Regenerates `docs/changelog.md` from consecutive graph snapshots, annotated with the comment or resolution that
caused each change.

### `audit PROJECT [--format table|json]`
Prints the audit log with totals (calls, tokens, USD).
```bash
migration-agent audit orders.migration --format json | jq '.[] | select(.status=="error")'
```

## 5. LLM configuration

`--config FILE` takes a JSON file; the settings may sit under a top-level `"llm"` key. Two ready-made files
ship in the repository root:

* `gemini.config.json`: all tasks use `gemini/gemini-3.5-flash`.
* `gemini-lite.config.json`: all tasks use `gemini/gemini-3.1-flash-lite` (cheapest, lowest quotas).

Fields (`LLMConfig`):

| Field | Default | Meaning |
|---|---|---|
| `models` | haiku for `chunk_extract`, sonnet for the rest | model per task tier: `chunk_extract`, `cross_ref`, `business_rules`, `gap_fill`, `verifier` |
| `default_model` | `claude-sonnet-5-5` | used for tasks missing from `models` |
| `pricing` / `default_pricing` | per-model table / `[3.0, 15.0]` | USD per 1M tokens `[input, output]`, used for budget and estimates. Set it for any non-default model, else costs are mis-estimated |
| `max_retries` | 4 | retries on 429 / 5xx / timeouts, exponential backoff with jitter |
| `backoff_base`, `backoff_max` | 1.0, 30.0 s | |
| `timeout` | 120 s | per call |
| `max_output_tokens` | 4096 | |

**Anthropic** (default; just set `ANTHROPIC_API_KEY`): cheap tier for bulk chunk extraction, stronger model for
reasoning tasks:
```json
{"llm": {"models": {"chunk_extract": "claude-haiku-5-5", "cross_ref": "claude-sonnet-5-5",
                    "business_rules": "claude-sonnet-5-5", "gap_fill": "claude-sonnet-5-5",
                    "verifier": "claude-sonnet-5-5"},
         "pricing": {"claude-haiku-5-5": [1.0, 5.0], "claude-sonnet-5-5": [3.0, 15.0]}}}
```
**Gemini**: set `GEMINI_API_KEY` and use `--config gemini.config.json`. Those files raise `max_retries` to 8
and backoff to 5 to 90 s because free-tier limits are tight.

Equivalent environment-variable configuration for containers (`MA_LLM_MODEL_<TASK>`, `MA_LLM_API_BASE`, ...) is
described in [deployment.md](deployment.md). Responses are cached in `llm_cache/`; identical re-runs cost nothing.

## 6. The review loop and server

1. `verify` produces findings. 2. `review` shows the document (with coverage and confidence), findings, graph
versions and diffs, and lets you comment on any graph item and resolve findings. 3. `resolve` (or the UI)
triggers targeted re-analysis -> new graph version -> `docs/changelog.md`. 4. The **Approval** tab shows readiness
(blockers, warnings), the approval state, a preserve/fix control for every quirk and suspected bug (also in the
item drawer), and an Approve button that stays disabled while blockers exist. Findings can also be answered
"Decide during migration (defer)".

Review server security: it binds **127.0.0.1 only** (refuses anything else), picks a random port unless
`--port` is given, and every request needs an unguessable token (URL path or `HttpOnly; SameSite=Strict`
cookie, constant-time compare). It validates the `Host` header (DNS-rebinding defence), rejects cross-origin POSTs,
requires `application/json`, caps bodies at 64 KB and texts at 5,000 characters, and exposes only comments,
resolutions, quirk decisions and approval for writing (plus, once Phase 2 files exist, Phase 2 comments, slice
approve/reject, fit-risk decisions and mask approvals; section 9.8). Do not forward the port. It cannot be used from inside a container; run it on the host.

## 7. Security model

* **Read-only on the source.** `ingest` copies; analysis reads only `code/`. Nothing from the source is executed.
* **Tool sandbox.** The model only gets `read_file`, `list_dir`, `grep`, `file_stat`, jailed to `code/` (symlinks resolved,
  `..` and NUL rejected, size caps, no write/exec/network capability exists).
* **Process guard.** An optional audit-hook tripwire blocks writes outside scratch space, process spawn and
  non-allowlisted sockets. It is defence in depth, not a boundary (see `src/migration_agent/sandbox/README.md`).
* **Container boundary.** The Docker image runs non-root with a read-only root filesystem, all capabilities
  dropped, and either no network or egress only through an allowlisting proxy to the model host.
* **Secrets.** Credential files are withheld at ingest and secret values are redacted from `code/`, reports, and user docs before they reach a prompt or `findings.json`.
  Findings contain kinds and redacted previews, never values. API keys are read from the environment and never written to the project folder or image.
* **Untrusted text.** Source code, user docs and reviewer notes are wrapped in delimiters that they cannot forge and
  are treated as data. A hand-built fixture in `eval/` checks prompt-injection canaries do not reach the graph.
* **User docs only to the Verifier.** The analysis never reads `user_docs/`, so the doc is derived from code alone; the Verifier
  never sees code. That separation is what makes disagreements meaningful.
* **Evidence.** A node without verifiable file/line/quote evidence has confidence `none`/`low` and is flagged in
  the document's blind-spots section.
* **Data leaves your machine only to the configured model endpoint.** Chunks of code go to that provider; use
  `--offline`, a self-hosted gateway (`MA_LLM_API_BASE`) or a no-retention provider plan if that matters.
* **Phase 2 changes two of these rules.** It executes the legacy service and the generated code, and it uses the
  original unredacted source (`legacy_src/`). Both happen only inside network-isolated exec containers; the model
  still sees only redacted text. Details in section 9.1.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No LLM API key found` | Export the key or put it in `.env` (section 1); or use `--offline`/`--dry-run`. |
| macOS `Operation not permitted` | The terminal lacks access to protected folders (Downloads, Desktop, Documents, external volumes). Grant the terminal app access in System Settings -> Privacy & Security (Files and Folders, or Full Disk Access), or keep the project and source elsewhere, e.g. `~/work`. Also appears when `.env` or the source lives in such a folder. |
| `429` / quota / `RESOURCE_EXHAUSTED` | Free tiers have per-minute and per-day limits that differ by model (lite models usually have the highest limits; larger "flash" models the lowest). The client retries with backoff up to `max_retries`; for free tiers use `gemini-lite.config.json`, raise `backoff_max`, or just re-run later: cache and checkpoints mean finished chunks are not repeated. |
| `503` / "model is overloaded" | Transient provider overload; retried automatically (5xx). If it persists after `max_retries`, the chunk is skipped (named in the log) and a re-run picks it up. Raise `MA_LLM_MAX_RETRIES` or switch model. |
| Run stopped on budget | `--max-usd` reached. Raise it and re-run; completed work is reused. |
| Many `unmapped` inventory items | Check `docs/understanding.latest.md` section 15 and `ingest_report.json` exclusions (large/binary/vendor files). |
| Low confidence / "blind spots" | Evidence quotes did not verify against `code/`; re-run, use a stronger `models.*` for that task, or comment on the item and `resolve`. |
| `not a project folder` | The folder lacks `project.json`; run `init` first. |
| `verify` reports nothing | `user_docs/` is empty or the formats are unsupported (`.pdf` needs `pypdf`, which is not installed by default). |
| `review` cannot be reached from a container | By design it binds to loopback; run it on the host. |

## 9. Phase 2: migrate and prove equivalence

Phase 2 turns the approved understanding into a new service and shows, case by case, that it behaves like the old
one. The running legacy service is the oracle: expected outputs are recorded from it, never written by the model
(except for behaviour you decided to `fix`, and only after you approve the corrected expectation).

Status: implemented and covered by offline unit and integration tests. It has run on a real Docker daemon on the three
eval fixtures (build, start, record, equivalence, fuzzing, the strangler router and three codemods), always with
`--offline`: **no stage has run with a real model yet**, so the quality of generated code is unmeasured. Treat the first
real runs on your own service as a trial. What has and has not run: [remaining-work.md](remaining-work.md). Design: [phase2-plan.md](phase2-plan.md); code map:
[phase2-architecture.md](phase2-architecture.md).

Phase 2 is CLI and self-hosted only. The hosted platform does not run it (section 9.11).

### 9.1 Trust change

Phase 1 promises that nothing from the source is executed and that the model sees only redacted text. Phase 2 keeps
the second promise and relaxes the first one, inside a boundary:

* **Code runs, but only in exec containers.** Legacy builds, the legacy service, generated code, codemods, test
  runners, the router and schema probes all run in disposable Docker containers started by `DockerBackend`
  (`migrate/exec/docker.py`): fixed argv, `--cap-drop ALL`, `no-new-privileges`, non-root user, read-only root
  filesystem, pids/memory/CPU/time limits, images never pulled, gVisor when installed. The host never imports or runs
  that code.
* **Test phase has no network.** Test jobs run with `--network none`, or on an `--internal` Docker network with no
  route out (harness services). Build jobs reach only the package-mirror hosts you configure, through an
  allowlisting proxy; with no mirror configured, builds are offline too.
* **`legacy_src/` holds the original, unredacted source.** Many legacy builds fail on redacted files, so
  `harness import-source` copies the original source into `legacy_src/`, after checking it against the contract
  `source_hash`. It is mounted only into exec containers. The model's read-only tools are rooted at `code/` (the
  redacted copy) and cannot reach `legacy_src/`. Credential files stay withheld (empty placeholders).
* **No real secrets in containers.** Job environment values that look like secrets are refused; required secrets get
  random synthetic values per run (`synthetic_secrets`). No model key, git or cloud credentials are passed.
* **Generated code is checked before it is committed.** Every model edit is path-jailed to `target_code/`, scanned
  for secrets, checked against the dependency allow/deny list and, after the build, reviewed by a fresh model
  session plus static deny checks (outbound HTTP to undeclared hosts, `eval`/`exec`, shell spawning, reading
  credential-like environment variables).

Threat model: [threat-model.md](threat-model.md) section 9.

### 9.2 Prerequisites

* An approved Phase 1 project: `migration-agent status PROJECT` shows `approved`, and
  `migration-agent contract check PROJECT` prints `contract: OK`.
* **Docker** (engine plus CLI on `PATH`) on the machine that runs Phase 2. Rootless Docker or Podman is preferred.
* **Images present locally.** Phase 2 never pulls images (`--pull never`, `docker build --pull=false`). Pull them
  yourself, pinned by digest where you can:
  * the legacy base image named in your harness Dockerfile (the fixtures use `node:20.18.0-bookworm`,
    `maven:3.9.9-eclipse-temurin-17`, `python:3.10.15-slim-bookworm`);
  * the target base image from `target.yaml` `target.base_image`;
  * `python:3.12.7-alpine3.20` (in-network case runner, router and schema probe);
  * `ubuntu/squid:6.6-24.04_beta` (egress proxy, only when a mirror is configured; override with `proxy_image`);
  * database and stub images named in `harness.yaml`;
  * codemod images when the codemods apply: `maven:3.9-eclipse-temurin-21` (OpenRewrite, Maven),
    `gradle:8.10-jdk21` (OpenRewrite, Gradle), `python:3.12-slim` (django-upgrade + pyupgrade), `node:20-alpine`
    (lebab, JavaScript).
* **Package mirror (optional).** List mirror hosts in `<project>/harness/mirror.json`:
  ```json
  {"hosts": ["nexus.corp.example"], "ports": [443], "runtime": "auto"}
  ```
  or set `MA_MIRROR_HOSTS=nexus.corp.example,pypi.corp.example` (overrides `hosts`). Without hosts, builds run with
  `--network none` and dependencies must come from the per-project cache (`harness/cache/`). A customer-controlled
  mirror is the strong option; public registries through the proxy are a weaker one.
* **gVisor (optional, recommended).** `runsc` is used automatically when Docker lists it. `MA_EXEC_RUNTIME=runsc`
  requires it (the backend reports unavailable otherwise); `MA_EXEC_RUNTIME=runc` disables it. The same setting is
  `runtime` in `mirror.json`.
* **git** on `PATH` (`target_code/` is a git repository).
* A model key as in section 1, unless you use `--offline` everywhere (offline runs produce placeholder output and
  are for trying the flow, not for real migrations).
* A project budget: `target init --budget-usd N`. Every model call in Phase 2 is capped by what remains of it,
  across sessions.

Check with:
```bash
migration-agent doctor orders.migration           # contract, exec sandbox, git, budget; exit 1 if any fails
```
`doctor` does not check that images are present or that the mirror is reachable; `harness build` reports those.

### 9.3 The harness spec (`harness/harness.yaml`)

The harness describes how to build and start the legacy service (and later the new one) in the sandbox. On the
first `harness build`, the tool copies it from `legacy_src/harness/harness.yaml` (or `harness.yaml.in`) into
`<project>/harness/harness.yaml` if you have not written one there. The eval fixtures ship examples in
`eval/repos/<repo>/harness/harness.yaml.in`. Main fields (full reference in the `migrate/harness.py` docstring):

```yaml
schema_version: 1
name: express-crud
legacy:
  image: ma-fixture/express-crud:legacy     # tag built from `dockerfile` with docker build --network none
  dockerfile: harness/Dockerfile            # relative to the source tree, else to <project>/harness/
  context: harness
  workdir: /work/app
  build:                                    # build-phase job in a writable copy of legacy_src/
    command: [sh, -c, "npm install --no-audit --no-fund --cache /cache --prefer-offline"]
    network: mirror                         # mirror hosts from mirror.json / MA_MIRROR_HOSTS
    cache: harness/cache/npm                # project-relative cache mounted at /cache
    timeout_s: 1800
  start: [node, src/server.js]              # test phase, internal network only
  port: 3000
  healthcheck: {path: /api/products}
  env: {PORT: "3000"}
  synthetic_secrets: [API_TOKEN]            # random value per `harness up`
  request_headers:                          # added to EVERY request the runner sends to this service
    X-API-Token: "{secret:API_TOKEN}"       # {secret:NAME} = the random value generated for NAME at `harness up`
db:
  engine: sqlite                            # sqlite | postgres | mysql | none
  # image, command, migrate, seed for server engines
seed_requests: harness/seed/requests.json   # HTTP seeds replayed after every `harness up` (in-memory databases)
stubs: []                                   # stub servers for external systems
# new: {...}        optional; default derived from target/target.yaml
# router: {image: "ma-router:1", port: 8080}
# schema_check: {...}                       # for `config schema-check`
```

**Authenticated services.** The sandbox generates a random value for each `synthetic_secrets` name every time the
harness starts. `request_headers` tells the case runner to send it: `{secret:NAME}` is replaced by that value, and the
header goes on every request (recording, seeds, equivalence, fuzzing, the composite). Without it a service that
requires a token answers `401` to everything, and you would be comparing two error pages. A case can override an
injected header with its own value (compared case-insensitively) or send none by giving the header an empty value
(`"headers": {"X-API-Token": ""}`), which is how a "missing token" case is written. The value never reaches a case
file or a report.

**Seeds for in-memory databases.** A service whose database lives in its process starts empty. `seed_requests` names
an HTTP seed file in the legacy source tree, `{"requests": [{"method", "path", "body"?, "headers"?, "query"?,
"expect_status"?: [codes]}]}` (up to 200). It is replayed after every `harness up`, for `legacy`, for `new`, and
directly against both services of the composite. A seed must answer below 400 (or one of its `expect_status`); a seed
that fails stops `harness up` and removes the containers, because every later case would be meaningless.

Timestamps: two ISO-8601 timestamps no more than 5 minutes apart compare equal everywhere (recording, equivalence,
fuzzing, the composite), because two instances seeded moments apart never agree on "now". Hours apart, or a zoned
timestamp against a naive one, is still a difference.

### 9.4 What Phase 2 writes in the project folder

| Folder | Written by | Content |
|---|---|---|
| `target/` | `target init`, `target fit`, `target resolve` | `target.yaml`, `fit_report.json`, `fit_report.md`, `fit_resolutions.json` |
| `plan/` | `plan` | `migration_plan.json`, `migration_plan.md`, `versions/vNNN.json` (immutable), `planner_report.json` |
| `legacy_src/` | `harness import-source` | original unredacted source (exec containers only) |
| `harness/` | `harness ...` | `harness.yaml`, `state.json`, `compose.<role>.yml` (for inspection), `legacy_src.json` (import manifest), `synthetic_secrets.json` (0600), `logs/`, `new/Dockerfile`, `router/`, `routes.json`, `flips.json` |
| `characterization/` | `tests ...` | `cases/SLICE-NNNN/CT-NNNN.json` (or `cases/UNPLANNED/` before a plan exists), `hypotheses.json`, `untestable.json`, `testgen_report.json`, `coverage.json`, `recordings/RUN-NNNN/`, `results/RUN-NNNN/` |
| `target_code/` | `generate`, codemods, you | the new service, a git repo; tags `slice/SLICE-NNNN/iter-K` and `slice/SLICE-NNNN/approved` |
| `slices/SLICE-NNNN/` | `plan`, `generate`, `equivalence`, approvals | `state.json`, `iterations/K/*.json`, `review.json`, `decisions.json`, `equivalence.json`, `equivalence_run.json`, `fix_requests.json`, `approval.json` |
| `runs/RUN-NNNN/` | every exec job | `meta.json` (job + result, image digest), `logs/` (capped, redacted), `reports/`, `work/` |
| `migration/` | several | `ids.json`, `comments.json`, `runtime_findings.json`, `codemods.json` |
| `config/` | `config map`, `config set` | `config_map.json` (no values), `config_map.md`, `decisions.json`, `generated/` |
| `data/` | you, `config schema-check` | optional `schema_changes.yaml` and `mapping.yaml`; `schema_compat.json`, `schema_compat.md` |
| `equivalence/` | `equivalence ...` | `report.md`, `masks.json`, `composite.json`, `composite_history.jsonl` |
| `cutover/` | `cutover` | `plan.md`, `approval.json`, `hashes.json` |

All writes are atomic. Every command can be re-run; the folder is the state.

### 9.5 Commands in order

Every Phase 2 command that writes runs the contract gate first. If the graph was re-analysed after approval, approve
again, or pass `--pin-contract` to work from the approved snapshot (a warning). `--accept-unmapped` accepts unmapped
inventory items (logged in the audit). Run `migration-agent COMMAND --help` for the full option list.

**1. Check the entry gate.**
```bash
migration-agent contract check orders.migration          # exit 1 on any error; lists deferred findings as blockers
migration-agent contract schema --out contract.schema.json
migration-agent doctor orders.migration
```

**2. Describe the target.** `target init` detects the source stack from the manifests and takes the target from
flags. The kind is inferred (`version_upgrade`, `framework_move`, `language_move`) unless you give `--kind`; the
strategy defaults to `in_place` for version upgrades and `strangler` otherwise.
```bash
migration-agent target init orders.migration --runtime 21 --framework spring-boot@3.3 --build maven \
  --base-image eclipse-temurin:21-jre@sha256:<digest> --budget-usd 25
migration-agent target show orders.migration             # validate and print target/target.yaml
```

**3. Target-fit analysis.** A deterministic layer matches the libmap tables (Java 8 to 21, Spring Boot 2 to 3, Django
3 to 5, Express to Fastify, JS to Go) against dependencies, code lines and config keys. A model layer adds risks
whose evidence quotes must verify against `code/`; unverified risks are dropped and counted.
```bash
migration-agent target fit orders.migration --max-usd 2          # or --offline: deterministic layer only
migration-agent target resolve orders.migration RISK-0003 --status mitigate --note "Replace springfox with springdoc"
```
Statuses: `accepted`, `mitigate`, `wont_fix`. Open high-severity risks become blockers on the slices they touch.

**4. Plan.** The planner orders slices from the approved graph: foundation slices (build, config, cross-cutting, data,
shared code) first, then entry slices (endpoint groups, jobs, consumers), then bootstrap. Every in-scope node lands
in exactly one slice. `dead_code` nodes are listed, not migrated, unless `--include-dead-code`.
```bash
migration-agent plan orders.migration --dry-run          # print, write nothing
migration-agent plan orders.migration                    # writes plan/ and slices/*/state.json (planned)
```
`--name-with-model` adds an optional naming pass; `--max-slice-tokens N` splits large slices (default 60000).
Re-planning keeps slice ids for the same node sets.

**5. Import the original source and start the legacy harness.**
```bash
migration-agent harness import-source orders.migration --from-original   # or --from PATH; checked against source_hash
migration-agent harness build orders.migration           # legacy is the default role
migration-agent harness up orders.migration              # waits for the health check
migration-agent harness status orders.migration
```
`import-source` refuses a source whose hash differs from the contract. A contract without `source_hash` needs
`--accept-unpinned` (audited). `--force` replaces an existing `legacy_src/`.

**6. Characterization tests.** `tests generate` proposes request inputs per endpoint, job, business rule, quirk and
suspected bug. The model never writes expected values for preserved behaviour.
```bash
migration-agent tests generate orders.migration --max-usd 3       # or --offline
migration-agent tests record orders.migration --harness           # every case twice against the running legacy harness
migration-agent tests coverage orders.migration --fail-on-gaps
migration-agent tests mark-untestable orders.migration JOB-0002 --reason "Needs the mainframe feed" --by alice
migration-agent tests approve-spec orders.migration CT-0014 --by alice     # fix-decision cases only
```
Recording runs each case twice. Fields that differ between the two runs become masks; secret-looking values are
masked too. Outcomes: `recorded`, `harness_issue` (no response; fix the harness), `unstable` (status differs),
`not_implemented` (gRPC and message cases are not supported yet). Instead of `--harness` you can point
`--base-url` (and `--second-base-url`) at a legacy instance you run yourself. Recordings that contradict a business
rule, or consistent 5xx answers, become runtime findings in `migration/runtime_findings.json`.

**7. Generate.** The generation loop works per slice, in plan order: context pack, model edits, validation, commit,
build in the sandbox, unit tests and the characterization suite against the new service (in-network), a fresh review,
then `awaiting_human`. Failures go back into a fix iteration until `--max-iterations` (default 4), then the slice is
`blocked` with a reason.
```bash
migration-agent generate orders.migration --all --dry-run         # per-slice token and USD estimate
migration-agent generate orders.migration --slice SLICE-0001 --max-usd 5
migration-agent generate orders.migration --all --max-usd 20 --max-usd-per-slice 4
```
The loop refuses blocked slices, slices with unaccepted blockers, slices whose dependencies are not approved, entry
slices without recorded cases, and a `target_code/` with uncommitted edits. For `in_place` version upgrades, the
codemods (OpenRewrite for Maven projects, django-upgrade + pyupgrade) run once in the sandbox before the first slice.
A run can be killed and resumed: each step is a file under `slices/SLICE-NNNN/iterations/K/`, so at most one model
call is lost.

**8. Equivalence.** Runs the slice's cases, plus every case of earlier approved slices, against legacy and new, and
classifies each difference as `match`, `noise`, `intended_divergence`, `regression` or `unknown`. Exit 1 when any
regression or unknown diff remains.
```bash
migration-agent harness build orders.migration --new && migration-agent harness up orders.migration --new
migration-agent equivalence orders.migration --slice SLICE-0001             # in-network against both harnesses
migration-agent equivalence orders.migration --all --triage --max-usd 1     # cheap model explains unknown diffs
migration-agent equivalence orders.migration --slice SLICE-0001 --legacy-url http://127.0.0.1:8080 --new-url http://127.0.0.1:8081
```
`equivalence PROJECT ...` is the same as `equivalence run PROJECT ...`. In harness mode both services are restarted
first, which re-runs the seeds, so each starts from the same data (`--no-reset` keeps their state; with
`--legacy-url`/`--new-url` nothing is restarted). Results: `slices/SLICE-NNNN/equivalence.json` and
`equivalence/report.md`, which also carries an advisory **latency** summary (p50 and p95 per side, and the cases where
the new service is more than x2 and 50 ms slower; sandbox timings are noisy, so it is a smoke signal, not a
benchmark). Regressions and `new_bug` triage become fix requests that the next `generate` run
picks up. A `legacy_bug` becomes a decision for you; a `harness_issue` becomes a mask proposal; a `spec_ambiguity`
blocks the slice until answered.

**Differential fuzzing (advisory).** Recorded cases only cover what was recorded. `equivalence fuzz` applies a fixed set
of mutations to each recorded HTTP case (empty, long, numeric-edge and unicode query values; JSON fields set to null,
a wrong type, empty or removed; another body shape; an unknown field or parameter), asks legacy twice so its own noise
is masked, and reports where the new service answers differently.
```bash
migration-agent equivalence fuzz orders.migration --slice SLICE-0001 --max-variants 8   # exit 1 on a high-severity divergence
```
Results: `equivalence/fuzz/SLICE-NNNN.json` and `equivalence/report.md`. A divergence is `high` when one side answers
5xx and the other does not, or the status class differs. It never blocks `approve-slice`; `cutover` lists it as a
warning. It mutates only fields that already exist in recorded bodies, so it will not invent a boundary the cases never
reached.

**9. Review and approve each slice** (section 9.6 and 9.8).

**10. Config and schema.**
```bash
migration-agent config map orders.migration              # writes config/ and generated files in target_code/
migration-agent config set orders.migration DB_URL --status renamed --target-key spring.datasource.url
migration-agent config set orders.migration LEGACY_FLAG --status dropped --reason "Feature removed in 2019"
migration-agent config schema-check orders.migration     # sandbox; --static compares Phase 1 facts without Docker
migration-agent config schema-blockers orders.migration
```
Secrets map to references only (`${NAME}` or a required env var) plus a deploy-time checklist; values are never
read or written. The default is no schema change (decision P2-Q10). Declared changes go in
`data/schema_changes.yaml` and are applied only to the ephemeral sandbox database.

**11. Strangler routing** (strategy `strangler`). The composite harness runs legacy, new and a router on one
internal network. Flipping a slice sends its endpoints to the new service; everything else stays on legacy.
```bash
migration-agent harness build orders.migration --composite && migration-agent harness up orders.migration --composite
migration-agent harness flip orders.migration SLICE-0003 --by alice --reason "approved"
migration-agent harness routes orders.migration
migration-agent equivalence composite orders.migration
migration-agent harness unflip orders.migration SLICE-0003 --by alice --reason "rollback test"
```
Only approved slices can be flipped. `--force-for-testing` flips an unapproved slice for sandbox testing and is
audited. When the composite is up, `flip` and `unflip` run the composite suite afterwards (`--no-verify` skips it).
With `db.engine` `sqlite` or `none`, each service keeps its own private database in the composite
(`db_shared: false`), so the composite does not test a shared database in that case.

**12. Cut-over.**
```bash
migration-agent cutover orders.migration                 # readiness + cutover/plan.md; exit 1 while not ready
migration-agent cutover orders.migration --approve --by alice --note "Go for Tuesday"
```

**13. Metrics.**
```bash
migration-agent metrics orders.migration                 # or --format json
```

**Harness clean-up:** `migration-agent harness down PROJECT [--legacy|--new|--composite]`.

### 9.6 Blockers

A slice cannot be generated or approved while it has open blockers. `approve-slice` lists every one.

| Blocker | Where it comes from | How to clear it |
|---|---|---|
| `F-NNNN` deferred Verifier finding | contract `deferred_findings` touching the slice's items | `migration-agent slice accept-observed PROJECT SLICE-NNNN F-NNNN --by NAME --note "..."` (migrate as observed), or resolve it in Phase 1 and approve again |
| `RISK-NNNN` | open high-severity fit risk on the slice | `migration-agent target resolve PROJECT RISK-NNNN --status ... --note ...` |
| Open `legacy_bug` decision | equivalence triage | `migration-agent slice decide PROJECT SLICE-NNNN EQ-NNNN --choice preserve|fix --by NAME` |
| Open `spec_ambiguity` question (slice `blocked`) | equivalence triage | `slice decide ... --choice answered --by NAME --note "..."`; the slice returns to its previous status once no question is open |
| Pending fix request | equivalence regression or `new_bug` | `migration-agent generate PROJECT --slice SLICE-NNNN`, then `equivalence` again |
| Unapproved mask proposal | `harness_issue` triage | `migration-agent equivalence approve-mask PROJECT EQ-NNNN --by NAME --reason "..."` (applies from the next run) |
| Unapproved specified expectation | fix-decision case | `migration-agent tests approve-spec PROJECT CT-NNNN --by NAME` |
| Schema blind spot | stored procedure / trigger not in the repo, or a `blind_spots:` table | supply a schema export (`schema_export:` in `data/schema_changes.yaml`) or list it under `accepted:` |

### 9.7 Slice approval and human edits

`migration-agent approve-slice PROJECT SLICE-NNNN --by NAME [--note TEXT]` is refused unless: the contract gate
passes; the slice is `awaiting_human`; the latest build is green; the latest suite is green except approved intended
divergences; the latest equivalence report has no `regression` or `unknown` diff; `target_code` HEAD has not changed
since that equivalence run; all blockers are cleared. On success it writes `slices/SLICE-NNNN/approval.json` (who,
when, commit, report hashes) and tags `slice/SLICE-NNNN/approved` in `target_code/`.
`migration-agent reject-slice PROJECT SLICE-NNNN --by NAME --reason TEXT` rejects a slice awaiting review.

You may edit `target_code/` yourself and commit with git. A commit after the equivalence run (yours or the agent's)
makes approval wait for a new equivalence run. The agent never overwrites uncommitted edits: `generate` refuses a
dirty tree. Human commits count in the human-edit metric.

Reviewer findings: mark each finding the reviewer raised as a real problem or as noise with
`migration-agent slice review-finding PROJECT SLICE-NNNN K:I (--accept|--dismiss) --by NAME [--note T]` (`K` is the
iteration, `I` the finding's position; the Slice tab has the same buttons). Your verdicts are stored in
`slices/SLICE-NNNN/review_marks.json` and are what `metrics` uses for reviewer precision.

Comments: `migration-agent slice comment PROJECT ITEM_ID --text "..." [--file F --line N] [--by NAME]`, where
`ITEM_ID` is a `SLICE-`, `CT-`, `EQ-`, `RISK-` or graph id and `--file` a `target_code`-relative path.
`migration-agent generate PROJECT --slice SLICE-NNNN --from-comments` runs a fix iteration with the unprocessed
comments and reopens `awaiting_human`, `rejected` or `approved` slices.

### 9.8 Review UI for Phase 2

`migration-agent review PROJECT` shows extra tabs once Phase 2 files exist (`target/fit_report.json`,
`plan/migration_plan.json` or `slices/`): **Target fit** (risks, resolve), **Plan** (slices, status, estimate,
equivalence %), **Slice** (state, iterations, build and test results, git diff, review findings, equivalence diffs,
blockers, approve and reject buttons, mask approvals, Accept/Dismiss buttons on each reviewer finding), **Cut-over**
(every readiness check with its detail, warnings, the approval and whether its hashes still match, and an approve
button that stays disabled until the project is ready) and **Comments**. The same loopback, token and Origin checks
apply.

### 9.9 Cut-over

`cutover` checks readiness and (re)writes `cutover/plan.md`. Every failing check is a blocker: contract gate; a valid
plan; every slice `approved` with a valid approval file; every slice's equivalence report free of regression and
unknown diffs; a full-suite run on the current `target_code` HEAD that covers every case of the plan (the composite
report for strangler projects, else the newest equivalence run covering all slices); a clean `target_code/`; no
coverage gaps; no open config entries; no schema blockers and no incompatible schema report; findings and risks
accepted; spend within the project budget.

Warnings (never blockers): differential-fuzzing divergences, cases the new service answers much slower than legacy,
and supply-chain findings from the last build of each service. Every `harness build` writes
`runs/RUN-NNNN/supply_chain.json` and `sbom.cdx.json` (CycloneDX 1.5) with the sha256 of each lockfile, and warns about
a manifest with no lockfile, a lockfile the build generated instead of the source containing it (commit it, or the next
build may resolve other versions) and unpinned dependencies. The SBOM lists what the lockfiles name; a package whose hash
differs from the lockfile is not refused (the sandbox only allows the mirror hosts you configured).

`cutover/plan.md` is deterministic: route-flip order, rollback per step, config and secret checklist (names only),
schema steps, monitoring per endpoint, open risks. `cutover --approve --by NAME` re-checks readiness and writes
`cutover/approval.json` with the sha256 of the contract and the plan, the `target_code` HEAD (or a tree hash) and a
digest over every equivalence artifact (`cutover/hashes.json`). A later change to any of them makes the approval
stale; `cutover` reports it.

The tool does not deploy anything. The cut-over plan is for your team to execute.

### 9.10 Metrics

`metrics` derives everything from the audit log, slice files, recordings and `target_code` git history: cost per
slice, per node and per 1k legacy lines; cache hit rate; iterations to green; build success on the first try;
characterization pass rate; equivalence %; unknown-diff rate; mask additions; human-edit rate; wall-clock split model
vs exec; runtime findings. A metric without data shows `n/a`, never 0. Reviewer precision is the share of reviewer
findings you marked that you accepted (`slice review-finding`, or the Slice tab); it is `n/a` until you mark some.

### 9.11 Hosted platform

Phase 2 runs only from the CLI on a machine you control (decision P2-Q13). The hosted worker in `backend/` runs only
the Phase 1 stages (ingest, extract, analyze, verify, resolve) and must not run Phase 2 exec: it holds database
credentials and is not an isolated job runner. `migration_agent.service.run_stage` exposes Phase 2 stages for
self-hosted integrations only ([platform-contract.md](platform-contract.md) section 2).

### 9.12 Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `contract gate: ...` | The contract is stale or missing. Run `contract check`; re-approve, or use `--pin-contract` |
| `doctor`: exec sandbox fail | Docker CLI missing, daemon not running, or `MA_EXEC_RUNTIME=runsc` without gVisor installed |
| `harness build` cannot find an image | Images are never pulled. `docker pull` the image named in the error (pin by digest) |
| Build fails resolving dependencies | No mirror configured (offline build). Add hosts to `harness/mirror.json` or `MA_MIRROR_HOSTS`, or pre-fill `harness/cache/` |
| `import-source` refuses the source | Its hash differs from the contract `source_hash`: use the exact source that was approved |
| Many `harness_issue` recordings | The legacy service did not answer: check `harness status`, `harness/logs/`, the health check and seed data |
| `unstable` recordings | The status differs between two runs (clock, randomness, order). Stabilise seeds or mark the node untestable |
| `generate` refuses a slice | Read the reason: blockers, dependencies not approved, no recorded cases, or uncommitted edits in `target_code/` |
| Characterization suite `skipped` | The new service could not be built or reached in the sandbox; the slice stays `built`. Check `runs/RUN-NNNN/logs/` |
| Slice `blocked` after the iteration cap | Read `slices/SLICE-NNNN/iterations/K/outcome.json`; add a comment and use `--from-comments`, or edit and commit yourself |
| `approve-slice` refused after an edit | HEAD changed since the last equivalence run: run `equivalence` again |
| Budget stop | The run or slice cap was reached. Raise `--max-usd` / `--max-usd-per-slice` or `budget_usd` in `target.yaml`, then re-run |
