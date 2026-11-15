"""Shared builders for the M3 (characterization tests) tests: an approved express-crud project with a
business rule and decisions, and an in-process stdlib "legacy service" on 127.0.0.1."""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from migration_agent.graph import load_latest, save_snapshot
from migration_agent.graph.models import Edge, Evidence, Node
from migration_agent.project import Project
from migration_agent.review.approval import approve
from migration_agent.review.decisions import add_decision
from migration_agent.review.docrender import write_latest
from migration_agent.review.resolutions import add_resolution

from phase1_helpers import make_project, write_findings

RULE_ID = "RULE-0001"
CANCEL_EP = "EP-0003"


def m3_project(tmp_path: Path, tmp_path_factory, *, fix_bug: bool = True, deferred: bool = False) -> Project:
    """express-crud, offline-analysed, plus RULE-0001 ("cancelling a shipped order returns 409") linked to
    EP-0003, the phase1 quirk (COMP-0001) and suspected bug (COMP-0002); bug decision `fix` by default;
    with `deferred`, finding F-0001 on the bug node is deferred. Approved by alice."""
    p = make_project(tmp_path, tmp_path_factory)
    g = load_latest(p.graph_dir)
    rule = Node(id=RULE_ID, type="business_rule", name="cancel shipped order",
                module="src/routes/orders.js", qualified_symbol="cancel#shipped-409",
                responsibility="Cancelling an order that is already shipped returns 409 Conflict",
                evidence=[Evidence(file="src/routes/orders.js", line_start=16, line_end=16)])
    n = max([int(e.id.split("-")[1]) for e in g.edges] or [0]) + 1
    edge = Edge(id=f"EDGE-{n:04d}", source=CANCEL_EP, target=RULE_ID, type="implements")
    save_snapshot(p.graph_dir, g.model_copy(update={"nodes": [*g.nodes, rule], "edges": [*g.edges, edge]}))
    if fix_bug:
        add_decision(p, p.bug_id, "fix", "return a proper error")
    if deferred:
        write_findings(p, p.bug_id)
        add_resolution(p, "F-0001", "deferred", "post-migration")
        add_resolution(p, "F-0002", "dismissed")
    write_latest(p)
    approve(p, "alice")
    return p


class LegacyService:
    """Tiny express-crud look-alike. Noise: `generated_at` and `orders[*].seen_at` change on every request;
    `instance` differs per server. Quirk: cancelling always answers 200 (the rule says 409). /api/boom is 500.
    `mode="regressed"` changes the products list; `flaky_status=True` alternates 200/503 on /api/orders."""

    def __init__(self, instance: str = "a", mode: str = "normal", flaky_status: bool = False, delay_s: float = 0.0):
        self.instance, self.mode, self.flaky, self.delay = instance, mode, flaky_status, delay_s
        self.requests: list[tuple[str, str, dict]] = []
        self._n = 0
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._httpd.daemon_threads = True
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    def respond(self, method: str, path: str, headers: dict, body):
        ts = time.time_ns()
        auth_ok = headers.get("authorization") == "Bearer good"
        if path == "/__harness/job" and method == "POST":
            return 200, {"exit_code": 0 if body.get("job") == "nightly" else 2,
                         "output": {"processed": 3, "job": body.get("job"), "at": ts}}
        if path == "/api/orders" and method == "GET":
            self._n += 1
            if self.flaky and self._n % 2 == 0:
                return 503, {"error": "busy"}
            return 200, {"orders": [{"id": 1, "status": "open", "seen_at": ts},
                                    {"id": 2, "status": "shipped", "seen_at": ts + 1}],
                         "generated_at": ts, "instance": self.instance}
        if path == "/api/orders" and method == "POST":
            if not isinstance(body, dict):
                return 400, {"error": "invalid body"}
            return 201, {"id": 3, "status": "open", "created_at": ts}
        m = re.fullmatch(r"/api/orders/(\d+)/cancel", path)
        if m and method == "POST":
            if m.group(1) not in ("1", "2"):
                return 404, None
            return 200, {"cancelled": True}           # quirk: even a shipped order is "cancelled"
        if path == "/api/products" and method == "GET":
            items = [{"id": 1, "name": "pen"}]
            if self.mode == "regressed":
                items = [{"id": 1, "name": "PEN"}]
            return 200, items
        m = re.fullmatch(r"/api/products/(\d+)", path)
        if m and method == "GET":
            return (200, {"id": 1, "name": "pen"}) if m.group(1) == "1" else (404, None)
        if path.startswith("/api/products") and method in ("POST", "PUT", "DELETE"):
            if not auth_ok:
                return 401, {"error": "unauthorized"}
            return 204, None
        if path == "/api/boom":
            return 500, {"error": "internal"}
        if path == "/redirect-out":
            return 302, None, {"Location": "http://example.invalid/steal"}
        if path == "/redirect-in":
            return 302, None, {"Location": "/api/products"}
        if path == "/slow":
            time.sleep(1.0)
            return 200, {"slow": True}
        return 404, {"error": "not found"}

    def _handler(self):
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                if svc.delay:
                    time.sleep(svc.delay)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode()
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                path = self.path.split("?", 1)[0]
                svc.requests.append((self.command, self.path, hdrs))
                out = svc.respond(self.command, path, hdrs, body if body is not None else {})
                status, payload = out[0], out[1]
                extra = out[2] if len(out) > 2 else {}
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("X-Request-Id", str(time.time_ns()))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _go

        return H
