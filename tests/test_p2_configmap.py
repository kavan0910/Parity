"""M6 config map (migrate/configmap.py): every config key mapped/renamed/dropped/open in the target convention,
libmap renames (Spring Boot 2 -> 3), secrets as references only, generated files secret-scanned, gaps gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.extract.config import extract_config, write_config
from migration_agent.migrate import configmap as C
from migration_agent.migrate.models import SourceStack, TargetSpec, TargetStack
from migration_agent.migrate.target import build_spec, detect_source, write_target

from phase2_helpers import approved_project

REPOS = Path(__file__).parent.parent / "eval" / "repos"
SECRET = "Pr0dS3cretValue!" + "42"                  # never allowed in any output
SPRING_PROPS = f"""server.port=8080
spring.redis.host=cache.internal
spring.redis.port=6379
server.max-http-header-size=16KB
management.metrics.export.prometheus.enabled=true
management.trace.http.enabled=false
spring.jpa.hibernate.use-new-id-generator-mappings=true
spring.datasource.url=jdbc:postgresql://db:5432/library
spring.datasource.password={SECRET}
library.loan-days=14
"""


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    return approved_project(tmp_path, tmp_path_factory)


def _fastify(p):
    write_target(p, build_spec(detect_source(p), framework="fastify@4"), force=True)


def _spring(p, tmp_path):
    """Synthetic Spring Boot 2 application.properties added to the (redacted) code/ copy + re-extracted facts."""
    res = p.code / "src" / "main" / "resources"
    res.mkdir(parents=True, exist_ok=True)
    (res / "application.properties").write_text(SPRING_PROPS)
    (res / "application-dev.properties").write_text("spring.redis.host=localhost\n")
    write_config(extract_config(p.code), p.facts)
    write_target(p, TargetSpec(kind="version_upgrade",
                               source=SourceStack(language="java", runtime="17", framework="spring-boot@2.7"),
                               target=TargetStack(language="java", runtime="21", framework="spring-boot@3.3",
                                                  build="maven")), force=True)


def _all_output_text(p) -> str:
    out = []
    for d in (p.root / "config", p.root / "target_code"):
        for f in d.rglob("*") if d.exists() else []:
            if f.is_file():
                out.append(f.read_text(errors="replace"))
    return "\n".join(out)


def _by_key(cmap):
    return {e.source_key: e for e in cmap.entries}


# ------------------------------------------------------------------ express-crud -> fastify (approved project)
def test_express_to_fastify_maps_every_key_and_references_secrets(proj):
    _fastify(proj)
    cmap = C.build_config_map(proj)
    keys = _by_key(cmap)
    assert set(keys) == {"PORT", "DB_URL", "API_TOKEN", "MAX_ORDER_ITEMS"}
    assert {e.node_id for e in cmap.entries} == {f"CFG-000{i}" for i in range(1, 5)}
    assert cmap.convention == "node" and all(e.status == "mapped" for e in cmap.entries)
    tok = keys["API_TOKEN"]
    assert tok.is_secret and tok.reference == "${API_TOKEN}" and tok.target_key == "API_TOKEN"
    assert [s.env_name for s in cmap.secrets] == ["API_TOKEN"] and cmap.secrets[0].vault.startswith("vault:")
    env = (proj.root / "config" / "generated" / ".env.example").read_text()
    assert "PORT=3000" in env and "MAX_ORDER_ITEMS=20" in env and "\nAPI_TOKEN=\n" in env
    schema = json.loads((proj.root / "config" / "generated" / "config" / "env.schema.json").read_text())
    assert schema["required"] == ["API_TOKEN"] and "default" not in schema["properties"]["API_TOKEN"]
    # the legacy template value of the token never reaches an output
    legacy_value = [ln.split("=", 1)[1] for ln in (REPOS / "express-crud" / ".env.example").read_text().splitlines()
                    if ln.startswith("API_TOKEN=")][0]
    assert legacy_value and legacy_value not in _all_output_text(proj)
    md = (proj.root / "config" / "config_map.md").read_text()
    assert "Deploy-time secrets checklist" in md and "| `API_TOKEN` | yes | mapped |" in md
    assert cmap.secret_scan.ok and C.config_gaps(proj) == []
    assert not (proj.root / "target_code").exists() and cmap.copied_to_target_code == []


def test_copies_into_target_code_without_clobbering_hand_written_files(proj):
    _fastify(proj)
    tc = proj.root / "target_code"
    (tc / "config").mkdir(parents=True)
    (tc / "config" / "env.schema.json").write_text('{"hand": "written"}')
    cmap = C.build_config_map(proj)
    assert cmap.copied_to_target_code == [".env.example"] and cmap.skipped_existing == ["config/env.schema.json"]
    assert (tc / "config" / "env.schema.json").read_text() == '{"hand": "written"}'
    assert C.MARKER in (tc / ".env.example").read_text()
    cmap = C.build_config_map(proj, force=True)
    assert "config/env.schema.json" in cmap.copied_to_target_code
    assert C.MARKER in (tc / "config" / "env.schema.json").read_text()


# ------------------------------------------------------------------ Spring Boot 2 -> 3 renames + secret
def test_spring_boot_renames_drops_and_secret_reference(proj, tmp_path):
    _spring(proj, tmp_path)
    cmap = C.build_config_map(proj)
    assert cmap.convention == "spring"
    e = {(x.source_file.rsplit("/", 1)[-1], x.source_key): x for x in cmap.entries}
    app = "application.properties"
    assert e[(app, "spring.redis.host")].status == "renamed"
    assert e[(app, "spring.redis.host")].target_key == "spring.data.redis.host"
    assert e[(app, "spring.redis.host")].rule == "libmap:spring-boot2-3/prop-spring-redis"
    assert e[(app, "server.max-http-header-size")].target_key == "server.max-http-request-header-size"
    assert e[(app, "management.metrics.export.prometheus.enabled")].target_key == \
        "management.prometheus.metrics.export.enabled"
    assert e[(app, "management.trace.http.enabled")].target_key == "management.httpexchanges.enabled"
    drop = e[(app, "spring.jpa.hibernate.use-new-id-generator-mappings")]
    assert drop.status == "dropped" and "Hibernate 6" in drop.reason
    assert e[(app, "library.loan-days")].status == "mapped"
    assert e[("application-dev.properties", "spring.redis.host")].target_file == \
        "src/main/resources/application-dev.properties"
    pw = e[(app, "spring.datasource.password")]
    assert pw.is_secret and pw.reference == "${SPRING_DATASOURCE_PASSWORD}"
    # env-style legacy keys bind through relaxed binding
    assert e[(".env.example", "MAX_ORDER_ITEMS")].target_key == "max.order.items"

    gen = (proj.root / "config" / "generated" / "src" / "main" / "resources" / "application.properties").read_text()
    assert "spring.data.redis.host=cache.internal" in gen and "spring.redis." not in gen
    assert "server.max-http-request-header-size=16KB" in gen
    assert "spring.datasource.password=${SPRING_DATASOURCE_PASSWORD}" in gen
    assert "use-new-id-generator-mappings" not in gen
    yml = (proj.root / "config" / "generated" / "src" / "main" / "resources" / "application.yml").read_text()
    assert "${API_TOKEN}" in yml and "max:" in yml
    assert SECRET not in _all_output_text(proj) and "Pr0dS3cret" not in _all_output_text(proj)
    assert cmap.secret_scan.ok


def test_libmap_rename_rules_unit():
    from migration_agent.migrate.knowledge import load_libmaps
    entries = {e.id: e for m in load_libmaps() for e in m.entries}
    assert C.libmap_rename(entries["prop-data-cassandra"], "spring.data.cassandra.keyspace-name") == \
        ("renamed", "spring.cassandra.keyspace-name", C.libmap_rename(entries["prop-data-cassandra"],
                                                                      "spring.data.cassandra.x")[2])
    st, tk, why = C.libmap_rename(entries["use-l10n-removed"], "USE_L10N")
    assert st == "open" and "decision" in why and tk == ""
    st, tk, why = C.libmap_rename(entries["removed-settings-4"], "PASSWORD_RESET_TIMEOUT_DAYS")
    assert st == "open"                                             # alternation: ambiguous, never guessed
    assert C.libmap_rename(entries["prop-metrics-export"], "management.metrics.export")[0] == "open"
    assert C.env_name("spring.datasource.hikari[0].url") == "SPRING_DATASOURCE_HIKARI_0_URL"
    assert C.env_name("9lives") == "_9LIVES"


# ------------------------------------------------------------------ gaps, decisions, staleness
def test_gaps_open_items_decisions_and_staleness(proj):
    assert C.config_gaps(proj)[0]["reason"].startswith("config map not generated")
    _fastify(proj)
    (proj.code / ".github" / "workflows").mkdir(parents=True)
    (proj.code / ".github" / "workflows" / "ci.yml").write_text("env:\n  NODE_VERSION: '20'\n")
    (proj.code / "deploy.yaml").write_text("apiVersion: v1\nkind: ConfigMap\ndata:\n  LOG_LEVEL: info\n")
    (proj.code / "docker-compose.yml").write_text("services:\n  app:\n    image: app\n    environment:\n"
                                                   "      - PORT=3000\n      - FEATURE_X=on\n")
    write_config(extract_config(proj.code), proj.facts)
    cmap = C.build_config_map(proj)
    k = _by_key(cmap)
    assert k["env.NODE_VERSION"].status == "dropped" and "CI" in k["env.NODE_VERSION"].reason
    assert k["services.app.image"].status == "dropped"
    assert k["services.app.environment.FEATURE_X"].status == "mapped"
    assert k["services.app.environment.FEATURE_X"].target_key == "FEATURE_X"
    assert k["services.app.environment.PORT"].status == "mapped"       # same variable as .env.example PORT
    assert k["data.LOG_LEVEL"].status == "open"
    gaps = C.config_gaps(proj)
    assert {g["id"] for g in gaps} == {"deploy.yaml:apiVersion", "deploy.yaml:kind", "deploy.yaml:data.LOG_LEVEL"}

    with pytest.raises(C.ConfigMapError, match="reason"):
        C.set_decision(proj, "kind", status="dropped")
    with pytest.raises(C.ConfigMapError, match="unknown config key"):
        C.set_decision(proj, "NOPE", status="dropped", reason="x")
    with pytest.raises(C.ConfigMapError, match="target"):
        C.set_decision(proj, "data.LOG_LEVEL", status="mapped")
    C.set_decision(proj, "data.LOG_LEVEL", status="mapped", target_key="LOG_LEVEL", by="alice")
    C.set_decision(proj, "deploy.yaml:apiVersion", status="dropped", reason="k8s manifest is rewritten by ops")
    C.set_decision(proj, "kind", status="dropped", reason="k8s manifest is rewritten by ops")
    cmap = C.build_config_map(proj)
    k = _by_key(cmap)
    assert k["data.LOG_LEVEL"].rule == "manual" and k["data.LOG_LEVEL"].decided_by == "alice"
    assert C.config_gaps(proj) == []
    assert "LOG_LEVEL=info" in (proj.root / "config" / "generated" / ".env.example").read_text()

    (proj.facts / "config.json").write_text((proj.facts / "config.json").read_text() + " ")
    assert any("stale" in g["reason"] for g in C.config_gaps(proj))


def test_secret_scan_hit_fails_and_writes_nothing(proj, monkeypatch):
    _fastify(proj)
    fake = "AKIA" + "Q" * 16
    real = C.render_files
    monkeypatch.setattr(C, "render_files", lambda *a, **k: {**real(*a, **k), "leak.txt": f"key={fake}\n"})
    (proj.root / "target_code").mkdir()
    with pytest.raises(C.ConfigMapError, match="secret scan"):
        C.build_config_map(proj)
    assert not (proj.root / "config" / "generated").exists()
    assert list((proj.root / "target_code").iterdir()) == []
    saved = json.loads((proj.root / "config" / "config_map.json").read_text())
    assert saved["secret_scan"]["ok"] is False and saved["secret_scan"]["findings"][0]["kind"] == "aws_access_key"
    assert fake not in _all_output_text(proj)
    assert any("secret scan" in g["reason"] for g in C.config_gaps(proj))


def test_secret_looking_value_under_innocent_key_becomes_a_reference(proj):
    _fastify(proj)
    facts = json.loads((proj.facts / "config.json").read_text())
    url = "postgres://app:" + "hunter2hunter2" + "@db:5432/x"
    facts["keys"].append({"file": ".env.example", "line": 9, "key": "UPSTREAM", "value_preview": url,
                          "is_secret": False})
    (proj.facts / "config.json").write_text(json.dumps(facts))
    cmap = C.build_config_map(proj)
    up = _by_key(cmap)["UPSTREAM"]
    assert up.is_secret and up.reference == "${UPSTREAM}"
    assert "hunter2hunter2" not in _all_output_text(proj)


def test_django_and_go_rendering():
    es = [C.ConfigMapEntry(id="a:DEBUG", source_file="a", source_key="DEBUG", status="mapped", target_key="DEBUG",
                           target_file="settings_env.py", env_name="DEBUG"),
          C.ConfigMapEntry(id="a:SECRET_KEY", source_file="a", source_key="SECRET_KEY", is_secret=True,
                           status="mapped", target_key="SECRET_KEY", target_file="settings_env.py",
                           env_name="SECRET_KEY", reference="${SECRET_KEY}"),
          C.ConfigMapEntry(id="a:OLD", source_file="a", source_key="OLD", status="dropped", reason="unused")]
    vals = {"a:DEBUG": "False", "a:SECRET_KEY": None}
    files = C.render_files("django", es, vals, "vault:x")
    py = files["settings_env.py"]
    assert 'DEBUG = os.environ.get("DEBUG", "False")' in py and 'SECRET_KEY = os.environ["SECRET_KEY"]' in py
    assert "OLD" not in py and compile(py, "settings_env.py", "exec")
    go = C.render_files("go", es, vals, "vault:x")["internal/config/config.go"]
    assert 'Debug string `env:"DEBUG" envDefault:"False"`' in go
    assert 'SecretKey string `env:"SECRET_KEY,required"`' in go and "package config" in go


def test_cli_map_and_set(proj):
    _fastify(proj)
    r = CliRunner().invoke(main, ["config", "map", str(proj.root), "--format", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["convention"] == "node"
    r = CliRunner().invoke(main, ["config", "map", str(proj.root)])
    assert r.exit_code == 0 and "# Config map" in r.output
    r = CliRunner().invoke(main, ["config", "set", str(proj.root), "PORT", "--status", "dropped"])
    assert r.exit_code != 0 and "reason" in r.output
    r = CliRunner().invoke(main, ["config", "set", str(proj.root), "PORT", "--status", "dropped",
                                  "--reason", "platform assigns the port"])
    assert r.exit_code == 0 and "config map updated: 0 open" in r.output, r.output
    assert _by_key(C.load_config_map(proj))["PORT"].status == "dropped"
