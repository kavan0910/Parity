"""Versioned target-fit knowledge: library / API mapping tables (docs/phase2-plan.md section 2, layer 1).

Package data: `migrate/knowledge/libmap/*.yaml`, one table per migration pair (or concern), loaded and validated
by `load_libmaps()`. Each file:

    schema_version: 1
    id: spring-boot2-3            # unique, also the file stem
    version: 1                    # bump when entries change (recorded in risk details)
    title: Spring Boot 2.x -> 3.x
    applies:                      # when the table is used for a TargetSpec
      source: {language: java, framework: spring-boot, below: "3"}     # all keys optional
      target: {language: java, framework: spring-boot, at_least: "3"}
    known: ["org.springframework.boot:*"]   # dependency globs that need no replacement (not "unmapped")
    entries:
      - id: springfox
        title: Springfox does not support Spring Boot 3
        match:
          dependencies: ["io.springfox:*"]   # fnmatch globs over dependency coordinates (case-insensitive)
          code: ['\\bspringfox\\.']          # regexes over single code lines (Python `re`)
          files: ["*.java"]                  # fnmatch globs restricting `code` matches by path
          config_keys: []                    # regexes over graph `config` node names
          unless: []                         # regexes: entry suppressed if ANY code line matches one
        replacement: org.springdoc:springdoc-openapi-starter-webmvc-ui
        kind: library_replacement            # library_replacement | removed_api | semantic_shift | config_rename
        severity: medium                     # low | medium | high
        needs_decision: false                # default: true for semantic_shift, else false
        note: ...
        source: https://...                  # where the fact comes from

`below` / `at_least` compare the framework version when `framework` is given, else the runtime version; an
unknown version is treated as "applies" (conservative).
"""
from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntryKind = Literal["library_replacement", "removed_api", "semantic_shift", "config_rename"]
Severity = Literal["low", "medium", "high"]

_LANG_ALIASES = {
    "js": "javascript", "node": "javascript", "nodejs": "javascript", "node.js": "javascript",
    "typescript": "javascript", "ts": "javascript", "py": "python", "python3": "python", "golang": "go",
}
_FW_ALIASES = {"springboot": "spring-boot", "spring_boot": "spring-boot"}


class KnowledgeError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def norm_language(lang: str) -> str:
    s = (lang or "").strip().lower()
    return _LANG_ALIASES.get(s, s)


def split_framework(fw: str) -> tuple[str, str]:
    """"spring-boot@2.7" -> ("spring-boot", "2.7"); "" -> ("", "")."""
    name, _, ver = (fw or "").strip().partition("@")
    name = name.strip().lower()
    return _FW_ALIASES.get(name, name), ver.strip()


def version_tuple(v: str) -> tuple[int, ...] | None:
    """"3.2.18" -> (3, 2, 18), "^4.18" -> (4, 18); None if no digits. (Java "1.8" is normalised to "8" by
    migrate/target.py before it reaches a TargetSpec.)"""
    nums = [int(x) for x in re.findall(r"\d+", v or "")]
    return tuple(nums) if nums else None


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a2, b2 = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


class StackFilter(_Strict):
    language: str = ""
    framework: str = ""
    below: str = ""          # version must be < this (framework version if framework given, else runtime)
    at_least: str = ""       # version must be >= this

    def accepts(self, language: str, runtime: str, framework: str) -> bool:
        if self.language and norm_language(self.language) != norm_language(language):
            return False
        fw_name, fw_ver = split_framework(framework)
        if self.framework and split_framework(self.framework)[0] != fw_name:
            return False
        ver = version_tuple(fw_ver if self.framework else runtime)
        if ver is not None:
            if self.below and _cmp(ver, version_tuple(self.below) or ()) >= 0:
                return False
            if self.at_least and _cmp(ver, version_tuple(self.at_least) or ()) < 0:
                return False
        return True


class Applies(_Strict):
    source: StackFilter = Field(default_factory=StackFilter)
    target: StackFilter = Field(default_factory=StackFilter)


class Match(_Strict):
    dependencies: list[str] = Field(default_factory=list)
    code: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    config_keys: list[str] = Field(default_factory=list)
    unless: list[str] = Field(default_factory=list)

    @field_validator("code", "config_keys", "unless")
    @classmethod
    def _regexes(cls, v: list[str]) -> list[str]:
        for rx in v:
            if len(rx) > 500:
                raise ValueError(f"regex longer than 500 chars: {rx[:40]}...")
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"invalid regex {rx!r}: {e}") from e
        return v

    @model_validator(mode="after")
    def _something(self):
        if not (self.dependencies or self.code or self.config_keys):
            raise ValueError("match needs at least one of dependencies, code, config_keys")
        return self

    def dependency_matches(self, coord: str) -> bool:
        return any(dep_glob_match(g, coord) for g in self.dependencies)

    def file_ok(self, path: str) -> bool:
        return not self.files or any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], g)
                                     for g in self.files)


class LibEntry(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    title: str = Field(min_length=1)
    match: Match
    replacement: str = ""
    kind: EntryKind
    severity: Severity = "medium"
    needs_decision: bool | None = None
    note: str = Field(min_length=1)
    source: str = Field(pattern=r"^https://\S+$")

    @model_validator(mode="after")
    def _default_decision(self):
        if self.needs_decision is None:
            self.needs_decision = self.kind == "semantic_shift"
        return self


class LibMap(_Strict):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    applies: Applies = Field(default_factory=Applies)
    known: list[str] = Field(default_factory=list)
    entries: list[LibEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        ids = [e.id for e in self.entries]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate entry ids: {dup}")
        return self

    def applies_to(self, spec) -> bool:
        """`spec` is a migrate.models.TargetSpec. For a version upgrade with no target framework, the source
        framework name is assumed to carry over."""
        src, tgt = spec.source, spec.target
        tgt_fw = tgt.framework
        if not tgt_fw and spec.kind == "version_upgrade":
            tgt_fw = split_framework(src.framework)[0]
        return (self.applies.source.accepts(src.language, src.runtime, src.framework)
                and self.applies.target.accepts(tgt.language, tgt.runtime, tgt_fw))

    def is_known(self, coord: str) -> bool:
        return any(dep_glob_match(g, coord) for g in self.known)


def _norm_coord(s: str) -> str:
    return re.sub(r"[-_.]+", "-", s.strip().lower()) if ":" not in s and "/" not in s else s.strip().lower()


def dep_glob_match(glob: str, coord: str) -> bool:
    """Case-insensitive fnmatch; pip-style names compare with -, _ and . unified."""
    g, c = glob.strip().lower(), coord.strip().lower()
    return fnmatch.fnmatchcase(c, g) or fnmatch.fnmatchcase(_norm_coord(c), _norm_coord(g))


def parse_libmap(text: str, name: str = "<libmap>") -> LibMap:
    import yaml
    from pydantic import ValidationError
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise KnowledgeError(f"{name}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise KnowledgeError(f"{name}: must be a mapping")
    try:
        return LibMap.model_validate(data)
    except ValidationError as e:
        raise KnowledgeError(f"{name}: {e}") from e


@lru_cache(maxsize=1)
def load_libmaps() -> tuple[LibMap, ...]:
    """All bundled tables, sorted by id. Raises KnowledgeError on an invalid or duplicate table."""
    root = resources.files(__package__) / "libmap"
    out: dict[str, LibMap] = {}
    for f in sorted(root.iterdir(), key=lambda x: x.name):
        if not f.name.endswith((".yaml", ".yml")):
            continue
        m = parse_libmap(f.read_text(encoding="utf-8"), f.name)
        stem = f.name.rsplit(".", 1)[0]
        if m.id != stem:
            raise KnowledgeError(f"{f.name}: id {m.id!r} must equal the file stem {stem!r}")
        if m.id in out:
            raise KnowledgeError(f"duplicate libmap id {m.id}")
        out[m.id] = m
    return tuple(out[k] for k in sorted(out))


def libmaps_for(spec) -> list[LibMap]:
    """Tables that apply to a TargetSpec."""
    return [m for m in load_libmaps() if m.applies_to(spec)]
