"""Latency comparison and differential fuzzing (migrate/differential.py): pure helpers, deterministic mutations,
latency recorded by equivalence runs, fuzz divergences (new 500 vs legacy 400), legacy noise ignored, the report,
the CLI and the advisory cut-over warnings."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from parity_migrate.cli import main
from parity_migrate.migrate import cutover, differential as df, equivalence as eq
from parity_migrate.migrate.casestore import save_case
from parity_migrate.migrate.ids import allocate
from parity_migrate.migrate.models import CharacterizationCase, HttpRequest
from parity_migrate.migrate.paths import Phase2Layout

from p2_m5_helpers import m5_project, record


class Calc:
    """GET /api/calc?qty=N -> {total}. mode legacy: negative qty -> 400. new: negative qty -> 500 (the seeded
    divergence). `slow_s` delays every answer; legacy adds a noisy `ts` field."""

    def __init__(self, mode, slow_s=0.0):
        self.mode, self.slow_s = mode, slow_s
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                time.sleep(svc.slow_s)
                try:
                    n = int(q.get("qty", ["1"])[0])
                    status, body = (200, {"total": n * 5})
                except ValueError:
                    status, body = 400, {"error": "bad qty"}
                if status == 200 and n < 0:
                    status, body = (400, {"error": "negative"}) if svc.mode == "legacy" else (500, {"error": "boom"})
                if svc.mode == "legacy" and status == 200:
                    body["ts"] = time.time_ns()
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *e):
        self._httpd.shutdown()
        self._httpd.server_close()


def _calc_case(p, s1, legacy_url):
    cid = allocate(p, "CT", key="calc")
    c = CharacterizationCase(id=cid, slice=s1, covers=["EP-0001"], kind="http", normalize=["$.ts"],
                             request=HttpRequest(method="GET", path="/api/calc", query={"qty": "2"}))
    save_case(p, c)
    return record(p, c, legacy_url)


# ------------------------------------------------------------------ pure helpers
def test_percentile_and_latency_summary():
    assert df.percentile([], 50) is None and df.percentile([3, 1, 2], 50) == 2 and df.percentile([1, 2, 3, 4], 95) == 4
    assert df.latency_summary([("CT-0001", None, 1.0)]) is None
    rows = [("CT-0001", 0.10, 0.11), ("CT-0002", 0.10, 0.50), ("CT-0003", 0.001, 0.004), ("CT-0004", 0.2, None)]
    s = df.latency_summary(rows)
    assert s.cases == 3 and [x.case_id for x in s.slower] == ["CT-0002"]       # CT-0003 is x4 but only 3 ms slower
    assert s.slower[0].ratio == 5.0 and s.ratio_p50 is not None


def test_mutations_are_deterministic_capped_and_never_touch_path_or_method():
    r = HttpRequest(method="POST", path="/api/x", query={"a": "1", "b": "2"}, body={"qty": 2, "name": "pen"})
    a, b = df.mutations(r, max_variants=8), df.mutations(r, max_variants=8)
    assert [x[0] for x in a] == [x[0] for x in b] and len(a) == 8
    kinds = {x[0].split(".")[0].split("=")[0].split("{")[0] for x in a}
    assert {"query", "body"} <= kinds                                           # families are interleaved
    for _label, m in df.mutations(r, max_variants=64):
        assert m.method == "POST" and m.path == "/api/x"
    assert r.body == {"qty": 2, "name": "pen"} and r.query == {"a": "1", "b": "2"}   # input untouched
    bare = df.mutations(HttpRequest(method="GET", path="/p"))
    assert [m[0] for m in bare] == ["query.+unknown_param"]
    post = [m[0] for m in df.mutations(HttpRequest(method="POST", path="/p", body={}), max_variants=16)]
    assert "body.shape=array" in post and "body.shape=object_with_field" in post and "query.+unknown_param" in post


# ------------------------------------------------------------------ integration
@pytest.fixture
def world(tmp_path, tmp_path_factory):
    p, s1, s2 = m5_project(tmp_path, tmp_path_factory)
    with Calc("legacy") as legacy, Calc("new") as new:
        case = _calc_case(p, s1, legacy.base_url)
        yield p, s1, legacy, new, case


def test_fuzz_finds_new_500_and_ignores_legacy_noise(world):
    p, s1, legacy, new, case = world
    rep = df.run_fuzz(p, s1, legacy.base_url, new.base_url, max_variants=7)
    assert rep.cases_considered == 1 and rep.variants_run == 7 and rep.variants_not_answered == 0
    neg = [d for d in rep.divergences if d.mutation == "query.qty=negative"]
    assert neg and neg[0].severity == "high" and neg[0].field == "status" and (neg[0].legacy, neg[0].new) == (400, 500)
    assert not any(d.field.endswith("ts") for d in rep.divergences)              # noise masked, not reported
    assert df.load_fuzz_report(p, s1).high and "Differential fuzzing" in Phase2Layout(p).equivalence_report_md.read_text()


def test_fuzz_identical_services_report_nothing(tmp_path, tmp_path_factory):
    p, s1, _s2 = m5_project(tmp_path, tmp_path_factory)
    with Calc("legacy") as a, Calc("legacy") as b:
        _calc_case(p, s1, a.base_url)
        rep = df.run_fuzz(p, s1, a.base_url, b.base_url, max_variants=7)
    assert rep.variants_run == 7 and rep.divergences == []


def test_equivalence_records_latency_and_cutover_warns(tmp_path, tmp_path_factory):
    p, s1, _s2 = m5_project(tmp_path, tmp_path_factory)
    with Calc("legacy") as legacy, Calc("legacy", slow_s=0.25) as slow:
        _calc_case(p, s1, legacy.base_url)
        eq.run_equivalence(p, s1, legacy.base_url, slow.base_url)
    meta = eq.load_run_meta(p, s1)
    assert meta.cases[0].legacy_s is not None and meta.cases[0].new_s >= 0.25
    assert meta.latency["cases"] == 1 and [x["case_id"] for x in meta.latency["slower"]] == [meta.cases[0].case_id]
    assert "Latency (advisory)" in eq.render_report_md(p)
    chk = cutover._c_equivalence(p, eq._plan(p))
    assert any("slower" in w for w in chk.warnings) and not chk.ok            # blocked only by the missing s2 report


def test_cli_fuzz_exit_code_and_selector(world):
    p, s1, legacy, new, _case = world
    r = CliRunner().invoke(main, ["equivalence", "fuzz", str(p.root), "--slice", s1, "--legacy-url", legacy.base_url,
                                  "--new-url", new.base_url, "--max-variants", "7"])
    assert r.exit_code == 1 and "high" in r.output and "divergence" in r.output
    assert CliRunner().invoke(main, ["equivalence", "fuzz", str(p.root)]).exit_code != 0
