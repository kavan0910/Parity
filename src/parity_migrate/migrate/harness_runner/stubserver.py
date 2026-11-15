"""Tiny stdlib HTTP stub server for external systems (`external_dependency` / `integration` nodes).

Stub files live in characterization/stubs/<NODE-ID>/<name>.json (model `StubDef`), reviewed and versioned
with the cases. A case references them as `setup.stubs: ["EXT-0002/payment_ok"]`.

Routing: the legacy service is configured (by the harness) to call `http://<stub-host>:<port>/<NODE-ID>/...`.
A request to `/<NODE-ID>/<rest>` is answered by the first active stub of that node whose `request.method`
matches and whose `request.path` equals `/<rest>` (a `*` path segment matches any one segment; a stub with
`request.query` requires those query values). Unmatched requests get 404 {"error": "no stub"}.

Control endpoints (loopback test harness only):
    GET  /__stub/calls      -> [{"stub": "EXT-0002/payment_ok" | null, "method", "path", "query", "body"}]
    POST /__stub/reset      -> clears the call log and activates every stub
    POST /__stub/activate   {"stubs": ["EXT-0002/payment_ok", ...]} -> only these answer
The call log is what `Expectation.stub_calls` is recorded from.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NODE_DIR = re.compile(r"^(EXT|INT)-\d{4,}$")
STUB_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
MAX_REQUEST_BYTES = 1_000_000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StubRequest(_Strict):
    method: str = "GET"
    path: str = Field(min_length=1)
    query: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _p(cls, v: str) -> str:
        if not v.startswith("/") or ".." in v.split("/"):
            raise ValueError("stub path must start with '/' and have no '..'")
        return v


class StubResponse(_Strict):
    status: int = Field(default=200, ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class StubDef(_Strict):
    """characterization/stubs/<NODE-ID>/<name>.json"""
    schema_version: int = 1
    node_id: str
    name: str
    request: StubRequest
    response: StubResponse = Field(default_factory=StubResponse)
    provenance: str = "generated"        # generated | recorded | human
    reviewed_by: str = ""

    @field_validator("node_id")
    @classmethod
    def _node(cls, v: str) -> str:
        if not NODE_DIR.match(v):
            raise ValueError("stub node_id must be an EXT- or INT- id")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not STUB_NAME.match(v):
            raise ValueError("stub name must be lower-case [a-z0-9_-], at most 64 chars")
        return v

    @property
    def ref(self) -> str:
        return f"{self.node_id}/{self.name}"


def stub_path(stubs_dir: Path, node_id: str, name: str) -> Path:
    if not NODE_DIR.match(node_id) or not STUB_NAME.match(name):
        raise ValueError(f"invalid stub reference {node_id}/{name}")
    return Path(stubs_dir) / node_id / f"{name}.json"


def load_stubs(stubs_dir: Path) -> list[StubDef]:
    """Every valid stub, sorted by reference. Invalid files raise (stubs are reviewed artifacts)."""
    out: list[StubDef] = []
    d = Path(stubs_dir)
    if not d.is_dir():
        return out
    for node_dir in sorted(p for p in d.iterdir() if p.is_dir() and NODE_DIR.match(p.name)):
        for f in sorted(node_dir.glob("*.json")):
            try:
                s = StubDef.model_validate_json(f.read_text())
            except ValidationError as e:
                raise ValueError(f"invalid stub {f}: {e.errors()[0]['msg']}") from None
            if s.node_id != node_dir.name or s.name != f.stem:
                raise ValueError(f"stub {f} does not match its location ({s.ref})")
            out.append(s)
    return out


def _seg_match(pattern: str, path: str) -> bool:
    a, b = pattern.strip("/").split("/"), path.strip("/").split("/")
    return len(a) == len(b) and all(x == "*" or x == y for x, y in zip(a, b))


class StubServer:
    """`with StubServer(stubs_dir) as s: s.base_url` (binds 127.0.0.1 by default, port 0 = ephemeral)."""

    def __init__(self, stubs_dir: Path | None = None, *, stubs: list[StubDef] | None = None,
                 host: str = "127.0.0.1", port: int = 0):
        self.stubs = list(stubs) if stubs is not None else load_stubs(Path(stubs_dir or "."))
        self.active: set[str] | None = None
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        h, p = self._httpd.server_address[:2]
        return f"http://{h}:{p}"

    def match(self, method: str, raw_path: str) -> StubDef | None:
        u = urllib.parse.urlsplit(raw_path)
        parts = u.path.lstrip("/").split("/", 1)
        if not parts or not NODE_DIR.match(parts[0]):
            return None
        node, rest = parts[0], "/" + (parts[1] if len(parts) > 1 else "")
        query = dict(urllib.parse.parse_qsl(u.query))
        for s in self.stubs:
            if s.node_id != node or (self.active is not None and s.ref not in self.active):
                continue
            if s.request.method.upper() != method.upper() or not _seg_match(s.request.path, rest):
                continue
            if any(query.get(k) != v for k, v in s.request.query.items()):
                continue
            return s
        return None

    def _handler(self):
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):   # quiet
                pass

            def _send(self, status: int, body: Any, headers: dict[str, str] | None = None):
                raw = b"" if body is None else (body.encode() if isinstance(body, str)
                                               else json.dumps(body).encode())
                self.send_response(status)
                hdrs = {"Content-Type": "application/json"} if body is not None and not isinstance(body, str) else {}
                hdrs.update(headers or {})
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            def _body(self):
                n = min(int(self.headers.get("Content-Length") or 0), MAX_REQUEST_BYTES)
                raw = self.rfile.read(n) if n > 0 else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return raw.decode("utf-8", errors="replace")

            def _handle(self):
                body = self._body()
                path = self.path
                if path.startswith("/__stub/"):
                    return self._control(path, body)
                s = server.match(self.command, path)
                u = urllib.parse.urlsplit(path)
                with server._lock:
                    server.calls.append({"stub": s.ref if s else None, "method": self.command, "path": u.path,
                                         "query": dict(urllib.parse.parse_qsl(u.query)), "body": body})
                if s is None:
                    return self._send(404, {"error": "no stub"})
                self._send(s.response.status, s.response.body, s.response.headers)

            def _control(self, path: str, body):
                if path == "/__stub/calls" and self.command == "GET":
                    with server._lock:
                        return self._send(200, list(server.calls))
                if path == "/__stub/reset" and self.command == "POST":
                    with server._lock:
                        server.calls.clear()
                        server.active = None
                    return self._send(200, {"ok": True})
                if path == "/__stub/activate" and self.command == "POST":
                    refs = body.get("stubs") if isinstance(body, dict) else None
                    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
                        return self._send(400, {"error": "expected {\"stubs\": [..]}"})
                    known = {s.ref for s in server.stubs}
                    unknown = sorted(set(refs) - known)
                    if unknown:
                        return self._send(400, {"error": "unknown stubs", "stubs": unknown})
                    with server._lock:
                        server.active = set(refs)
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "unknown control endpoint"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

        return H

    def start(self) -> "StubServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="stub-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
