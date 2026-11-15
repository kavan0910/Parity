"""Strangler-fig router: a small stdlib HTTP reverse proxy (runs inside the sandbox router container).

    python proxy.py --routes /routes/routes.json --port 8080
    python proxy.py --check --port 8080          # health probe used by `harness up --composite`

routes.json (written by `migrate/harness.py`, never by hand):

    {"schema_version": 1, "default": "legacy",
     "upstreams": {"legacy": "http://legacy:3000", "new": "http://new:3000"},
     "routes": [{"method": "GET", "regex": "^/api/products/?$", "upstream": "new",
                 "slice": "SLICE-0003", "endpoint": "EP-0005", "path": "/api/products"}, ...]}

Routes are tried in file order (the generator sorts the most specific paths first); the first route whose method and
regex match decides the upstream; anything else goes to `default`. The file is re-read when its mtime or size
changes; an invalid file keeps the last good table (and is logged). Requests and responses are forwarded verbatim
(no added headers) except hop-by-hop headers; bodies are buffered (cap MAX_BODY) and sent with Content-Length.
Upstreams must be plain `http://host:port` on the internal network.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HEALTH_PATH = "/__ma_router/health"
MAX_BODY = 50 * 1024 * 1024
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                        "trailers", "transfer-encoding", "upgrade", "proxy-connection"})
_METHOD = re.compile(r"^(\*|[A-Z]{1,16})$")
_UPSTREAM = re.compile(r"^http://([a-z0-9][a-z0-9.-]{0,62}):(\d{1,5})$")


class RouteError(ValueError):
    pass


class Route:
    __slots__ = ("method", "regex", "upstream", "slice", "endpoint", "path")

    def __init__(self, d: dict, upstreams: dict):
        if not isinstance(d, dict):
            raise RouteError("route must be an object")
        self.method = str(d.get("method", "*")).upper()
        if not _METHOD.match(self.method):
            raise RouteError(f"invalid method {self.method!r}")
        rx = d.get("regex")
        if not isinstance(rx, str) or not rx.startswith("^") or len(rx) > 1000:
            raise RouteError(f"invalid regex {rx!r}")
        self.regex = re.compile(rx)
        self.upstream = d.get("upstream")
        if self.upstream not in upstreams:
            raise RouteError(f"unknown upstream {self.upstream!r}")
        self.slice = str(d.get("slice", ""))
        self.endpoint = str(d.get("endpoint", ""))
        self.path = str(d.get("path", ""))

    def matches(self, method: str, path: str) -> bool:
        if self.method != "*" and self.method != method and not (method == "HEAD" and self.method == "GET"):
            return False
        return self.regex.match(path) is not None


class RouteTable:
    """Parsed, validated routes.json. `choose(method, path) -> (upstream name, Route | None)`."""

    def __init__(self, data: dict):
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise RouteError("routes.json must be an object with schema_version 1")
        ups = data.get("upstreams")
        if not isinstance(ups, dict) or not ups:
            raise RouteError("upstreams missing")
        self.upstreams: dict[str, tuple[str, int]] = {}
        for name, url in ups.items():
            m = _UPSTREAM.match(str(url))
            if not m or not 0 < int(m.group(2)) < 65536:
                raise RouteError(f"upstream {name}: {url!r} is not http://host:port")
            self.upstreams[str(name)] = (m.group(1), int(m.group(2)))
        self.default = data.get("default", "legacy")
        if self.default not in self.upstreams:
            raise RouteError(f"default upstream {self.default!r} unknown")
        routes = data.get("routes", [])
        if not isinstance(routes, list):
            raise RouteError("routes must be a list")
        self.routes = [Route(r, self.upstreams) for r in routes]

    @classmethod
    def from_file(cls, path: str) -> "RouteTable":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def choose(self, method: str, target: str) -> tuple[str, Route | None]:
        path = urlsplit(target).path or "/"
        m = method.upper()
        for r in self.routes:
            if r.matches(m, path):
                return r.upstream, r
        return self.default, None


class Reloader:
    """Holds the current table; reloads when the file changes, keeps the last good table on errors."""

    def __init__(self, path: str, log=None):
        self.path = path
        self._sig = None
        self._table: RouteTable | None = None
        self._lock = threading.Lock()
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def get(self) -> RouteTable:
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError as e:
            if self._table is None:
                raise RouteError(f"routes file unavailable: {e}") from None
            return self._table
        with self._lock:
            if sig != self._sig:
                try:
                    self._table = RouteTable.from_file(self.path)
                    self._log(f"router: loaded {len(self._table.routes)} route(s)")
                except (OSError, ValueError) as e:
                    self._log(f"router: invalid routes file kept previous table: {e}")
                    if self._table is None:
                        raise RouteError(str(e)) from None
                self._sig = sig
            return self._table


def _read_body(rfile, headers) -> bytes:
    te = (headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        out = bytearray()
        while True:
            line = rfile.readline(1024).strip()
            size = int(line.split(b";", 1)[0] or b"0", 16)
            if size == 0:
                while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(out)
            out += rfile.read(size)
            rfile.readline(8)
            if len(out) > MAX_BODY:
                raise ValueError("request body too large")
    n = int(headers.get("Content-Length") or 0)
    if n > MAX_BODY:
        raise ValueError("request body too large")
    return rfile.read(n) if n > 0 else b""


def make_handler(reloader: Reloader, timeout: float = 60.0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ma-router"

        def log_message(self, fmt, *args):  # noqa: D401 - concise access log on stderr
            sys.stderr.write("router: " + (fmt % args) + "\n")

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self) -> None:
            if self.path == HEALTH_PATH:
                try:
                    t = reloader.get()
                    return self._reply(200, {"ok": True, "routes": len(t.routes)})
                except RouteError as e:
                    return self._reply(503, {"ok": False, "error": str(e)})
            try:
                table = reloader.get()
                body = _read_body(self.rfile, self.headers)
            except (RouteError, ValueError) as e:
                return self._reply(502 if isinstance(e, RouteError) else 413, {"error": f"router: {e}"})
            name, route = table.choose(self.command, self.path)
            host, port = table.upstreams[name]
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
                       and k.lower() != "content-length"}
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            try:
                conn.request(self.command, self.path, body=body or None, headers=headers)
                resp = conn.getresponse()
                data = resp.read() if self.command != "HEAD" else b""
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                sys.stderr.write(f"router: {self.command} {self.path} -> {name} failed: {type(e).__name__}\n")
                return self._reply(502, {"error": f"router: upstream {name} unavailable"})
            conn.close()
            sys.stderr.write(f"router: {self.command} {self.path} -> {name}"
                             f"{' (' + route.slice + ')' if route and route.slice else ''} {resp.status}\n")
            self.send_response_only(resp.status, resp.reason)
            length_sent = False
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP:
                    continue
                if lk == "content-length":
                    if self.command == "HEAD":
                        self.send_header(k, v)
                        length_sent = True
                    continue
                self.send_header(k, v)
            if not length_sent:
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy

    return Handler


def serve(routes: str, port: int, host: str = "0.0.0.0") -> None:  # noqa: S104 - internal network only
    reloader = Reloader(routes)
    reloader.get()
    srv = ThreadingHTTPServer((host, port), make_handler(reloader))
    srv.daemon_threads = True
    sys.stderr.write(f"router: listening on {host}:{port}\n")
    srv.serve_forever()


def check(port: int) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", HEALTH_PATH)
        return 0 if conn.getresponse().status == 200 else 1
    except OSError:
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ma strangler router")
    ap.add_argument("--routes", default="/routes/routes.json")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check(a.port)
    serve(a.routes, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
