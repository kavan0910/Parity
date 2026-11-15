"""FakeExecBackend: scripted ExecResults for tests (no containers, no subprocesses).

    fake = FakeExecBackend(project, script=[FakeOutcome(exit_code=0, stdout="BUILD OK"),
                                            FakeOutcome(exit_code=1, stderr="1 test failed",
                                                        reports={"junit.xml": "<testsuite .../>"})])
    res = fake.run(job)            # writes runs/RUN-NNNN/ exactly like a real backend (record_run)
    fake.jobs                      # every ExecJob received, in order

`script` is a list consumed in order (the last entry repeats), or a callable `job -> FakeOutcome`.
With `project=None` nothing is written and refs stay empty.
"""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from ..models import ExecJob, ExecResult
from .base import record_run


class FakeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.01
    timed_out: bool = False
    oom_killed: bool = False
    reports: dict[str, str] = Field(default_factory=dict)
    image_digest: str = "sha256:" + "0" * 64
    error: str = ""


class FakeExecBackend:
    name = "fake"

    def __init__(self, project=None, script: list[FakeOutcome] | Callable[[ExecJob], FakeOutcome] | None = None,
                 available: tuple[bool, str] = (True, "")):
        self.project = project
        self.script = script if script is not None else [FakeOutcome()]
        self._available = available
        self.jobs: list[ExecJob] = []

    def available(self) -> tuple[bool, str]:
        return self._available

    def _next(self, job: ExecJob) -> FakeOutcome:
        if callable(self.script):
            return self.script(job)
        if not self.script:
            return FakeOutcome()
        i = min(len(self.jobs) - 1, len(self.script) - 1)
        return self.script[i]

    def run(self, job: ExecJob) -> ExecResult:
        self.jobs.append(job)
        if not self._available[0]:
            o = FakeOutcome(exit_code=None, error=self._available[1] or "backend unavailable")
        else:
            o = self._next(job)
        if self.project is None:
            return ExecResult(run_id=job.run_id, exit_code=o.exit_code, duration_s=o.duration_s,
                              timed_out=o.timed_out, oom_killed=o.oom_killed, stdout_tail=o.stdout[-4096:],
                              stderr_tail=o.stderr[-4096:], image_digest=o.image_digest, error=o.error)
        return record_run(self.project, job, exit_code=o.exit_code, stdout=o.stdout, stderr=o.stderr,
                          duration_s=o.duration_s, timed_out=o.timed_out, oom_killed=o.oom_killed,
                          reports=dict(o.reports), image_digest=o.image_digest, error=o.error)
