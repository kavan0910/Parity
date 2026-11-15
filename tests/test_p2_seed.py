"""Authenticated services: `request_headers` with `{secret:NAME}` and API seed replay after `harness up`
(found by the first live run: every recorded case was a 401 because nothing sent the synthetic token)."""
import json
import types

import pytest
import yaml

from parity_migrate.migrate import harness as H

from phase2_helpers import approved_project


@pytest.fixture
def proj(tmp_path, tmp_path_factory):
    p = approved_project(tmp_path, tmp_path_factory)
    H.import_source(p, from_original=True)
    return p


def _spec(proj, **svc_updates):
    spec = H.load_spec(proj, adopt=True)
    spec.legacy = spec.legacy.model_copy(update=svc_updates)
    return spec


def _secrets(proj, role, values):
    H._write_secrets(proj, role, values)


def test_request_headers_validate_names_and_values():
    base = dict(image="i:1", build={"command": ["true"]}, start=["x"], port=80)
    H.ServiceSpec(**base, request_headers={"X-API-Token": "{secret:API_TOKEN}"})
    for bad in ({"Bad Header": "v"}, {"X": "a\nb"}, {"X": "a" * 501}):
        with pytest.raises(ValueError):
            H.ServiceSpec(**base, request_headers=bad)


def test_headers_resolve_from_the_roles_secrets(proj):
    spec = _spec(proj, request_headers={"X-API-Token": "{secret:API_TOKEN}", "X-Static": "1"})
    assert spec.legacy.synthetic_secrets == ["API_TOKEN"]
    _secrets(proj, "legacy", {"API_TOKEN": "abc123"})
    _secrets(proj, "composite", {"API_TOKEN": "zzz"})
    assert H.request_headers_for(proj, "legacy", spec) == {"X-API-Token": "abc123", "X-Static": "1"}
    assert H.request_headers_for(proj, "composite", spec)["X-API-Token"] == "zzz"      # composite: legacy's headers


def test_new_service_inherits_request_headers_by_default(proj):
    from parity_migrate.migrate.paths import Phase2Layout
    lay = Phase2Layout(proj)
    lay.ensure(lay.target_dir)
    lay.target_yaml.write_text("kind: framework_move\nsource: {language: javascript, runtime: node@20}\n"
                               "target: {language: javascript, runtime: node@20, build: npm}\n")
    spec = _spec(proj, request_headers={"X-API-Token": "{secret:API_TOKEN}"})
    _secrets(proj, "new", {"API_TOKEN": "newtok"})
    assert H.request_headers_for(proj, "new", spec) == {"X-API-Token": "newtok"}


def test_unknown_or_missing_secret_is_a_clear_error(proj):
    spec = _spec(proj, request_headers={"X-API-Token": "{secret:NOPE}"})
    _secrets(proj, "legacy", {"API_TOKEN": "abc"})
    with pytest.raises(H.HarnessError, match="not in synthetic_secrets"):
        H.request_headers_for(proj, "legacy", spec)
    spec = _spec(proj, request_headers={"X-API-Token": "{secret:API_TOKEN}"})
    (proj.root / "harness" / "synthetic_secrets.json").unlink()
    with pytest.raises(H.HarnessError, match="is the legacy harness up"):
        H.request_headers_for(proj, "legacy", spec)


def _fake_results(statuses):
    def run(project, role, cases, **kw):
        run.calls.append((role, kw.get("base_url"), [c.request.path for c in cases]))
        return [types.SimpleNamespace(observed=None if s is None else types.SimpleNamespace(status=s), error="boom")
                for s in statuses[:len(cases)]]
    run.calls = []
    return run


def test_load_seed_cases_from_the_fixture_file(proj):
    cases = H.load_seed_cases(proj)
    assert [c.request.method for c in cases] == ["POST", "POST", "POST"] and cases[0].id == "CT-9001"
    assert cases[0].request.body["sku"] == "BOOK-001" and cases[0].tags == ["seed"]


def test_replay_seeds_ok_and_failures(proj, monkeypatch):
    run = _fake_results([201, 201, 201])
    monkeypatch.setattr(H, "run_cases_in_network", run)
    out = H.replay_seeds(proj, "legacy")
    assert out == {"requests": 3, "ok": True, "targets": ["legacy"]} and run.calls[0][0] == "legacy"
    monkeypatch.setattr(H, "run_cases_in_network", _fake_results([201, 401, None]))
    with pytest.raises(H.HarnessError, match=r"2 of 3 seed request\(s\) failed.*request_headers"):
        H.replay_seeds(proj, "legacy")


def _writable(path):
    path.chmod(0o644)                              # legacy_src files are imported read-only
    return path


def test_seed_expect_status_overrides_the_default(proj, monkeypatch):
    seed = _writable(proj.root / "legacy_src" / "harness" / "seed" / "requests.json")
    d = json.loads(seed.read_text())
    d["requests"] = [{"method": "POST", "path": "/api/x", "expect_status": [409]}]
    seed.write_text(json.dumps(d))
    monkeypatch.setattr(H, "run_cases_in_network", _fake_results([409]))
    assert H.replay_seeds(proj, "legacy")["ok"]
    monkeypatch.setattr(H, "run_cases_in_network", _fake_results([201]))
    with pytest.raises(H.HarnessError):
        H.replay_seeds(proj, "legacy")


def test_bad_seed_files_are_rejected(proj):
    seed = _writable(proj.root / "legacy_src" / "harness" / "seed" / "requests.json")
    for text, msg in (("not json", "not valid"), (json.dumps({"requests": [{"path": "/x"}]}), "item #1 is invalid"),
                      (json.dumps({"requests": [{"method": "GET", "path": "x"}]}), "must start with /"),
                      (json.dumps({"requests": [{"method": "GET", "path": "/x"}] * 201}), "at most 200")):
        seed.write_text(text)
        with pytest.raises(H.HarnessError, match=msg):
            H.load_seed_cases(proj)


def test_composite_seeds_both_services_directly(proj, monkeypatch):
    st = H.load_state(proj)
    st["composite"] = {"up": {"network": "n", "services": {"legacy": {"port": 3000}, "new": {"port": 3001}}}}
    H.save_state(proj, st)
    run = _fake_results([201, 201, 201])
    monkeypatch.setattr(H, "run_cases_in_network", run)
    out = H.replay_seeds(proj, "composite")
    assert out["targets"] == ["legacy", "new"]
    assert [c[1] for c in run.calls] == ["http://legacy:3000", "http://new:3001"]


def test_failed_seed_on_up_tears_the_harness_down(proj, monkeypatch):
    calls = []
    monkeypatch.setattr(H, "replay_seeds", lambda *a, **k: (_ for _ in ()).throw(H.HarnessError("seed failed")))
    monkeypatch.setattr(H, "down", lambda project, role, **k: calls.append(role))
    from p2_exec_helpers import FakeDocker
    from parity_migrate.migrate import exec as p2exec
    fd = FakeDocker()
    monkeypatch.setattr(p2exec, "get_backend", lambda project=None: fd.backend(project))
    H.build(proj, "legacy")
    with pytest.raises(H.HarnessError, match="seed failed"):
        H.up(proj, "legacy")
    assert calls == ["legacy"]
