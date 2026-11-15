"""HTTP / job wire protocol of the characterization runner. STANDARD LIBRARY ONLY, no package-relative imports:
this file is also copied (with netrunner.py) into a read-only folder mounted into the runner container
(`python:3.12.7-alpine3.20`, no pydantic), see `migrate.harness.run_cases_in_network`.

Safety rules (shared by the in-process runner and the in-network runner):
* Only the scheme://host:port of the base URL is ever contacted. A case path is joined to the base and the
  resulting URL must still point to that origin (absolute URLs, `//host` paths, control characters and `..`
  segments are refused). Environment proxies are ignored (ProxyHandler({})).
* Redirects are not followed by default (a 3xx is a recorded response). With follow_redirects only same-origin
  redirects are followed; any other target raises HostNotAllowed.
* Every request has a timeout; response bodies are capped (max_body_bytes).

`observe_raw(case, cfg)` executes one case given as plain JSON (CharacterizationCase.model_dump(mode="json"))
with a plain config dict and returns a plain observation dict:
    {"case_id", "kind", "outcome": "unrecorded" | "error" | "not_implemented",
     "observed": {"status", "headers" (ALL response headers, lower-case names), "body", "body_is_json",
                  "duration_s"} | None, "error": str, "error_class": str}
Header allowlisting and comparison happen in the trusted host process (harness_runner.runner.result_from_raw).
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_CTRL = re.compile(r"[\x00-\x20\x7f]")
_HDR_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SAFE_PATH = "/:@!$&'()*+,;=%?~-._"

DEFAULTS: dict[str, Any] = {"timeout_s": 10.0, "job_endpoint": "/__harness/job", "follow_redirects": False,
                            "max_redirects": 5, "max_body_bytes": 1_000_000, "extra_headers": {}}


class HostNotAllowed(ValueError):
    """A request or redirect would leave the configured base origin."""


class CaseInvalid(ValueError):
    pass


class ConfigError(RuntimeError):
    pass


class ProtocolError(RuntimeError):
    pass


def _origin(url: str) -> tuple[str, str, int]:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise HostNotAllowed(f"only http(s) URLs are allowed, got {u.scheme or '(none)'!r}")
    if not u.hostname:
        raise HostNotAllowed("URL has no host")
    if u.username is not None or u.password is not None:
        raise HostNotAllowed("credentials in URLs are not allowed")
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError as e:
        raise HostNotAllowed(f"invalid port: {e}") from None
    return u.scheme, u.hostname.lower(), port


def parse_base_url(base_url: str) -> tuple[tuple[str, str, int], str]:
    """(origin, path prefix) of a base URL; raises HostNotAllowed for anything unusable."""
    if _CTRL.search(base_url or ""):
        raise HostNotAllowed("base URL contains whitespace or control characters")
    u = urllib.parse.urlsplit(base_url)
    if u.query or u.fragment:
        raise HostNotAllowed("base URL must not have a query or fragment")
    return _origin(base_url), u.path.rstrip("/")


def build_url(base_url: str, path: str, query: dict | None = None) -> str:
    """Join a case path to the base URL, refusing anything that would reach another origin."""
    origin, prefix = parse_base_url(base_url)
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise HostNotAllowed(f"case path must be origin-relative (start with a single '/'): {path!r}")
    if _CTRL.search(path) or "\\" in path:
        raise CaseInvalid("case path contains whitespace, control characters or backslashes")
    raw_path = path.split("?", 1)[0]
    if any(seg in ("..", ".") for seg in urllib.parse.unquote(raw_path).split("/")):
        raise CaseInvalid("case path must not contain '.' or '..' segments")
    url = f"{origin[0]}://{urllib.parse.urlsplit(base_url).netloc}{prefix}{urllib.parse.quote(path, safe=_SAFE_PATH)}"
    if query:
        qs = urllib.parse.urlencode({k: v for k, v in sorted(query.items())}, doseq=True)
        url += ("&" if "?" in url else "?") + qs
    if _origin(url) != origin:
        raise HostNotAllowed(f"request would leave the base origin {origin[1]}:{origin[2]}")
    return url


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, follow: bool, max_redirects: int, origin: tuple[str, str, int]):
        self.follow, self.origin = follow, origin
        self.max_redirections = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.follow:
            return None          # urllib raises HTTPError: the 3xx itself becomes the observed response
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != self.origin:
            raise HostNotAllowed(f"redirect to another host refused: {urllib.parse.urlsplit(target).netloc}")
        return super().redirect_request(req, fp, code, msg, headers, target)


def check_headers(headers: dict) -> dict[str, str]:
    for k, v in headers.items():
        if not _HDR_NAME.match(str(k)) or re.search(r"[\r\n\x00]", str(v)):
            raise CaseInvalid(f"invalid header {k!r}")
    return {str(k): str(v) for k, v in headers.items()}


def encode_body(body: Any, headers: dict[str, str]) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    if not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def decode(raw: bytes, content_type: str) -> tuple[Any, bool]:
    if not raw:
        return None, False
    text = raw.decode("utf-8", errors="replace")
    if "json" in (content_type or "").lower():
        try:
            return json.loads(text), True
        except ValueError:
            return text, False
    return text, False


def _cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if v is not None or k == "job_endpoint"})
    if not out.get("base_url"):
        raise ConfigError("no base_url configured")
    return out


def exchange(cfg: dict, method: str, path: str, *, headers: dict | None = None, query: dict | None = None,
             body: Any = None) -> dict:
    """One HTTP request to the base origin -> {status, headers (all, lower-case), body, body_is_json, duration_s}.
    Raises HostNotAllowed / CaseInvalid / OSError-family errors."""
    c = _cfg(cfg)
    origin, _ = parse_base_url(c["base_url"])
    url = build_url(c["base_url"], path, query)
    if not re.fullmatch(r"[A-Z]{3,10}", str(method).upper()):
        raise CaseInvalid(f"invalid HTTP method {method!r}")
    hdrs = check_headers({**(c.get("extra_headers") or {}), **(headers or {})})
    data = encode_body(body, hdrs)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=str(method).upper())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         _Redirects(bool(c["follow_redirects"]), int(c["max_redirects"]), origin))
    cap = int(c["max_body_bytes"])
    t0 = time.monotonic()
    try:
        resp = opener.open(req, timeout=float(c["timeout_s"]))
    except urllib.error.HTTPError as e:       # non-2xx (and unfollowed 3xx) is a normal response
        resp = e
    try:
        status = resp.getcode() if hasattr(resp, "getcode") else resp.code
        raw = resp.read(cap + 1) or b""
        hdr_items = list(resp.headers.items()) if resp.headers is not None else []
    finally:
        resp.close()
    if len(raw) > cap:
        raise CaseInvalid(f"response body larger than {cap} bytes")
    all_headers = {k.lower(): v for k, v in hdr_items}
    parsed, is_json = decode(raw, all_headers.get("content-type", ""))
    return {"status": status, "headers": all_headers, "body": parsed, "body_is_json": is_json,
            "duration_s": round(time.monotonic() - t0, 4)}


def job_exchange(cfg: dict, job: str, args: list) -> dict:
    c = _cfg(cfg)
    if not c.get("job_endpoint"):
        raise ConfigError("no harness job endpoint configured (RunnerConfig.job_endpoint)")
    obs = exchange(c, "POST", c["job_endpoint"], body={"job": job, "args": list(args or [])})
    body = obs["body"]
    if obs["status"] != 200 or not isinstance(body, dict) or not isinstance(body.get("exit_code"), int) \
            or isinstance(body.get("exit_code"), bool):
        raise ProtocolError(f"harness job endpoint answered {obs['status']} without an integer exit_code")
    return {"status": body["exit_code"], "headers": {}, "body": body.get("output"), "body_is_json": True,
            "duration_s": obs["duration_s"]}


def observe_raw(case: dict, cfg: dict) -> dict:
    """Execute one case (plain JSON) and return the plain observation dict (see module docstring)."""
    kind = str(case.get("kind", ""))
    out: dict[str, Any] = {"case_id": str(case.get("id", "")), "kind": kind, "outcome": "unrecorded",
                           "observed": None, "error": "", "error_class": ""}

    def err(msg: str, cls: str) -> dict:
        out.update(outcome="error", error=msg, error_class=cls)
        return out

    if kind in ("grpc", "message"):
        out.update(outcome="not_implemented", error=(
            "gRPC cases are not supported by the runner yet (needs a gRPC client and the proto descriptors)"
            if kind == "grpc" else
            "message cases are not supported by the runner yet (needs an in-sandbox broker adapter)"))
        return out
    req = case.get("request") or {}
    timeout = _cfg(cfg)["timeout_s"]
    try:
        if kind in ("http", "replay"):
            obs = exchange(cfg, req.get("method", ""), req.get("path", ""), headers=req.get("headers") or {},
                           query=req.get("query") or {}, body=req.get("body"))
        elif kind == "job":
            obs = job_exchange(cfg, req.get("job", ""), req.get("args") or [])
        else:
            return err(f"unknown case kind {kind!r}", "invalid_case")
    except HostNotAllowed as e:
        return err(str(e), "host_refused")
    except CaseInvalid as e:
        return err(str(e), "invalid_case")
    except ConfigError as e:
        return err(str(e), "config")
    except ProtocolError as e:
        return err(str(e), "protocol")
    except (socket.timeout, TimeoutError) as e:
        return err(f"timeout after {timeout}s ({e})", "timeout")
    except urllib.error.URLError as e:
        reason = e.reason
        cls = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "transport"
        if isinstance(reason, HostNotAllowed):
            cls = "host_refused"
        return err(f"{type(reason).__name__}: {reason}", cls)
    except (OSError, ValueError) as e:     # connection reset, bad status line ...
        return err(f"{type(e).__name__}: {e}", "transport")
    out["observed"] = obs
    return out
