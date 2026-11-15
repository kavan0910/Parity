"""M3 harness runner: JSONPath masks, JSON-semantic comparison, origin jail, redirects, jobs, stub server."""
import json

import pytest

from parity_migrate.migrate.harness_runner import (
    MASK, CaseResult, HostNotAllowed, MaskError, Observed, RunnerConfig, StubDef, StubServer, apply_masks, build_url,
    compare, diff_paths, generalize, json_equal, load_stubs, observe, run_case, stub_path, validate_mask)
from parity_migrate.migrate.harness_runner.__main__ import main as runner_main
from parity_migrate.migrate.models import CharacterizationCase, Expectation

from p2_m3_helpers import LegacyService


def _case(path="/api/products", method="GET", kind="http", cid="CT-0001", **kw):
    req = kw.pop("request", None) or {"method": method, "path": path, **kw.pop("req", {})}
    return CharacterizationCase(id=cid, kind=kind, request=req, **kw)


@pytest.fixture
def legacy():
    with LegacyService() as svc:
        yield svc


# ------------------------------------------------------------------ jsonpath
def test_mask_syntax_subset():
    assert validate_mask("$.a.b") == "$.a.b"
    assert validate_mask("$.items[*].id") == "$.items[*].id"
    assert validate_mask("$['a key'][0]") == "$['a key'][0]"
    assert validate_mask("headers.Date") == "headers.date"
    for bad in ("a.b", "$..a", "$.a[?(@.x)]", "$.a[-1]", "$[1:2]"):
        with pytest.raises(MaskError):
            validate_mask(bad)


def test_apply_masks_and_generalize():
    body = {"ts": 1, "items": [{"id": 1, "at": "x"}, {"id": 2, "at": "y"}], "meta": {"a": 1, "b": 2}}
    out = apply_masks(body, ["$.ts", "$.items[*].at", "$.meta.*", "headers.date", "$.missing.x"])
    assert out == {"ts": MASK, "items": [{"id": 1, "at": MASK}, {"id": 2, "at": MASK}],
                   "meta": {"a": MASK, "b": MASK}}
    assert body["ts"] == 1                                     # input untouched
    a = {"items": [{"t": 1}, {"t": 2}], "x": 1}
    b = {"items": [{"t": 3}, {"t": 4}], "x": 1}
    assert generalize(diff_paths(a, b)) == ["$.items[*].t"]
    assert generalize(diff_paths([1, 2], [1, 2, 3])) == ["$"]
    assert apply_masks("text", ["$"]) == MASK


def test_json_semantic_equality():
    assert json_equal({"a": 1, "b": [1, 2]}, {"b": [1, 2.0], "a": 1.0})
    assert not json_equal({"a": True}, {"a": 1})
    assert not json_equal({"a": "1"}, {"a": 1})
    assert not json_equal([1, 2], [2, 1])
    assert not json_equal({"a": None}, {})


def test_compare_status_headers_body():
    exp = Expectation(status=200, headers={"Content-Type": "application/json; charset=UTF-8", "Date": "x"},
                      body={"id": 1, "ts": 5})
    obs = Observed(status=200, headers={"content-type": "application/json;charset=utf-8", "date": "y"},
                   body={"ts": 9, "id": 1})
    allow = ["content-type", "location"]
    assert [d.field for d in compare(exp, obs, [], allow)] == ["body.$.ts"]
    assert compare(exp, obs, ["$.ts"], allow) == []                         # date is not allowlisted
    obs2 = obs.model_copy(update={"status": 201, "headers": {"content-type": "text/plain", "location": "/x"}})
    fields = [d.field for d in compare(exp, obs2, ["$.ts"], allow)]
    assert fields == ["status", "headers.content-type", "headers.location"]
    assert [d.field for d in compare(exp, obs2, ["$.ts", "headers.content-type", "headers.location"], allow)] == ["status"]
    spec = Expectation(source="specified", status=201, headers={}, body={"id": 1, "ts": 0})
    assert compare(spec, obs2, ["$.ts"], allow) == []          # a spec checks only the headers it names


# ------------------------------------------------------------------ origin jail
@pytest.mark.parametrize("path", ["http://evil.example/x", "//evil.example/x", "https://127.0.0.1:1/x", "evil"])
def test_build_url_refuses_other_hosts(path):
    with pytest.raises(HostNotAllowed):
        build_url("http://127.0.0.1:8080", path)


def test_build_url_refuses_bad_bases_and_paths():
    for base in ("ftp://127.0.0.1/", "http://user:pw@127.0.0.1/", "http:///x", "http://127.0.0.1/?a=1", "file:///etc"):
        with pytest.raises(HostNotAllowed):
            build_url(base, "/a")
    with pytest.raises(ValueError):
        build_url("http://127.0.0.1:8080", "/a/../../etc/passwd")
    with pytest.raises(ValueError):
        build_url("http://127.0.0.1:8080", "/a b")
    assert build_url("http://127.0.0.1:8080/base/", "/api/x", {"b": "2", "a": ["1", "3"]}) == \
        "http://127.0.0.1:8080/base/api/x?a=1&a=3&b=2"


def test_runner_refuses_other_hosts(legacy):
    cfg = RunnerConfig(base_url=legacy.base_url)
    for path in ("http://evil.example/x", "//evil.example/x"):
        res = run_case(_case(path), cfg)
        assert res.outcome == "error" and res.error_class == "host_refused"
    assert legacy.requests == []


def test_redirect_to_other_host_is_not_followed(legacy):
    cfg = RunnerConfig(base_url=legacy.base_url)
    res = observe(_case("/redirect-out"), cfg)
    assert res.observed.status == 302 and res.observed.headers["location"] == "http://example.invalid/steal"
    follow = cfg.model_copy(update={"follow_redirects": True})
    res = observe(_case("/redirect-out"), follow)
    assert res.outcome == "error" and res.error_class == "host_refused"
    res = observe(_case("/redirect-in"), follow)                     # same origin: followed
    assert res.observed.status == 200 and res.observed.body == [{"id": 1, "name": "pen"}]


def test_environment_proxy_is_ignored(legacy, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert observe(_case("/api/products"), RunnerConfig(base_url=legacy.base_url)).observed.status == 200


def test_timeout_and_connection_errors(legacy):
    res = observe(_case("/slow"), RunnerConfig(base_url=legacy.base_url, timeout_s=0.2))
    assert res.outcome == "error" and res.error_class == "timeout"
    res = observe(_case("/x"), RunnerConfig(base_url="http://127.0.0.1:9"))
    assert res.outcome == "error" and res.error_class == "transport"


# ------------------------------------------------------------------ outcomes and kinds
def test_unrecorded_pass_fail(legacy):
    cfg = RunnerConfig(base_url=legacy.base_url)
    c = _case("/api/orders")
    res = run_case(c, cfg)
    assert res.outcome == "unrecorded" and res.observed.status == 200 and res.observed.body_is_json
    assert "x-request-id" not in res.observed.headers                # not in the header allowlist
    exp = Expectation(status=200, headers=res.observed.headers, body=res.observed.body)
    rec = c.model_copy(update={"expect": exp})
    assert run_case(rec, cfg).outcome == "fail"                      # timestamps differ
    masked = rec.model_copy(update={"normalize": ["$.generated_at", "$.orders[*].seen_at"]})
    assert run_case(masked, cfg).outcome == "pass"
    wrong = masked.model_copy(update={"expect": exp.model_copy(update={"status": 201})})
    r = run_case(wrong, cfg)
    assert r.outcome == "fail" and r.diffs[0].field == "status"


def test_http_body_and_headers_are_sent(legacy):
    cfg = RunnerConfig(base_url=legacy.base_url, extra_headers={"X-Harness-Seed": "1"})
    c = _case("/api/products/1", method="PUT", req={"headers": {"Authorization": "Bearer good"}, "body": {"a": 1}})
    assert observe(c, cfg).observed.status == 204
    m, path, hdrs = legacy.requests[-1]
    assert (m, path, hdrs["content-type"], hdrs["x-harness-seed"]) == ("PUT", "/api/products/1", "application/json", "1")
    bad = _case("/api/products", req={"headers": {"X-Evil": "a\r\nInjected: 1"}})
    assert observe(bad, cfg).error_class == "invalid_case"


def test_job_cases(legacy):
    cfg = RunnerConfig(base_url=legacy.base_url)
    c = _case(kind="job", request={"job": "nightly", "args": ["--all"]})
    res = observe(c, cfg)
    assert res.observed.status == 0 and res.observed.body["processed"] == 3
    assert observe(c.model_copy(update={"request": c.request.model_copy(update={"job": "other"})}),
                   cfg).observed.status == 2
    none = cfg.model_copy(update={"job_endpoint": None})
    assert observe(c, none).error_class == "config"
    wrong = cfg.model_copy(update={"job_endpoint": "/api/products"})
    assert observe(c, wrong).error_class == "protocol"


def test_grpc_and_message_not_implemented():
    cfg = RunnerConfig(base_url="http://127.0.0.1:9")
    g = _case(kind="grpc", request={"service": "Shop", "rpc": "Get"})
    m = _case(kind="message", request={"topic": "orders", "payload": {}})
    for c in (g, m):
        r = run_case(c, cfg)
        assert r.outcome == "not_implemented" and "not supported" in r.error


def test_module_entry_point(legacy, tmp_path):
    d = tmp_path / "cases" / "UNPLANNED"
    d.mkdir(parents=True)
    obs = observe(_case("/api/products"), RunnerConfig(base_url=legacy.base_url)).observed
    c = _case("/api/products", expect=Expectation(status=200, headers=obs.headers, body=obs.body))
    (d / "CT-0001.json").write_text(c.model_dump_json())
    out = tmp_path / "out.json"
    assert runner_main(["--base-url", legacy.base_url, "--cases", str(tmp_path / "cases"), "--out", str(out)]) == 0
    assert [CaseResult.model_validate(r).outcome for r in json.loads(out.read_text())] == ["pass"]
    assert runner_main(["--base-url", "ftp://x", "--cases", str(tmp_path / "cases")]) == 2


# ------------------------------------------------------------------ stub server
def test_stub_server(tmp_path):
    stubs = tmp_path / "stubs"
    for name, status in (("payment_ok", 200), ("payment_declined", 402)):
        s = StubDef(node_id="EXT-0002", name=name, request={"method": "POST", "path": "/v1/charges/*"},
                    response={"status": status, "body": {"result": name}})
        p = stub_path(stubs, s.node_id, s.name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(s.model_dump_json())
    assert [s.ref for s in load_stubs(stubs)] == ["EXT-0002/payment_declined", "EXT-0002/payment_ok"]
    with pytest.raises(ValueError):
        stub_path(stubs, "EXT-0002", "../x")
    with StubServer(stubs) as srv:
        cfg = RunnerConfig(base_url=srv.base_url)
        charge = _case("/EXT-0002/v1/charges/7", method="POST", req={"body": {"amount": 5}})
        assert observe(charge, cfg).observed.status == 402                        # first by name
        act = _case("/__stub/activate", method="POST", req={"body": {"stubs": ["EXT-0002/payment_ok"]}})
        assert observe(act, cfg).observed.status == 200
        r = observe(charge, cfg).observed
        assert (r.status, r.body) == (200, {"result": "payment_ok"})
        assert observe(_case("/EXT-0009/v1/x"), cfg).observed.status == 404
        bad = _case("/__stub/activate", method="POST", req={"body": {"stubs": ["EXT-0002/nope"]}})
        assert observe(bad, cfg).observed.status == 400
        calls = observe(_case("/__stub/calls"), cfg).observed.body
        assert [c["stub"] for c in calls] == ["EXT-0002/payment_declined", "EXT-0002/payment_ok", None]
        assert calls[0]["body"] == {"amount": 5}
        observe(_case("/__stub/reset", method="POST"), cfg)
        assert observe(_case("/__stub/calls"), cfg).observed.body == []


def test_injected_headers_are_overridden_or_removed_by_a_case():
    from parity_migrate.migrate.harness_runner.wire import merge_headers
    inj = {"X-API-Token": "secret", "X-Static": "1"}
    assert merge_headers(inj, None) == inj
    assert merge_headers(inj, {"x-api-token": "wrong"}) == {"X-Static": "1", "x-api-token": "wrong"}   # case wins, any case
    assert merge_headers(inj, {"X-API-Token": ""}) == {"X-Static": "1"}                                # empty removes it
    assert merge_headers(None, {"Accept": "text/plain", "X-Nope": ""}) == {"Accept": "text/plain"}
