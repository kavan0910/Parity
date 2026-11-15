"""M4 codemods (migrate/codemods/): adapters, sandbox-only execution, patch apply + agent commit."""
import subprocess

from migration_agent.migrate import slice_runner as sr
from migration_agent.migrate import targetrepo
from migration_agent.migrate.codegen import OfflineCodegenLLM
from migration_agent.migrate.codemods import (
    DjangoUpgradeAdapter, JscodeshiftAdapter, LebabAdapter, OpenRewriteAdapter, run_codemod, run_codemods, tree_changes,
)
from migration_agent.migrate.exec.fake import FakeExecBackend, FakeOutcome
from migration_agent.migrate.models import TargetSpec
from migration_agent.migrate.paths import Phase2Layout

from p2_m4_helpers import m4_project, passing_suite, ready_slice

JAVA = TargetSpec.model_validate({
    "kind": "version_upgrade", "strategy": "in_place",
    "source": {"language": "java", "runtime": "java@8", "framework": "spring-boot@2.7"},
    "target": {"language": "java", "runtime": "java@21", "framework": "spring-boot@3.2", "build": "maven"}})
DJANGO = TargetSpec.model_validate({
    "kind": "version_upgrade", "strategy": "in_place", "source": {"language": "python", "framework": "django@3.2"},
    "target": {"language": "python", "runtime": "python@3.12", "framework": "django@5.0", "build": "pip"}})


def _seeded(tmp_path, tmp_path_factory):
    p = m4_project(tmp_path, tmp_path_factory)
    lay = Phase2Layout(p)
    (lay.legacy_src / "src").mkdir(parents=True)
    (lay.legacy_src / "src" / "App.java").write_text("import javax.servlet.Filter;\nclass App {}\n")
    targetrepo.init_repo(p, JAVA)
    return p


def test_adapters_select_recipes_and_commands():
    a = OpenRewriteAdapter()
    assert a.applies(JAVA)[0]
    cmd = a.command(JAVA)
    assert cmd[0] == "sh" and "UpgradeToJava21,org.openrewrite.java.spring.boot3.UpgradeSpringBoot_3_0" in cmd[2]
    assert "mvn -B -q" in cmd[2] and "-s /tmp/settings.xml" in cmd[2] and "egress-proxy" in cmd[2]   # Maven ignores HTTPS_PROXY
    assert "-Dmaven.repo.local=/cache/m2" in cmd[2] and a.cache == "harness/cache/m2"
    assert "-Dhttps.proxyHost=egress-proxy" in cmd[2] and "/tmp/.m2/settings.xml" in cmd[2]
    gradle = JAVA.model_copy(update={"target": JAVA.target.model_copy(update={"build": "gradle"})})
    gcmd = a.command(gradle)
    assert gcmd[0] == "sh" and "rewriteRun" in gcmd[2] and "UpgradeToJava21" in gcmd[2]
    assert "org.openrewrite.gradle.RewritePlugin" in gcmd[2] and a.image_for(gradle).startswith("gradle:")
    assert a.image_for(JAVA) == a.image
    ant = JAVA.model_copy(update={"target": JAVA.target.model_copy(update={"build": "ant"})})
    assert isinstance(a.command(ant), str) and "Maven and Gradle only" in a.command(ant)
    assert not a.applies(DJANGO)[0]
    d = DjangoUpgradeAdapter()
    assert d.applies(DJANGO)[0] and not d.applies(JAVA)[0]
    script = d.command(DJANGO)[2]
    assert "--target-version 5.0" in script and "--py312-plus" in script
    js = TargetSpec.model_validate({"kind": "version_upgrade", "source": {"language": "javascript"},
                                    "target": {"language": "javascript"}})
    jc = JscodeshiftAdapter().command(js)          # JscodeshiftAdapter is the old name of LebabAdapter
    assert JscodeshiftAdapter is LebabAdapter and LebabAdapter().name == "lebab"
    assert LebabAdapter().applies(js)[0] and jc[0] == "sh" and "lebab@" in jc[2] and "--transform let,template" in jc[2]
    assert "node /tmp/tools/node_modules/lebab/bin/index.js" in jc[2]          # /tmp is noexec: started via node
    assert "lebab failed on" in jc[2] and "exit 1" in jc[2]                      # a failed file fails the job
    assert "Traceback|No module named" in DjangoUpgradeAdapter().command(DJANGO)[2]
    move = js.model_copy(update={"kind": "framework_move",
                                 "source": js.source.model_copy(update={"framework": "express@4"}),
                                 "target": js.target.model_copy(update={"framework": "fastify@5"})})
    assert isinstance(JscodeshiftAdapter().command(move), str)
    go = TargetSpec.model_validate({"kind": "language_move", "source": {"language": "javascript"},
                                    "target": {"language": "go"}})
    assert isinstance(JscodeshiftAdapter().command(go), str)


def test_skipped_when_backend_unavailable(tmp_path, tmp_path_factory):
    p = _seeded(tmp_path, tmp_path_factory)
    head = targetrepo.head(p)
    fb = FakeExecBackend(p, available=(False, "docker daemon not reachable"))
    r = run_codemod(p, OpenRewriteAdapter(), JAVA, backend=fb)
    assert r.status == "skipped" and "never run on the host" in r.reason and "docker daemon" in r.reason
    assert not fb.jobs and targetrepo.head(p) == head
    assert not Phase2Layout(p).runs_dir.exists()


def test_openrewrite_patch_applied_and_committed(tmp_path, tmp_path_factory):
    p = _seeded(tmp_path, tmp_path_factory)
    lay = Phase2Layout(p)

    def rewrite(job):
        ws = p.root / job.mounts[0].source
        f = ws / "src" / "App.java"
        f.write_text(f.read_text().replace("javax.servlet", "jakarta.servlet"))
        (ws / "target").mkdir()
        (ws / "target" / "App.class").write_text("build output")
        return FakeOutcome(exit_code=0)
    fb = FakeExecBackend(p, script=rewrite)
    r = run_codemod(p, OpenRewriteAdapter(), JAVA, backend=fb)
    assert r.status == "applied" and r.files == ["src/App.java"]
    assert (fb.jobs[0].phase, fb.jobs[0].network) == ("build", "mirror")
    assert "jakarta.servlet" in (lay.target_code / "src" / "App.java").read_text()
    assert not (lay.target_code / "target").exists()
    assert "jakarta.servlet" in (p.root / r.patch_ref).read_text()
    log = subprocess.run(["git", "-C", str(lay.target_code), "log", "-1", "--format=%ae|%s|%D"],
                         capture_output=True, text=True).stdout
    assert log.startswith("agent@localhost|codemod openrewrite") and f"codemod/openrewrite/{r.run_id}" in log


def test_failed_and_no_change_codemods(tmp_path, tmp_path_factory):
    p = _seeded(tmp_path, tmp_path_factory)
    head = targetrepo.head(p)
    r = run_codemod(p, OpenRewriteAdapter(), JAVA, backend=FakeExecBackend(p, script=[FakeOutcome(exit_code=1,
                                                                                                    stderr="boom")]))
    assert r.status == "failed" and "boom" in r.reason
    r = run_codemod(p, OpenRewriteAdapter(), JAVA, backend=FakeExecBackend(p))
    assert r.status == "no_changes" and targetrepo.head(p) == head
    assert run_codemods(p, DJANGO.model_copy(update={"source": JAVA.source}), backend=FakeExecBackend(p)) == []


def test_tree_changes_ignores_build_outputs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "src").mkdir(parents=True)
    (a / "src" / "x.py").write_text("old\n")
    (b / "src" / "x.py").write_text("new\n")
    (a / "gone.py").write_text("1\n")
    (b / "node_modules").mkdir()
    (b / "node_modules" / "m.js").write_text("1")
    (b / "added.py").write_text("2\n")
    ch, patch = tree_changes(a, b)
    assert ch == {"src/x.py": "new\n", "gone.py": None, "added.py": "2\n"}
    assert "+new" in patch and "node_modules" not in patch


def test_slice_runner_runs_codemods_once_for_in_place_upgrades(tmp_path, tmp_path_factory):
    y = ("kind: version_upgrade\nstrategy: in_place\nsource: {language: java, runtime: java@8}\n"
         "target: {language: java, runtime: java@21, build: maven}\n")
    p = m4_project(tmp_path, tmp_path_factory, target_yaml=y)
    sid = ready_slice(p).id
    fb = FakeExecBackend(p)
    rep = sr.generate(p, slice_id=sid, llm=OfflineCodegenLLM(), backend=fb, suite_runner=passing_suite)
    assert [m["name"] for m in rep.codemods] == ["openrewrite"] and rep.codemods[0]["status"] == "no_changes"
    assert fb.jobs[0].labels.get("stage") == "codemod"
    n = len(fb.jobs)
    rep = sr.generate(p, slice_id=sid, llm=OfflineCodegenLLM(), backend=fb, suite_runner=passing_suite)
    assert rep.codemods == [] and len(fb.jobs) == n                 # recorded in migration/codemods.json
    assert (Phase2Layout(p).migration / "codemods.json").exists()
