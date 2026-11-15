"""M2: target spec (`target init` / `target show`), source detection and the libmap knowledge tables."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from migration_agent.analyze.runner import run_analyze
from migration_agent.cli import main
from migration_agent.llm.offline import OfflineLLM
from migration_agent.migrate.knowledge import (KnowledgeError, LibMap, dep_glob_match, libmaps_for, load_libmaps,
                                               parse_libmap)
from migration_agent.migrate.models import TargetSpec
from migration_agent.migrate.paths import Phase2Layout
from migration_agent.migrate.target import (TargetSpecError, build_spec, detect_source, java_version, load_target,
                                            major_minor, parse_target_text)
from migration_agent.project import Project
from migration_agent.review.approval import approve

REPOS = Path(__file__).parent.parent / "eval" / "repos"
_CACHE: dict[str, Path] = {}


def _extracted(base: Path, repo: Path, name: str) -> Path:
    root = base / f"{name}.migration"
    r = CliRunner()
    for args in (["init", str(repo), "--project", str(root)], ["ingest", str(root)], ["extract", str(root)]):
        res = r.invoke(main, args)
        assert res.exit_code == 0, res.output
    return root


def project_from(tmp_path, tmp_path_factory, repo: Path, *, approved: bool = True, name: str | None = None
                 ) -> Project:
    """Offline-analysed (and approved) copy of `repo`, built once per session per repo."""
    name = name or repo.name
    key = f"{name}:{approved}"
    if key not in _CACHE:
        base = tmp_path_factory.getbasetemp() / "m2-target" / key.replace(":", "-")
        root = _extracted(base, repo, name)
        p = Project.load(root)
        if approved:
            run_analyze(p, OfflineLLM())
            approve(p, "alice")
        _CACHE[key] = root
    dst = tmp_path / f"{name}.migration"
    shutil.copytree(_CACHE[key], dst)
    return Project(dst)


def _run(*args):
    return CliRunner().invoke(main, [str(a) for a in args])


# ------------------------------------------------------------------ detection on the eval repos
def test_detect_spring_lite(tmp_path, tmp_path_factory):
    det = detect_source(project_from(tmp_path, tmp_path_factory, REPOS / "spring-lite", approved=False))
    assert det.source.language == "java" and det.build == "maven"
    assert det.source.framework == "spring-boot"            # no version in the pom
    assert any("pom.xml" in n for n in det.notes)


def test_detect_express_crud(tmp_path, tmp_path_factory):
    det = detect_source(project_from(tmp_path, tmp_path_factory, REPOS / "express-crud", approved=False))
    assert det.source.language == "javascript" and det.build == "npm"
    assert det.source.framework == "express@4.18"


def test_detect_django_lite_without_manifest(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "django-lite", approved=False)
    assert json.loads((p.facts / "manifests.json").read_text())["manifests"] == []
    det = detect_source(p)
    assert det.source.language == "python" and det.source.framework == "django" and det.build == "pip"


def _write_manifest(p: Project, manifests: list[dict]) -> None:
    (p.facts / "manifests.json").write_text(json.dumps({"manifests": manifests}))


def test_detect_versions_from_manifests(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "spring-lite", approved=False)
    _write_manifest(p, [{"path": "pom.xml", "ecosystem": "maven", "runtime": {
        "java.version": "1.8", "parent": "org.springframework.boot:spring-boot-starter-parent:2.7.18"},
        "dependencies": [{"name": "org.springframework.boot:spring-boot-starter-web"}]}])
    det = detect_source(p)
    assert (det.source.runtime, det.source.framework) == ("8", "spring-boot@2.7")

    _write_manifest(p, [{"path": "requirements.txt", "ecosystem": "pip", "runtime": {},
                         "dependencies": [{"name": "Django", "version": "==3.2.18"}]}])
    assert detect_source(p).source.framework == "django@3.2"

    _write_manifest(p, [{"path": "pyproject.toml", "ecosystem": "python", "runtime": {"python": "^3.9"},
                         "dependencies": [{"name": "django", "version": ">=3.2,<4"}]}])
    det = detect_source(p)
    assert (det.source.language, det.source.runtime, det.source.framework, det.build) == (
        "python", "3.9", "django@3.2", "poetry")

    _write_manifest(p, [{"path": "build.gradle", "ecosystem": "gradle",
                         "runtime": {"sourceCompatibility": "JavaVersion.VERSION_1_8"},
                         "dependencies": [{"name": "org.springframework.boot:spring-boot-starter-web"}]}])
    det = detect_source(p)
    assert (det.source.language, det.source.runtime, det.build) == ("java", "8", "gradle")


def test_version_helpers():
    assert major_minor("^4.18.2") == "4.18" and major_minor(">=3.2,<4") == "3.2" and major_minor("${x}") == ""
    assert java_version("1.8") == "8" and java_version("17") == "17" and java_version("VERSION_11") == "11"
    assert java_version("${java.version}") == ""


# ------------------------------------------------------------------ spec building / validation
def test_build_spec_infers_kind_and_strategy(tmp_path, tmp_path_factory):
    det = detect_source(project_from(tmp_path, tmp_path_factory, REPOS / "express-crud", approved=False))
    s = build_spec(det, framework="fastify@4")
    assert s.kind == "framework_move" and s.strategy == "strangler" and s.target.build == "npm"
    s = build_spec(det, language="go", runtime="1.22")
    assert s.kind == "language_move" and s.target.build == "go-mod"
    s = build_spec(det, runtime="20", framework="express@4.21")
    assert s.kind == "version_upgrade" and s.strategy == "in_place"


def test_parse_target_errors_are_friendly():
    with pytest.raises(TargetSpecError) as e:
        parse_target_text("kind: rewrite\nsource: {language: java}\ntarget: {language: java}\ncolour: blue\n")
    msg = str(e.value)
    assert "kind: must be one of" in msg and "colour: unknown field" in msg
    with pytest.raises(TargetSpecError, match="required field is missing"):
        parse_target_text("kind: version_upgrade\nsource: {language: java}\n")
    with pytest.raises(TargetSpecError, match="not valid YAML"):
        parse_target_text("kind: [unclosed\n")
    with pytest.raises(TargetSpecError, match="must be a mapping"):
        parse_target_text("- a\n- b\n")


# ------------------------------------------------------------------ CLI
def test_target_init_requires_contract(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "spring-lite", approved=False)
    r = _run("target", "init", p.root, "--runtime", "21")
    assert r.exit_code != 0 and "contract" in r.output
    assert not Phase2Layout(p).target_yaml.exists()


def test_target_init_show_and_force(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "spring-lite")
    r = _run("target", "init", p.root, "--runtime", "21", "--framework", "spring-boot@3.3", "--budget-usd", "25")
    assert r.exit_code == 0, r.output
    spec = load_target(p)
    assert spec.kind == "version_upgrade" and spec.source.language == "java"
    assert spec.source.framework == "spring-boot" and spec.target.framework == "spring-boot@3.3"
    assert spec.budget_usd == 25 and spec.strategy == "in_place"
    text = Phase2Layout(p).target_yaml.read_text()
    assert text.startswith("# target/target.yaml") and TargetSpec.from_yaml(text) == spec
    assert json.loads((p.root / "project.json").read_text())["schema_version"] == 2

    again = _run("target", "init", p.root, "--runtime", "17")
    assert again.exit_code != 0 and "--force" in again.output
    assert load_target(p).target.runtime == "21"
    forced = _run("target", "init", p.root, "--runtime", "17", "--force")
    assert forced.exit_code == 0 and load_target(p).target.runtime == "17"

    show = _run("target", "show", p.root, "--format", "json")
    assert show.exit_code == 0 and json.loads(show.output)["target"]["runtime"] == "17"


def test_target_init_source_overrides_and_bad_flags(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "express-crud")
    r = _run("target", "init", p.root, "--language", "go", "--runtime", "1.22",
             "--source-runtime", "18", "--base-image", "golang:1.22@sha256:" + "a" * 64)
    assert r.exit_code == 0, r.output
    s = load_target(p)
    assert s.kind == "language_move" and s.source.runtime == "18" and s.source.framework == "express@4.18"
    bad = _run("target", "init", p.root, "--force", "--language", "")
    assert bad.exit_code != 0 and "target.language" in bad.output and "Traceback" not in bad.output
    assert load_target(p).target.language == "go"      # the failed init did not overwrite


def test_target_show_invalid_file(tmp_path, tmp_path_factory):
    p = project_from(tmp_path, tmp_path_factory, REPOS / "spring-lite")
    lay = Phase2Layout(p)
    missing = _run("target", "show", p.root)
    assert missing.exit_code != 0 and "target init" in missing.output
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text("kind: version_upgrade\nsource: {language: java}\n"
                               "target: {language: java, port: 80}\nbudget_usd: -1\n")
    r = _run("target", "show", p.root)
    assert r.exit_code != 0
    assert "target.port: unknown field" in r.output and "budget_usd" in r.output
    assert "Traceback" not in r.output


# ------------------------------------------------------------------ knowledge tables
def test_libmaps_load_and_are_well_formed():
    maps = {m.id: m for m in load_libmaps()}
    assert {"java8-21", "spring-boot2-3", "django3-5", "express-fastify", "js-go"} <= set(maps)
    for m in maps.values():
        assert m.entries, m.id
        for e in m.entries:
            assert e.source.startswith("https://") and e.note and e.kind
    sb = maps["spring-boot2-3"]
    assert {"javax-persistence", "springfox", "trailing-slash-matching", "prop-spring-redis"} <= {
        e.id for e in sb.entries}
    assert {"security-manager", "nashorn-removed"} <= {e.id for e in maps["java8-21"].entries}
    assert {"use-l10n-removed", "conf-urls-url"} <= {e.id for e in maps["django3-5"].entries}
    assert {"body-parsing", "error-handler", "express-router"} <= {e.id for e in maps["express-fastify"].entries}


def _spec(kind, src, tgt):
    return TargetSpec.model_validate({"kind": kind, "source": src, "target": tgt})


def test_libmap_applicability():
    ids = lambda s: {m.id for m in libmaps_for(s)}  # noqa: E731
    boot = _spec("version_upgrade", {"language": "java", "runtime": "8", "framework": "spring-boot@2.7"},
                 {"language": "java", "runtime": "21", "framework": "spring-boot@3.3"})
    assert ids(boot) == {"java8-21", "spring-boot2-3"}
    already3 = _spec("version_upgrade", {"language": "java", "runtime": "17", "framework": "spring-boot@3.1"},
                     {"language": "java", "runtime": "21", "framework": "spring-boot@3.3"})
    assert ids(already3) == {"java8-21"}
    dj = _spec("version_upgrade", {"language": "python", "framework": "django@3.2"},
               {"language": "python", "framework": "django@5.1"})
    assert ids(dj) == {"django3-5"}
    fw = _spec("framework_move", {"language": "javascript", "framework": "express@4.18"},
               {"language": "js", "framework": "fastify@4"})
    assert ids(fw) == {"express-fastify"}
    go = _spec("language_move", {"language": "javascript", "framework": "express@4.18"}, {"language": "go"})
    assert ids(go) == {"js-go"}


def test_libmap_schema_rejects_bad_tables():
    base = {"schema_version": 1, "id": "x", "version": 1, "title": "t"}
    entry = {"id": "e", "title": "t", "kind": "removed_api", "note": "n", "source": "https://x.y",
             "match": {"code": ["("]}}
    with pytest.raises(Exception, match="invalid regex"):
        LibMap.model_validate({**base, "entries": [entry]})
    with pytest.raises(Exception, match="at least one"):
        LibMap.model_validate({**base, "entries": [{**entry, "match": {"files": ["*.java"]}}]})
    with pytest.raises(Exception):
        LibMap.model_validate({**base, "entries": [{**entry, "match": {"code": ["x"]}, "source": "http://x"}]})
    with pytest.raises(KnowledgeError):
        parse_libmap("entries: [", "bad.yaml")
    ok = LibMap.model_validate({**base, "entries": [{**entry, "kind": "semantic_shift", "match": {"code": ["x"]}}]})
    assert ok.entries[0].needs_decision is True


def test_dep_glob_match():
    assert dep_glob_match("io.springfox:*", "io.springfox:springfox-swagger2")
    assert dep_glob_match("django", "Django")
    assert dep_glob_match("python-dateutil", "python_dateutil")
    assert not dep_glob_match("express", "express-validator")
