"""Sandbox execution interface (docs/phase2-plan.md section 6). Interface only: backends implement it.

Contract every ExecBackend MUST honour (the Docker backend is migrate/exec/docker.py):

* One `ExecJob` = one disposable container. `job.command` is an argv list, never a shell string.
* Network (`job.network`):
    - "none":   no network interface except loopback. Mandatory for phase "test" (ExecJob validates this).
    - "mirror": phase "build" only; egress ONLY to the configured package-mirror hosts through the allowlisting
                proxy (docker/ Squid pattern). Never the open internet.
* Limits (`job.limits`): cpus, memory_mb, pids, timeout_s (wall clock; kill on expiry, set `timed_out`),
  disk_mb (workspace quota). Also always: cap_drop ALL, no-new-privileges, non-root uid, read-only rootfs plus
  tmpfs, no docker socket, gVisor (runsc) when available.
* Mounts: only the job's own project-relative mounts (`job.mounts`, validated: no absolute host paths, no
  `..`). `legacy_src/` (unredacted) may only be mounted into jobs with network "none" or "mirror", never
  into anything that talks to a model.
* No secrets: `job.env` is validated against ingest/secrets.py patterns; names in `job.synthetic_secrets` are
  filled by the backend with fresh random values per run. No model key, git or cloud credentials, ever.
* Results are FILES under runs/RUN-NNNN/ (use `record_run`):
      runs/RUN-NNNN/meta.json          {"job": ExecJob, "result": ExecResult}
      runs/RUN-NNNN/logs/stdout.txt    capped (LOG_CAP_BYTES) and redacted
      runs/RUN-NNNN/logs/stderr.txt
      runs/RUN-NNNN/reports/<name>     JUnit XML / JSON results copied out of the container
  `ExecResult.*_ref` and `reports` hold project-relative paths; `*_tail` hold the last TAIL_BYTES, redacted.
  Callers feed tails to a model only delimited and neutralised (analyze/module_analyzer.neutralize).
* `run()` never raises for job failures: a non-zero exit, timeout or OOM is a normal ExecResult. Backend
  problems (runtime missing, image pull refused) return `exit_code=None` and `error=...`.
* `available()` -> (ok, reason): cheap, no side effects, used by `parity doctor`.
* Every run is audited (agent "exec", action "exec_run": run_id, image, digest, argv, exit code, duration).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ...ingest.secrets import redact_string
from ..models import ExecJob, ExecResult

LOG_CAP_BYTES = 1_000_000
TAIL_BYTES = 4096


@runtime_checkable
class ExecBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """(True, "") when jobs can run; otherwise (False, human-readable reason)."""

    def run(self, job: ExecJob) -> ExecResult:
        """Run one job to completion and return its result (see module docstring)."""


def cap_redact(text: str | bytes, cap: int = LOG_CAP_BYTES) -> tuple[str, bool]:
    """(redacted text capped to `cap` bytes keeping the END, truncated?)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    b = text.encode("utf-8")
    truncated = len(b) > cap
    if truncated:
        text = "[... truncated ...]\n" + b[-cap:].decode("utf-8", errors="ignore")
    return redact_string(text), truncated


def _tail(text: str) -> str:
    b = text.encode("utf-8")
    return b[-TAIL_BYTES:].decode("utf-8", errors="ignore") if len(b) > TAIL_BYTES else text


def record_run(project, job: ExecJob, *, exit_code: int | None, stdout: str | bytes = "",
               stderr: str | bytes = "", duration_s: float = 0.0, timed_out: bool = False,
               oom_killed: bool = False, reports: dict[str, str | bytes] | None = None, image_digest: str = "",
               started_at: str = "", finished_at: str = "", error: str = "") -> ExecResult:
    """Write a run's files under runs/RUN-NNNN/ and return its ExecResult (shared by every backend)."""
    from ...audit import AuditLog
    from ..paths import Phase2Layout
    from ..state import atomic_write_json, atomic_write_text, now_iso

    lay = Phase2Layout(project)
    lay.ensure(lay.run_dir(job.run_id))
    out, t1 = cap_redact(stdout)
    err, t2 = cap_redact(stderr)
    so = atomic_write_text(lay.run_logs(job.run_id) / "stdout.txt", out)
    se = atomic_write_text(lay.run_logs(job.run_id) / "stderr.txt", err)
    rep_refs: list[str] = []
    for name, content in sorted((reports or {}).items()):
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"invalid report name {name!r}")
        p = lay.run_reports(job.run_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            atomic_write_text(p, content)
        rep_refs.append(lay.rel(p))
    now = now_iso()
    res = ExecResult(run_id=job.run_id, exit_code=exit_code, duration_s=max(0.0, duration_s), timed_out=timed_out,
                     oom_killed=oom_killed, stdout_ref=lay.rel(so), stderr_ref=lay.rel(se),
                     stdout_tail=_tail(out), stderr_tail=_tail(err), truncated=t1 or t2, reports=rep_refs,
                     image_digest=image_digest, started_at=started_at or now, finished_at=finished_at or now,
                     error=redact_string(error))
    atomic_write_json(lay.run_meta(job.run_id), {"job": job.model_dump(mode="json"),
                                                 "result": res.model_dump(mode="json")})
    AuditLog(Path(project.root) / "audit" / "audit.jsonl").record(
        "exec", "exec_run", run_id=job.run_id, phase=job.phase, network=job.network, image=job.image,
        image_digest=image_digest, argv=job.command, exit_code=exit_code, duration_ms=round(duration_s * 1000, 1),
        timed_out=timed_out, error=res.error[:300], labels=job.labels)
    return res


def load_run(project, run_id: str) -> tuple[ExecJob, ExecResult]:
    import json
    from ..paths import Phase2Layout
    d = json.loads(Phase2Layout(project).run_meta(run_id).read_text())
    return ExecJob.model_validate(d["job"]), ExecResult.model_validate(d["result"])


class UnavailableBackend:
    """Placeholder when no real backend is installed; every run returns an error result."""
    name = "unavailable"

    def __init__(self, project=None, reason: str = "no exec backend installed"):
        self.project, self.reason = project, reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def run(self, job: ExecJob) -> ExecResult:
        if self.project is not None:
            return record_run(self.project, job, exit_code=None, error=self.reason)
        return ExecResult(run_id=job.run_id, exit_code=None, error=self.reason)
