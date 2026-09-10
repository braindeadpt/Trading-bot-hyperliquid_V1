"""Tests for scripts/research/overnight_nightly.py — the queue-driven cron wrapper.

Pins the wrapper's contract without running backtests:
- pick_ready(): first READY section, parseable family + window set + cells,
  tolerating both "**Harness:**" and "**Harness**:" markdown forms.
- count_windows(): uses the same splitter as the sweep (runtime K=4 guard).
- main() paths: nothing-READY exits 0 with a status file; a READY selection
  with K < 4 is REFUSED (exit 0, honest reason); dry-run writes the status
  without running; heartbeat refreshes only.
- run_session(): captures rc/verdicts/duration from --summary output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.research import overnight_nightly as nw  # noqa: E402

READY_QUEUE = """# queue

## Test family (READY — wired)
- **Harness:** `python scripts/research/overnight_runner.py --family vwap_thresholds --cells 0,1 --symbols BTC,ETH`
- **Window set:** 2026-05-18..2026-09-08, split 30d -> 4 non-overlapping windows.
- **Grid:** 2 cells x 4 = 8 runs.

## Blocked family (BLOCKED — K=4 floor)
- **Harness:** `python scripts/research/overnight_runner.py --family flush_fade`
- **Window set:** 2026-08-07..2026-09-09, split 30d -> 2 windows.
"""

K3_QUEUE = """# queue

## Short family (READY — wired)
- **Harness:** `python scripts/research/overnight_runner.py --family vwap_thresholds`
- **Window set:** 2026-08-07..2026-09-09, split 30d -> windows.
"""

DVOL_QUEUE = """# queue

## Night 3 (READY — coverage-gated)
- **Harness:** `python scripts/research/overnight_runner.py --family iv_thresholds --start dvol --end dvol --symbols BTC,ETH,SOL,HYPE --cells 0,1,2`
- **Window set:** DVOL coverage (starts 2026-06-14), 30d split — K=4 needs ~115d.
"""

NO_READY_QUEUE = """# queue

## Night 1 — done (CLOSED)
- **Harness:** `python scripts/research/overnight_runner.py --family flush_fade`

## Night 3 — blocked (BLOCKED — reopen ~ 2026-10-08)
- **Harness:** wrap iv script as family `iv_thresholds`.
"""


@pytest.fixture
def status_file(tmp_path):
    """Redirect the status write to tmp — the real file stays untouched."""
    p = tmp_path / "NIGHTLY_STATUS.json"
    return p


def _run_main(monkeypatch, tmp_path, status_file, queue_text, argv):
    q = tmp_path / "QUEUE.md"
    q.write_text(queue_text, encoding="utf-8")
    monkeypatch.setattr(nw, "STATUS_PATH", status_file)
    monkeypatch.setattr(nw, "_write_status",
                        lambda payload: status_file.write_text(
                            json.dumps(payload, default=str), encoding="utf-8"))
    monkeypatch.setattr(sys, "argv", ["overnight_nightly.py",
                                      "--queue", str(q)] + argv)
    return nw.main()


class TestPickReady:
    def test_first_ready_section_parsed(self, tmp_path):
        q = tmp_path / "Q.md"
        q.write_text(READY_QUEUE, encoding="utf-8")
        sel = nw.pick_ready(q)
        assert sel is not None
        assert sel["family"] == "vwap_thresholds"
        assert sel["window"] == {"start": "2026-05-18", "end": "2026-09-08",
                                 "split_days": 30}
        assert sel["cells"] == "0,1"
        assert sel["symbols"] == "BTC,ETH"

    def test_colon_inside_bold_label_accepted(self, tmp_path):
        # "**Harness**:" (colon outside bold) also parses — both are correct.
        q = tmp_path / "Q.md"
        q.write_text("""# q
## F (READY — wired)
- **Harness**: `python scripts/research/overnight_runner.py --family flush_fade`
- **Window set**: 2026-05-18..2026-09-08, split 30d -> 4 windows.
""", encoding="utf-8")
        sel = nw.pick_ready(q)
        assert sel and sel["family"] == "flush_fade"

    def test_no_ready_returns_none(self, tmp_path):
        q = tmp_path / "Q.md"
        q.write_text(NO_READY_QUEUE, encoding="utf-8")
        assert nw.pick_ready(q) is None

    def test_missing_window_spec_is_not_ready(self, tmp_path):
        q = tmp_path / "Q.md"
        q.write_text("""# q
## F (READY — wired)
- **Harness:** `python scripts/research/overnight_runner.py --family vwap_thresholds`
""", encoding="utf-8")
        assert nw.pick_ready(q) is None

    def test_dvol_span_entry_parsed_as_coverage_gated(self, tmp_path):
        q = tmp_path / "QUEUE.md"
        q.write_text(DVOL_QUEUE, encoding="utf-8")
        ready = nw.pick_ready(q)
        assert ready["family"] == "iv_thresholds"
        assert ready["window"] == {"span": "dvol", "split_days": 30}
        assert ready["cells"] == "0,1,2"
        assert ready["symbols"] == "BTC,ETH,SOL,HYPE"

    def test_real_queue_q6_is_first_ready(self):
        # Live fact check: Q6 (hype_vwap_refine) is the FIRST READY — it is
        # fully runnable now (6 windows, research DB + live DB on disk) and
        # the nightly wrapper runs only the first READY section. Night 3
        # (iv_thresholds) stays READY coverage-gated behind it.
        ready = nw.pick_ready(nw.QUEUE_PATH)
        assert ready is not None
        assert ready["family"] == "hype_vwap_refine"
        assert ready["window"] == {"start": "2026-03-13",
                                    "end": "2026-09-08", "split_days": 30}
        assert ready["symbols"] == "HYPE,BTC,ETH"
        assert ready["cells"] == "0,1,2"
        assert nw.count_windows(ready["window"]) == 6
        assert nw.count_windows(ready["window"]) >= nw.K_FLOOR


    def test_coverage_gated_ready_reports_no_precomputed_k(self, tmp_path):
        """A dvol-gated entry defers the K=4 floor to the runner — the
        status file marks it coverage_gated instead of a fixed K."""
        q = tmp_path / "QUEUE.md"
        q.write_text(DVOL_QUEUE, encoding="utf-8")
        st = nw.build_nightly_status(queue_path=q)
        nr = st["next_ready"]
        assert nr["coverage_gated"] is True
        assert nr["k_floor_ok"] is None
        assert nr["windows"] is None


class TestCountWindows:
    def test_uses_the_sweep_splitter(self):
        assert nw.count_windows({"start": "2026-05-18", "end": "2026-09-08",
                                 "split_days": 30}) == 4
        assert nw.count_windows({"start": "2026-08-07", "end": "2026-09-09",
                                 "split_days": 30}) == 2

    def test_k_floor_constant_is_four(self):
        assert nw.K_FLOOR == 4


class TestMainPaths:
    def test_nothing_ready_exits_zero_and_writes_status(self, tmp_path,
                                                        status_file,
                                                        monkeypatch):
        rc = _run_main(monkeypatch, tmp_path, status_file, NO_READY_QUEUE, [])
        assert rc == 0
        d = json.loads(status_file.read_text(encoding="utf-8"))
        assert d["next_ready"] is None
        assert "nothing READY" in d["reason"]

    def test_ready_below_k_floor_is_refused_not_run(self, tmp_path,
                                                    status_file, monkeypatch):
        ran = []
        monkeypatch.setattr(nw, "run_session",
                            lambda sel, sym: ran.append(sel) or {})
        rc = _run_main(monkeypatch, tmp_path, status_file, K3_QUEUE, [])
        assert rc == 0                      # refusal is a normal outcome
        assert ran == []                    # nothing ran
        d = json.loads(status_file.read_text(encoding="utf-8"))
        assert "K=4 floor" in d["reason"]

    def test_dry_run_reports_without_running(self, tmp_path, status_file,
                                             monkeypatch):
        ran = []
        monkeypatch.setattr(nw, "run_session",
                            lambda sel, sym: ran.append(sel) or {})
        rc = _run_main(monkeypatch, tmp_path, status_file, READY_QUEUE,
                       ["--dry-run"])
        assert rc == 0
        assert ran == []
        d = json.loads(status_file.read_text(encoding="utf-8"))
        assert d["next_ready"]["family"] == "vwap_thresholds"
        assert d["next_ready"]["k_floor_ok"] is True
        assert d["reason"].startswith("dry-run:")

    def test_heartbeat_only_refreshes_status(self, tmp_path, status_file,
                                             monkeypatch):
        rc = _run_main(monkeypatch, tmp_path, status_file, READY_QUEUE,
                       ["--heartbeat"])
        assert rc == 0
        d = json.loads(status_file.read_text(encoding="utf-8"))
        assert d["reason"] == "heartbeat"


class TestRunSession:
    def test_summary_output_is_parsed_into_verdicts(self, tmp_path,
                                                    monkeypatch):
        # Stub subprocess.run instead of launching a real sweep.
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = ("overnight: family=x windows=4K\n"
                      "  v1: net=1.00 n=40 PF=1.20 vs base -5.00 -> KEEP\n"
                      "  v2: net=-1.00 n=40 PF=0.80 vs base -5.00 -> DISCARD\n")
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return FakeProc()

        monkeypatch.setattr(nw.subprocess, "run", fake_run)
        sel = {"family": "vwap_thresholds",
               "window": {"start": "2026-05-18", "end": "2026-09-08",
                          "split_days": 30}}
        res = nw.run_session(sel, "BTC,ETH")
        assert res["verdicts"] == ["KEEP", "DISCARD"]
        assert res["returncode"] == 0
        assert "--summary" in captured["argv"]
        assert "--cells" not in captured["argv"]   # cells None → flag omitted

    def test_run_session_passes_dvol_span_through(self, tmp_path, monkeypatch):
        """A coverage-gated selection forwards --start dvol --end dvol to
        the runner — the span is resolved at run time, not by the wrapper."""
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = "overnight: family=iv_thresholds windows=4K\n"
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return FakeProc()

        monkeypatch.setattr(nw.subprocess, "run", fake_run)
        sel = {"family": "iv_thresholds",
               "window": {"span": "dvol", "split_days": 30}}
        res = nw.run_session(sel, "BTC,ETH,SOL,HYPE")
        assert res["returncode"] == 0
        joined = " ".join(captured["argv"])
        assert "--family iv_thresholds" in joined
        assert "--start dvol" in joined and "--end dvol" in joined
        assert "--symbols BTC,ETH,SOL,HYPE" in joined

    def test_timeout_is_reported_not_raised(self, tmp_path, monkeypatch):
        import subprocess as sp

        def fake_run(argv, **kw):
            raise sp.TimeoutExpired(cmd=argv, timeout=999)

        monkeypatch.setattr(nw.subprocess, "run", fake_run)
        sel = {"family": "x", "window": {"start": "2026-05-18",
                                         "end": "2026-09-08", "split_days": 30}}
        res = nw.run_session(sel, "BTC,ETH")
        assert res["timed_out"] is True
        assert res["returncode"] == -1


class TestRecentSessions:
    def test_reads_artifact_digest_newest_first(self, tmp_path):
        art = tmp_path / "20260909_173039_vwap_thresholds.json"
        art.write_text(json.dumps({
            "family": "vwap_thresholds",
            "windows": [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]],
            "results": [{"verdict": "DISCARD"}, {"verdict": "DISCARD"}],
        }), encoding="utf-8")
        (tmp_path / "NIGHTLY_STATUS.json").write_text("{}", encoding="utf-8")
        out = nw._recent_sessions.__wrapped__ if hasattr(
            nw._recent_sessions, "__wrapped__") else nw._recent_sessions
        # _recent_sessions globs STATUS_PATH.parent — point it at tmp_path.
        orig = nw.STATUS_PATH
        nw.STATUS_PATH = tmp_path / "NIGHTLY_STATUS.json"
        try:
            sessions = nw._recent_sessions(limit=5)
        finally:
            nw.STATUS_PATH = orig
        assert sessions[0]["family"] == "vwap_thresholds"
        assert sessions[0]["verdicts"] == ["DISCARD", "DISCARD"]
        assert sessions[0]["windows"] == 4
        assert all("NIGHTLY_STATUS" not in s["artifact"] for s in sessions)
