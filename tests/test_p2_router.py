"""M6 strangler router: proxy routing logic (pure + in-process HTTP), route-table generation from the plan,
flip/unflip rules, --new / --composite compose rendering and build/up/down through DockerBackend with a scripted
docker CLI. A live docker test runs only when a daemon is reachable."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
import yaml
from click.testing import CliRunner

from migration_agent.cli import main
from migration_agent.migrate import exec as p2exec
from migration_agent.migrate import harness as H
from migration_agent.migrate.models import MigrationPlan, Slice, SliceApproval
from migration_agent.migrate.router import proxy as P
from migration_agent.migrate.target import build_spec, detect_source, write_target

from p2_exec_helpers import FakeDocker, flag_values
from phase2_helpers import approved_project

REPOS = Path(__file__).parent.parent / "eval" / "repos"


# ------------------------------------------------------------------ fixtures
def _write_plan(p) -> dict[str, str]:
    g = H.approved_graph(p)
    eps = {n.name: n.id for n in g.nodes if n.type == "endpoint"}
    comps = [n.id for n in g.nodes if n.type not in ("endpoint",)]
    products = [i for name, i in eps.items() if "/api/products" in name]
    orders = [i for name, i in eps.items() if "/api/orders" in name]
    plan = MigrationPlan(version=1, slices=[
        Slice(id="SLICE-0001", name="foundation", kind="foundation", node_ids=comps, order=0),
        Slice(id="SLICE-0002", name="products", kind="entry", node_ids=products, order=1, depends_on=["SLICE-0001"]),
        Slice(id="SLICE-0003", name="orders", kind="entry", node_ids=orders, order=2, depends_on=["SLICE-0001"]),
    ])
    f = p.root / "plan" / "migration_plan.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(plan.model_dump_json(indent=2))
    return eps


def _approve(p, sid: str) -> None:
    f = p.root / "slices" / sid / "approval.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(SliceApproval(slice_id=sid, approved_by="alice", approved_at="2026-01-01T00:00:00Z").model_dump_json())


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    p = approved_project(tmp_path, tmp_path_factory)
    write_target(p, build_spec(detect_source(p), framework="fastify@4"))
    H.import_source(p, from_original=True)
    p.eps = _write_plan(p)
    return p


def _fake(monkeypatch, **kw) -> FakeDocker:
    fd = FakeDocker(**kw)
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fd.backend(project))
    return fd


def _cli(*args):
    return CliRunner().invoke(main, [str(a) for a in args])


def _target_code(p) -> None:
    tc = p.root / "target_code"
    shutil.copytree(REPOS / "express-crud", tc, ignore=shutil.ignore_patterns("harness"))


# ------------------------------------------------------------------ pure routing logic
TABLE = {"schema_version": 1, "default": "legacy",
         "upstreams": {"legacy": "http://legacy:3000", "new": "http://new:8080"},
         "routes": [
             {"method": "GET", "regex": "^/api/products/search/?$", "upstream": "legacy", "slice": "SLICE-0003"},
             {"method": "GET", "regex": "^/api/products/[^/]+/?$", "upstream": "new", "slice": "SLICE-0002"},
             {"method": "*", "regex": "^/api/admin/?$", "upstream": "new", "slice": "SLICE-0004"}]}


def test_route_table_chooses_first_match_and_falls_back_to_legacy():
    t = P.RouteTable(TABLE)
    assert t.choose("GET", "/api/products/7")[0] == "new"
    assert t.choose("HEAD", "/api/products/7?x=1")[0] == "new"             # HEAD follows GET, query ignored
    assert t.choose("GET", "/api/products/search")[0] == "legacy"         # more specific route listed first
    assert t.choose("DELETE", "/api/products/7") == ("legacy", None)      # method mismatch -> default
    assert t.choose("PATCH", "/api/admin")[0] == "new"                    # wildcard method
    assert t.choose("GET", "/unplanned") == ("legacy", None)
    assert t.upstreams["new"] == ("new", 8080)


@pytest.mark.parametrize("bad", [
    {"upstreams": {"legacy": "http://evil.example.com:80/x"}},
    {"upstreams": {"legacy": "https://legacy:3000"}},
    {"default": "nowhere"},
    {"routes": [{"method": "GET", "regex": "/no-anchor", "upstream": "new"}]},
    {"routes": [{"method": "GET", "regex": "^/x", "upstream": "elsewhere"}]},
    {"routes": [{"method": "GE T", "regex": "^/x", "upstream": "new"}]},
    {"schema_version": 2},
])
def test_route_table_rejects_invalid_tables(bad):
    with pytest.raises(P.RouteError):
        P.RouteTable({**TABLE, **bad})


def test_reloader_keeps_last_good_table(tmp_path):
    f = tmp_path / "routes.json"
    f.write_text(json.dumps(TABLE))
    logs = []
    r = P.Reloader(str(f), log=logs.append)
    assert len(r.get().routes) == 3
    f.write_text("{not json")
    assert len(r.get().routes) == 3 and any("kept previous" in m for m in logs)
    f.write_text(json.dumps({**TABLE, "routes": []}))
    assert r.get().routes == []


# ------------------------------------------------------------------ in-process HTTP (loopback only)
def _upstream(name: str):
    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            payload = json.dumps({"who": name, "method": self.command, "path": self.path,
                                  "body": body.decode(), "host": self.headers.get("Host")}).encode()
            self.send_response(404 if self.path.startswith("/missing") else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Upstream", name)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_proxy_forwards_to_the_routed_upstream_and_reloads(tmp_path):
    leg, new = _upstream("legacy"), _upstream("new")
    table = {"schema_version": 1, "default": "legacy",
             "upstreams": {"legacy": f"http://127.0.0.1:{leg.server_port}", "new": f"http://127.0.0.1:{new.server_port}"},
             "routes": [{"method": "POST", "regex": "^/api/products/?$", "upstream": "new", "slice": "SLICE-0002"}]}
    f = tmp_path / "routes.json"
    f.write_text(json.dumps(table))
    rt = ThreadingHTTPServer(("127.0.0.1", 0), P.make_handler(P.Reloader(str(f), log=lambda m: None)))
    threading.Thread(target=rt.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{rt.server_port}"
    try:
        r = urlopen(Request(base + "/api/products", data=b'{"name":"x"}', method="POST",
                            headers={"Content-Type": "application/json"}), timeout=5)
        d = json.loads(r.read())
        assert d["who"] == "new" and d["body"] == '{"name":"x"}' and r.headers["X-Upstream"] == "new"
        d = json.loads(urlopen(base + "/api/products?page=2", timeout=5).read())
        assert d["who"] == "legacy" and d["path"] == "/api/products?page=2"
        with pytest.raises(Exception) as ei:
            urlopen(base + "/missing", timeout=5)
        assert getattr(ei.value, "code", None) == 404                      # status passes through
        assert json.loads(urlopen(base + P.HEALTH_PATH, timeout=5).read())["ok"] is True
        table["routes"][0]["upstream"] = "legacy"                           # unflip: picked up without restart
        f.write_text(json.dumps(table) + " ")
        r = urlopen(Request(base + "/api/products", data=b"{}", method="POST"), timeout=5)
        assert json.loads(r.read())["who"] == "legacy"
        new.shutdown()
        new.server_close()
        table["routes"][0]["upstream"] = "new"
        f.write_text(json.dumps(table) + "  ")
        with pytest.raises(Exception) as ei:
            urlopen(Request(base + "/api/products", data=b"{}", method="POST"), timeout=5)
        assert getattr(ei.value, "code", None) == 502
    finally:
        rt.shutdown()
        leg.shutdown()


# ------------------------------------------------------------------ route table from the plan
def test_route_table_from_plan_and_flips(proj):
    t = H.write_routes(proj)
    assert t["flipped"] == [] and {r["upstream"] for r in t["routes"]} == {"legacy"}
    assert len(t["routes"]) == len(proj.eps)
    paths = [r["path"] for r in t["routes"]]
    assert paths.index("/api/orders/:id/cancel") < paths.index("/api/orders")
    rt = P.RouteTable(json.loads((proj.root / "harness" / "routes.json").read_text()))
    assert rt.choose("GET", "/api/products/3")[1].endpoint == proj.eps["GET /api/products/:id"]
    live = json.loads((proj.root / "harness" / "router" / "live" / "routes.json").read_text())
    assert live["routes"] == t["routes"]

    _approve(proj, "SLICE-0002")
    r = H.flip(proj, "SLICE-0002", actor="bob")
    assert not r["forced"] and r["warnings"] == ["depends on SLICE-0001, which is not approved"]
    t = json.loads((proj.root / "harness" / "routes.json").read_text())
    assert t["flipped"] == ["SLICE-0002"]
    for x in t["routes"]:
        assert x["upstream"] == ("new" if x["slice"] == "SLICE-0002" else "legacy")
    rt = P.RouteTable(t)
    assert rt.choose("PUT", "/api/products/9")[0] == "new" and rt.choose("GET", "/api/orders")[0] == "legacy"
    assert '"harness_flip"' in (proj.root / "audit" / "audit.jsonl").read_text()

    # approval withdrawn -> the flip is stale and the routes fall back to legacy
    (proj.root / "slices" / "SLICE-0002" / "approval.json").unlink()
    t = H.write_routes(proj)
    assert t["stale_flips"] == ["SLICE-0002"] and t["flipped"] == []
    assert all(x["upstream"] == "legacy" for x in t["routes"])

    H.unflip(proj, "SLICE-0002")
    assert H.load_flips(proj) == {}
    with pytest.raises(H.HarnessError, match="not flipped"):
        H.unflip(proj, "SLICE-0002")


def test_flip_rules(proj):
    with pytest.raises(H.HarnessError, match="not approved"):
        H.flip(proj, "SLICE-0003")
    assert not (proj.root / "harness" / "flips.json").exists()
    r = H.flip(proj, "SLICE-0003", force_for_testing=True, reason="sandbox experiment")
    assert r["forced"] and {x["upstream"] for x in r["routes"]} == {"new"}
    assert H.load_flips(proj)["SLICE-0003"]["forced"] is True
    assert '"harness_flip_forced"' in (proj.root / "audit" / "audit.jsonl").read_text()
    t = H.generate_routes(proj)
    assert t["flipped"] == ["SLICE-0003"] and t["stale_flips"] == []
    with pytest.raises(H.HarnessError, match="no HTTP endpoint"):
        H.flip(proj, "SLICE-0001", force_for_testing=True)
    with pytest.raises(H.HarnessError, match="not in the current plan"):
        H.flip(proj, "SLICE-0099", force_for_testing=True)
    with pytest.raises(H.HarnessError, match="invalid slice id"):
        H.flip(proj, "../../etc")
    _approve(proj, "SLICE-0002")
    (proj.root / "slices" / "SLICE-0002" / "approval.json").write_text('{"slice_id": "SLICE-0003"}')
    with pytest.raises(H.HarnessError, match="not approved"):          # invalid approval file is not an approval
        H.flip(proj, "SLICE-0002")


def test_flip_without_plan_is_refused(proj):
    (proj.root / "plan" / "migration_plan.json").unlink()
    with pytest.raises(H.HarnessError, match="no plan"):
        H.flip(proj, "SLICE-0002", force_for_testing=True)
    assert H.generate_routes(proj)["routes"] == []


def test_cli_flip_unflip_routes(proj):
    r = _cli("harness", "flip", proj.root, "SLICE-0002")
    assert r.exit_code != 0 and "not approved" in r.output
    _approve(proj, "SLICE-0002")
    r = _cli("harness", "flip", proj.root, "SLICE-0002", "--by", "carol")
    assert r.exit_code == 0 and "route(s) -> new" in r.output and "FORCED" not in r.output, r.output
    r = _cli("harness", "routes", proj.root)
    assert r.exit_code == 0 and "flipped: SLICE-0002" in r.output and "-> new" in r.output
    r = _cli("harness", "routes", proj.root, "--format", "json")
    assert json.loads(r.output)["flipped"] == ["SLICE-0002"]
    r = _cli("harness", "unflip", proj.root, "SLICE-0002")
    assert r.exit_code == 0 and "flipped now: none" in r.output
    r = _cli("harness", "flip", proj.root, "SLICE-0003", "--force-for-testing")
    assert r.exit_code == 0 and "FORCED" in r.output


# ------------------------------------------------------------------ --new defaults and compose rendering
def _spec(**extra) -> H.HarnessSpec:
    return H.HarnessSpec.model_validate(yaml.safe_load(
        (REPOS / "express-crud" / "harness" / "harness.yaml.in").read_text()) | extra)


def test_default_new_service_from_target_build_tool(proj):
    from migration_agent.migrate.target import load_target
    spec = _spec()
    t = load_target(proj)
    svc = H.default_new_service(spec, t)
    assert svc.image == "ma-express-crud:new" and svc.dockerfile == "new/Dockerfile"
    assert svc.build.command[-1].startswith("npm ci") and svc.port == 3000
    assert svc.synthetic_secrets == ["API_TOKEN"] and svc.env["PORT"] == "3000"
    assert "node" == svc.healthcheck.command[0]
    for build, start0 in (("maven", "sh"), ("go-mod", "./bin/service")):
        t2 = t.model_copy(update={"target": t.target.model_copy(update={"build": build})})
        assert H.default_new_service(spec, t2).start[0] == start0
    t3 = t.model_copy(update={"target": t.target.model_copy(update={"build": "cargo"})})
    with pytest.raises(H.HarnessError, match="no harness defaults"):
        H.default_new_service(spec, t3)
    df = H.default_new_dockerfile(t)
    assert "USER 10001:10001" in df and "TODO(M6): pin" in df
    assert not any(line.startswith("RUN ") for line in df.splitlines())
    t4 = t.model_copy(update={"target": t.target.model_copy(update={"base_image": "node:20\nRUN curl evil"})})
    with pytest.raises(H.HarnessError, match="invalid"):
        H.default_new_dockerfile(t4)


def _hardened(doc):
    assert doc["networks"] == {"internal": {"internal": True}}
    for name, svc in doc["services"].items():
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert svc["security_opt"] == ["no-new-privileges:true"] and svc["pull_policy"] == "never"
        assert "ports" not in svc and svc["networks"] == ["internal"] and svc["user"] != "0"
        assert all("docker.sock" not in v for v in svc.get("volumes", []))


def test_compose_new_and_composite(proj):
    spec = _spec()
    doc = H.render_compose(proj, spec, "new", "runs/RUN-0002/work/app")
    _hardened(doc)
    assert doc["services"]["new"]["image"] == "ma-express-crud:new"
    assert doc["services"]["new"]["volumes"] == ["../runs/RUN-0002/work/app:/work/app"]
    doc = H.render_compose(proj, spec, "composite", {"legacy": "runs/RUN-0001/work/app",
                                                     "new": "runs/RUN-0002/work/app"})
    _hardened(doc)
    assert set(doc["services"]) == {"legacy", "new", "router"} and doc["x-db-shared"] is False
    router = doc["services"]["router"]
    assert router["volumes"] == ["./router/live:/routes:ro"] and router["depends_on"] == ["legacy", "new"]
    assert router["entrypoint"] + router["command"] == H.router_argv(spec)
    assert doc["x-synthetic-secrets"] == ["API_TOKEN"]
    pg = _spec(db={"engine": "postgres", "image": "postgres:16.4-alpine", "command": ["postgres"],
                   "synthetic_secrets": ["POSTGRES_PASSWORD"]})
    doc = H.render_compose(None, pg, "composite", {"legacy": "a", "new": "b"}, new_service=pg.legacy)
    assert doc["x-db-shared"] is True and doc["services"]["new"]["depends_on"] == ["db"]
    assert "db" in doc["services"] and doc["services"]["legacy"]["depends_on"] == ["db"]
    with pytest.raises(H.HarnessError, match="workspaces"):
        H.render_compose(proj, spec, "composite", "one-workspace")


# ------------------------------------------------------------------ build / up / down with a scripted docker
def test_new_and_composite_build_up_down(proj, monkeypatch):
    monkeypatch.setattr(H, "replay_seeds", lambda *a, **k: {"requests": 0, "ok": True})   # seeds: see test_p2_seed.py
    fd = _fake(monkeypatch)
    assert H.build(proj, "legacy")["ok"]
    with pytest.raises(H.HarnessError, match="M4"):
        H.build(proj, "new")                                               # target_code/ not generated yet
    with pytest.raises(H.HarnessError, match="M6.*missing: new"):
        H.build(proj, "composite")
    _target_code(proj)
    ent = H.build(proj, "new")
    assert ent["ok"] and ent["image"] == "ma-express-crud:new"
    assert (proj.root / "harness" / "new" / "Dockerfile").read_text().startswith("# GENERATED")
    b = [c for c in fd.calls if c[0] == "build"][-1]
    assert b[b.index("-f") + 1].endswith("harness/new/Dockerfile") and b[b.index("--network") + 1] == "none"
    assert (proj.root / ent["workspace"] / "src" / "server.js").exists()
    assert yaml.safe_load((proj.root / "harness" / "compose.new.yml").read_text())["services"]["new"]

    _approve(proj, "SLICE-0002")
    H.flip(proj, "SLICE-0002")
    comp = H.build(proj, "composite")
    assert comp["ok"] and comp["image"] == "ma-router:1" and comp["routes_to_new"] == 5
    b = [c for c in fd.calls if c[0] == "build"][-1]
    assert b[b.index("-f") + 1].endswith("harness/router/Dockerfile") and "ma-router:1" in b
    assert (proj.root / "harness" / "router" / "proxy.py").exists()
    assert (proj.root / "harness" / "compose.composite.yml").exists()

    assert H.service_urls(proj) == {"legacy": None, "new": None, "composite": None}
    n = len(fd.calls)
    u = H.up(proj, "composite", sleep=lambda s: None)["up"]
    calls = fd.calls[n:]
    starts = [c for c in calls if c[0] == "run" and "--detach" in c]
    aliases = [flag_values(c, "--network-alias") for c in starts]
    assert aliases == [["legacy"], ["new"], ["router"]]
    assert {flag_values(c, "--network")[0] for c in starts} == {u["network"]}
    router = starts[-1]
    assert any("harness/router/live,target=/routes,readonly" in m for m in flag_values(router, "--mount"))
    assert [c for c in calls if c[0] == "exec"][-1][2:5] == ["python", "/app/proxy.py", "--check"]
    assert u["alias"] == "router" and u["port"] == 8080 and u["db_shared"] is False
    assert set(u["services"]) == {"legacy", "new", "router"}
    assert H.service_urls(proj) == {"legacy": None, "new": None, "composite": "http://router:8080"}
    st = H.status(proj, "composite")
    assert st["routes"]["flipped"] == ["SLICE-0002"] and st["routes"]["to_new"] == 5
    r = H.down(proj, "composite")
    assert set(u["containers"]) <= set(r["removed"])
    assert H.service_urls(proj)["composite"] is None

    u = H.up(proj, "new", sleep=lambda s: None)["up"]
    assert u["alias"] == "new" and H.service_urls(proj)["new"] == "http://new:3000"
    H.down(proj, "new")


def test_cli_composite_build(proj, monkeypatch):
    _fake(monkeypatch)
    assert _cli("harness", "build", proj.root).exit_code == 0
    _target_code(proj)
    assert _cli("harness", "build", proj.root, "--new").exit_code == 0
    r = _cli("harness", "build", proj.root, "--composite")
    assert r.exit_code == 0 and "router image ma-router:1 OK" in r.output and "NOT shared" in r.output, r.output


# ------------------------------------------------------------------ live docker (skipped without a daemon)
def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_ok(), reason="docker daemon not reachable")
def test_live_router_image_builds_offline(proj):
    from migration_agent.migrate.exec.docker import DockerBackend
    base = "python:3.12.7-alpine3.20"
    if subprocess.run(["docker", "image", "inspect", base], capture_output=True).returncode != 0:
        pytest.skip(f"base image {base} not present locally (builds never pull)")
    b = DockerBackend(proj)
    out = H.ensure_router_image(proj, H.load_spec(proj, adopt=True), b)
    assert out["image_digest"]
