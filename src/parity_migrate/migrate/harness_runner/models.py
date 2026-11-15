"""Runner-local models (not shared contracts): runner configuration, an observed response, field diffs and
the per-case result. Shared contracts (CharacterizationCase, Expectation ...) live in migrate/models.py."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_HEADER_ALLOWLIST: tuple[str, ...] = ("content-type", "location", "www-authenticate", "content-disposition",
                                             "allow", "retry-after")
DEFAULT_JOB_ENDPOINT = "/__harness/job"

Outcome = Literal["pass", "fail", "error", "not_implemented", "unrecorded"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunnerConfig(_Strict):
    """How the runner reaches the service under test. Only the host:port of `base_url` is ever contacted."""
    base_url: str = Field(min_length=1)
    timeout_s: float = Field(default=10.0, gt=0, le=600)
    header_allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_HEADER_ALLOWLIST))
    # job trigger: POST {"job": name, "args": [...]} to this path on the base host; the harness answers
    # {"exit_code": int, "output": any}. None disables job cases (error result, triaged harness_issue).
    job_endpoint: str | None = DEFAULT_JOB_ENDPOINT
    follow_redirects: bool = False     # False: 3xx is recorded as-is (Location is in the allowlist)
    max_redirects: int = Field(default=5, ge=0, le=20)
    max_body_bytes: int = Field(default=1_000_000, ge=1)
    extra_headers: dict[str, str] = Field(default_factory=dict)   # e.g. a harness clock/seed header

    @field_validator("header_allowlist")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return sorted({h.strip().lower() for h in v if h.strip()})


class Observed(_Strict):
    """What the service returned. `status` is the HTTP status, or the exit code for job cases."""
    status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)   # allowlisted, lower-case names
    body: Any = None                                        # parsed JSON, else text, else None (empty)
    body_is_json: bool = False
    duration_s: float = 0.0


class FieldDiff(_Strict):
    field: str                     # "status", "headers.content-type", "body.$.total", "body"
    expected: Any = None
    actual: Any = None


class CaseResult(_Strict):
    case_id: str
    kind: str
    outcome: Outcome
    observed: Observed | None = None
    diffs: list[FieldDiff] = Field(default_factory=list)
    error: str = ""
    error_class: Literal["", "transport", "host_refused", "config", "invalid_case", "timeout", "protocol"] = ""
    expectation_source: str = ""   # recorded | specified
    masks: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "pass"
