# Sandbox baseline (A-09)

Two layers. Be clear about which one you are relying on.

## Layer 1: container (the real boundary) - `docker/`

| Control | Mechanism |
|---|---|
| Code is read-only | repo mounted `:ro` at `/workspace`; image code is root-owned; `--read-only` root fs |
| Scratch only | `tmpfs` on `/tmp` and `/scratch` (noexec, size-limited) |
| No network by default | `docker/run.sh` uses `--network none` (compose profile `offline`: `network_mode: none`) |
| Model-endpoint-only egress | compose profile `model`: worker sits on an `internal: true` network (no route out). The only other member is an allowlisting Squid proxy (`docker/squid.conf`) that permits HTTPS `CONNECT` to the single model host and denies everything else. Worker gets `HTTPS_PROXY`. |
| Privileges | `cap_drop: ALL`, `no-new-privileges`, non-root uid 10001, pids/memory limits |
| Credentials | none in the image; `ANTHROPIC_API_KEY` is passed at runtime and only in `model` mode |

Caveats: the proxy allowlist is by domain name (SNI/CONNECT host), so DNS or the
model host itself could be abused as a channel; pin to the narrowest host. The
proxy container itself needs `SETUID/SETGID` and is a trusted component.
`network_mode: none` and `networks:` are mutually exclusive, hence two services.
This was not exercised in CI: Docker is not available on the dev machine used
to author it, so treat the Docker files as reviewed-but-untested.

## Layer 2: in-process (defense in depth) - `guard.py`, `tools.py`

`ReadOnlyTools(root)` (tools.py): the agent-facing tool surface. Methods:
`read_file`, `list_dir`, `grep`, `file_stat`. Paths are resolved with symlinks
followed and must remain under `root`; `..`, NUL bytes, outside absolute paths
and escaping symlinks raise `SandboxViolation`. Size/line/match caps, binary
files refused. There are no write/exec/network methods. This is enforced by
construction (the capability doesn't exist), not by a filter.

`enforce_readonly_process(SandboxPolicy(allowed_hosts, write_dirs))` (guard.py):
call once at worker start. Irreversible for the process lifetime.
- Sockets: `socket.connect/connect_ex/create_connection` patched, plus audit events
  `socket.connect/sendto/sendmsg/getaddrinfo/gethostbyname` -> only allowlisted hosts
  (names are pre-resolved at activation). AF_UNIX is blocked.
- Writes: audit `open` with write flags/modes outside `write_dirs`; `os.remove/rename/
  mkdir/rmdir/symlink/link/truncate/chmod/chown/utime` and `shutil.*` mutators.
- Process spawn: `subprocess.Popen`, `os.system`, `os.exec*`, `os.spawn*`,
  `os.posix_spawn`, `os.fork`.
- Violations raise `GuardViolation` (a `PermissionError`).

### What the in-process guard does NOT stop
- Code in the same interpreter can use `ctypes`/cffi/native extensions or raw
  syscalls to bypass Python-level hooks (audit hooks can't be removed, but they only see
  what CPython reports). A malicious in-process payload is out of scope.
- Preexisting open file descriptors/sockets are not re-checked.
- Writes through already-open writable fds, `mmap`, or paths via `/proc/self/fd` tricks.
- Allowlist is by host, not port/protocol; DNS names are matched as strings/pre-resolved IPs.
- Reads are not restricted by the guard (only `ReadOnlyTools` jails reads).

Therefore: treat the guard as a tripwire that catches bugs and naive misuse of
tools; rely on the container for isolation from untrusted repositories.

## Layer 3: Phase 2 exec sandbox - `migrate/exec/docker.py`, `migrate/exec/policy.py`

Phase 2 builds and runs the legacy service and generated code. That code is hostile, so it only ever runs
in disposable containers started by `DockerBackend` (ExecBackend contract in `migrate/exec/base.py`); the
orchestrator never imports or executes it. Threat model section 9 (T19-T27).

| Control | Mechanism |
|---|---|
| Fixed argv, no host shell | `docker run ...` built as a list, `shell=False`; job command passed after the image (`--entrypoint argv[0]`) |
| No network in tests | test phase: `--network none`; harness services: a backend-created `--internal` network (verified before attach) |
| Mirror-only builds | build phase + mirror hosts (`harness/mirror.json` or `MA_MIRROR_HOSTS`): per-run `--internal` network + Squid allowlist proxy (same pattern as `docker/squid.conf`, rendered per run, egress log kept as a report). No hosts configured: build is offline |
| Privileges | `--cap-drop ALL`, `no-new-privileges`, non-root `--user`, `--read-only` + tmpfs, `--runtime runsc` when installed (`MA_EXEC_RUNTIME=runsc` requires it) |
| Limits | `--pids-limit`, `--memory`/`--memory-swap`, `--cpus`, tmpfs `size=disk_mb`, wall-clock timeout then `docker kill` |
| Mounts | only project-relative paths under `legacy_src`, `target_code`, `runs`, `harness`, `characterization`, resolved through symlinks; never the docker socket, home or credential folders |
| Secrets | docker CLI gets an allowlisted env (never the model key); job env refused for secret-looking names/values and credential families; app secrets are `synthetic_secrets` (random per run, passed by name only, scrubbed from logs) |
| Images | never pulled (`--pull never`), run by image ID, digest recorded in `runs/RUN-NNNN/meta.json` |
| Results | `record_run` -> `runs/RUN-NNNN/` (capped, redacted logs; reports copied without following symlinks) |

`policy.scan_job` / `policy.scan_argv` run before every container start (red-team tests in
`tests/redteam/test_p2_exec_sandbox.py`).

`legacy_src/` (original, unredacted source, `parity harness import-source`) is the one exception to
"only redacted text leaves ingest": it is verified against the contract `source_hash` and mounted only into exec
containers. `ReadOnlyTools` is rooted at `code/`, so agent tools cannot reach it (`..`, absolute paths and
escaping symlinks are refused; `test_legacy_src_is_unreachable_through_readonly_tools`).

Harness (`parity harness build|up|down|status PROJECT [--legacy|--new|--composite]`, `migrate/harness.py`)
drives this backend from `harness/harness.yaml` and writes `harness/compose.<role>.yml` with the same settings.
`--new` builds and runs the generated service from `target_code/` (a `new:` section, or defaults derived from
`target/target.yaml`: build-tool commands and a `harness/new/Dockerfile` with no RUN steps). `--composite` is the
strangler mode: legacy, new and a router container (`migrate/router/`: stdlib Python proxy, built with
`--network none`, read-only, non-root, no published ports) on one `--internal` network; server-engine databases
are shared, sqlite/none stay private per service (reported as `db_shared: false`). The router mounts
`harness/router/live/` read-only and routes the endpoints of flipped slices to `new`, everything else to `legacy`
(`harness flip|unflip PROJECT SLICE-NNNN`; unapproved slices only with `--force-for-testing`, audited as
`harness_flip_forced`). `config schema-check` (`migrate/schemacompat.py`) reuses the same jobs: legacy migrations
on an ephemeral DB, the schema probe in the router image, and the ORM check in the new image, all test phase with
no network.

Caveat, as for layer 1: Docker was not available on the authoring machine. The backend is tested against a
scripted docker CLI; live tests (network canary, fork bomb, disk fill, read-only rootfs) skip when `docker info`
fails.

## Tests
`tests/test_sandbox_tools.py`, `tests/test_sandbox_guard.py` (guard tests run in
subprocesses so audit hooks don't leak into pytest). No Docker is required.
Phase 2 exec: `tests/test_p2_exec_docker.py`, `tests/test_p2_harness.py`,
`tests/redteam/test_p2_exec_sandbox.py` (scripted docker CLI; live tests need a daemon).
