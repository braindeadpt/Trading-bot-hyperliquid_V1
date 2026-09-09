"""Tests for scripts/overnight_runner.py — the gated overnight sweep.

Pins the research_program.md verdict rules: KEEP requires multi-window
majority + aggregate n>=30 + PF>1 + no catastrophic window (+ the exact
paired sign-flip noise gate when per-trade PnL is available); INCONCLUSIVE
parks on structural insufficiency (<2 valid windows) or n<30; DISCARD on
majority/PF/catastrophic failure; BLOCKED when nothing survived to compare.
Also pins the ledger contract: morning report on top, append-only history,
one block per experiment with the audit line.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from scripts import overnight_runner as mod  # noqa: E402


def cell(pnl: float, n: int, gw: float, gl: float) -> dict:
    return {"total_pnl_usd": pnl, "n_trades": n,
            "gross_win_usd": gw, "gross_loss_usd": gl}


# --- paired per-window noise model (exact sign-flip) -----------------------

class TestPairedNoiseModel:
    """The paired per-window randomization behind the KEEP gate.

    The resampling unit is the window PAIR (variant−baseline delta of the
    same window, from both trade lists together) so window-level regime
    correlation is preserved. H0 (no edge) = every sign pattern of the K
    deltas is equally likely; p = P_H0(sum >= observed); pass iff p <= 0.10.
    """

    def test_paired_deltas_are_window_units_not_trades(self):
        base = [{"trade_pnls": [10.0, -5.0]}, {"trade_pnls": [3.0]},
                {"trade_pnls": [7.0, -2.0, 1.0]}]
        var = [{"trade_pnls": [14.0, -5.0]}, {"trade_pnls": [5.0]},
               {"trade_pnls": [9.0, -2.0, 1.0]}]
        assert mod.paired_window_deltas(base, var) == [4.0, 2.0, 2.0]

    def test_paired_deltas_missing_data_vs_double_zero(self):
        base = [{"trade_pnls": [5.0]}, {"trade_pnls": []}, {"n_trades": 3}]
        var = [{"trade_pnls": [9.0]}, {"trade_pnls": []},
               {"trade_pnls": [1.0, 1.0, 1.0]}]
        # Window 3 baseline has NO trade_pnls key → legacy artifact → None.
        assert mod.paired_window_deltas(base, var) is None
        base[2] = {"trade_pnls": [2.0, 2.0, 2.0]}
        # Now window 2 is a genuine double-zero → skipped as no-evidence;
        # window 3's variant (3.0) LOSES to its baseline (6.0) → −3.0, a
        # legitimate negative pair the gate must see, not hide.
        assert mod.paired_window_deltas(base, var) == [4.0, -3.0]

    def test_exact_k4_all_positive_passes_at_p_0625(self):
        base = [{"trade_pnls": [5.0]}, {"trade_pnls": [4.0]},
                {"trade_pnls": [3.0]}, {"trade_pnls": [2.0]}]
        var = [{"trade_pnls": [6.0]}, {"trade_pnls": [6.0]},
               {"trade_pnls": [6.0]}, {"trade_pnls": [6.0]}]
        ng = mod.paired_bootstrap_noise_gate(base, var)
        assert ng["evaluated"] is True and ng["pass"] is True
        assert ng["p_value"] == 0.0625          # 2^-4 — the exact floor at K=4
        assert ng["method"] == "paired_window_signflip_exact_k4"
        assert ng["alpha"] == 0.10

    def test_exact_k3_all_positive_cannot_clear_alpha(self):
        # The principled floor: with every window favoring the variant,
        # p = 2^-K, so K=3 (0.125) can never clear alpha=0.10. Three
        # windows cannot KEEP on the noise gate; four can.
        base = [{"trade_pnls": [1.0]}] * 3
        var = [{"trade_pnls": [2.0]}] * 3
        ng = mod.paired_bootstrap_noise_gate(base, var)
        assert ng["pass"] is False and ng["p_value"] == 0.125

    def test_one_losing_window_doubles_p_and_fails(self):
        # Same everywhere-edge, but window 4's variant LOSES to its baseline:
        # the null can flip that window into a bigger sum → p doubles > alpha.
        base = [{"trade_pnls": [1.0]}] * 4
        var = [{"trade_pnls": [2.0]}, {"trade_pnls": [2.0]},
               {"trade_pnls": [2.0]}, {"trade_pnls": [0.5]}]
        ng = mod.paired_bootstrap_noise_gate(base, var)
        assert ng["pass"] is False and ng["p_value"] == 0.125

    def test_exact_path_is_fully_deterministic_and_alpha_honored(self):
        base = [{"trade_pnls": [5.0]}, {"trade_pnls": [4.0]},
                {"trade_pnls": [3.0]}, {"trade_pnls": [2.0]}]
        var = [{"trade_pnls": [6.0]}, {"trade_pnls": [6.0]},
               {"trade_pnls": [6.0]}, {"trade_pnls": [6.0]}]
        a = mod.paired_bootstrap_noise_gate(base, var)
        b = mod.paired_bootstrap_noise_gate(base, var, seed=1)  # seed ignored: exact
        assert a == b
        strict = mod.paired_bootstrap_noise_gate(base, var, alpha=0.05)
        assert strict["pass"] is False and strict["p_value"] == 0.0625

    def test_sampled_path_beyond_k12_is_seeded_and_reproducible(self):
        base = [{"trade_pnls": [1.0]}] * 13
        var = [{"trade_pnls": [2.0]}] * 13
        a = mod.paired_bootstrap_noise_gate(base, var, resamples=500, seed=7)
        b = mod.paired_bootstrap_noise_gate(base, var, resamples=500, seed=7)
        c = mod.paired_bootstrap_noise_gate(base, var, resamples=500, seed=8)
        assert a == b  # same seed → identical verdict
        assert a["method"] == "paired_window_signflip_sampled_k13_n500"
        assert 0.0 <= a["p_value"] <= 1.0
        assert isinstance(c["p_value"], float)

    def test_gate_skips_without_trade_data(self):
        base = [cell(-50, 20, 30, 80)]
        var = [cell(5, 22, 60, 55)]
        ng = mod.paired_bootstrap_noise_gate(base, var)
        assert ng == {"evaluated": False, "pass": None,
                      "reason": "per-trade PnL unavailable — noise gate skipped"}

    def test_gate_skips_with_no_valid_pairs(self):
        base = [{"trade_pnls": []}]
        var = [{"trade_pnls": []}]
        ng = mod.paired_bootstrap_noise_gate(base, var)
        assert ng["evaluated"] is False and ng["pass"] is None

    def test_decide_demotes_keep_to_inconclusive_on_noise(self):
        base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50),
                cell(-30, 12, 20, 50), cell(-5, 10, 18, 23)]
        good = [cell(60, 22, 90, 30), cell(50, 18, 80, 30),
                cell(40, 14, 60, 20), cell(30, 11, 50, 20)]
        base_t = [{**c, "trade_pnls": [float(i % 7) - 3.0 for i in range(c["n_trades"])]}
                  for c in base]
        good_t = [{**c, "trade_pnls": [10.0 + float(i % 3) for i in range(c["n_trades"])]}
                  for c in good]
        v, reasons = mod.decide(base_t, good_t, noise_model=True)
        assert v == "KEEP" and any("paired sign-flip p=" in r for r in reasons)
        # Break one window pair (variant loses there) → p doubles past alpha.
        broken = list(good_t)
        broken[3] = {**broken[3], "trade_pnls": [-50.0] * broken[3]["n_trades"]}
        v2, reasons2 = mod.decide(base_t, broken, noise_model=True)
        assert v2 == "INCONCLUSIVE" and any("noise:" in r for r in reasons2)


# --- ledger reference header (queue format + verdict schema) ----------------

class TestLedgerReferenceHeader:
    """LEDGER_HEADER is the persistent reference re-written every session;
    it must keep documenting the queue states, the verdict schema, and the
    real worked example (stopout-bypass DISCARD)."""

    def test_header_documents_queue_states(self):
        for state in ("READY", "NEEDS-WIRING", "BLOCKED", "NEEDS-CODE", "CLOSED"):
            assert state in mod.LEDGER_HEADER, f"queue state {state!r} missing"
        assert "QUEUE.md" in mod.LEDGER_HEADER

    def test_header_documents_verdict_schema(self):
        for verdict in ("KEEP", "INCONCLUSIVE", "DISCARD", "BLOCKED"):
            assert f"**{verdict}**" in mod.LEDGER_HEADER
        # The non-obvious rules stay spelled out:
        assert "still loses money" in mod.LEDGER_HEADER  # PF gate rationale
        assert "no-evidence" in mod.LEDGER_HEADER        # double-zero rule
        assert "noise gate" in mod.LEDGER_HEADER         # paired condition
        assert "sign-flip" in mod.LEDGER_HEADER          # its null
        assert "alpha=0.10" in mod.LEDGER_HEADER         # its threshold

    def test_header_worked_example_is_the_real_stopout_bypass(self):
        assert "flush_fade/delay=0 stopout=OFF" in mod.LEDGER_HEADER
        assert "PF=0.306" in mod.LEDGER_HEADER
        assert "net=-325.67" in mod.LEDGER_HEADER
        assert "DISCARD" in mod.LEDGER_HEADER

    def test_header_points_editors_at_the_generator(self):
        assert "LEDGER_HEADER" in mod.LEDGER_HEADER


# --- verdict rules ---------------------------------------------------------

def test_keep_requires_all_gates():
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    good = [cell(5, 22, 60, 55), cell(8, 18, 70, 62)]
    v, reasons = mod.decide(base, good)
    assert v == "KEEP"
    assert any("SHADOW CANDIDATE" in r for r in reasons)


def test_inconclusive_parks_below_n30():
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    small = [cell(5, 8, 20, 15), cell(8, 10, 30, 22)]
    v, reasons = mod.decide(base, small)
    assert v == "INCONCLUSIVE"
    assert any("n<30" in r for r in reasons)


def test_inconclusive_on_single_valid_window():
    # One failed cell: even a unanimous improvement is not multi-window evidence.
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    one = [{"error": "boom"}, cell(8, 35, 130, 122)]
    v, reasons = mod.decide(base, one)
    assert v == "INCONCLUSIVE"
    assert any("valid window" in r for r in reasons)


def test_discard_without_majority():
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    mixed = [cell(8, 35, 60, 52), cell(-60, 35, 100, 160)]
    v, _ = mod.decide(base, mixed)
    assert v == "DISCARD"


def test_discard_on_catastrophic_window():
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    # W1 improved, W2 loses >2x the baseline's own worst window (-50).
    catastro = [cell(5, 30, 60, 55), cell(-120, 30, 140, 260)]
    v, reasons = mod.decide(base, catastro)
    assert v == "DISCARD"
    assert any("catastrophic" in r for r in reasons)


def test_blocked_when_comparison_impossible():
    # Baseline failed -> no deltas computable -> BLOCKED, not a fake DISCARD.
    base = [{"error": "x"}, {"error": "y"}]
    v, reasons = mod.decide(base, [cell(1, 40, 10, 5), cell(1, 40, 10, 5)])
    assert v == "BLOCKED"
    assert any("failed" in r for r in reasons)
    base_ok = [cell(-1, 40, 10, 11), cell(-1, 40, 10, 11)]
    v, reasons = mod.decide(base_ok, [{"error": "x"}, {"error": "y"}])
    assert v == "BLOCKED"
    assert any("failed" in r for r in reasons)


def test_deltas_invent_nothing_for_failed_cells():
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    var = [{"error": "boom"}, cell(8, 18, 70, 62)]
    d = mod.delta_per_window(base, var)
    assert d[0] is None
    assert d[1] == 18.0


def test_deltas_treat_double_zero_trade_windows_as_no_evidence():
    base = [cell(0, 0, 0, 0), cell(-10, 15, 40, 50)]
    var = [cell(0, 0, 0, 0), cell(8, 18, 70, 62)]
    d = mod.delta_per_window(base, var)
    assert d[0] is None  # no trades either side -> no evidence, not +0.0
    assert d[1] == 18.0


def test_verdict_excludes_no_evidence_windows_from_majority():
    base = [cell(-50, 20, 30, 80), cell(0, 0, 0, 0), cell(-10, 15, 40, 50)]
    good = [cell(5, 22, 60, 55), cell(0, 0, 0, 0), cell(8, 18, 70, 62)]
    v, reasons = mod.decide(base, good)
    assert v == "KEEP"
    assert any("no-evidence window" in r for r in reasons)
    assert any("improved 2/2" in r for r in reasons)


def test_aggregate_pf_from_gross_flows():
    ok = mod.aggregate([cell(10, 5, 60, 50), cell(-5, 3, 20, 25)])
    assert ok["pnl"] == 5.0
    assert ok["n"] == 8
    assert ok["pf"] == round(80 / 75, 3)
    no_loss = mod.aggregate([cell(10, 2, 10, 0)])
    assert no_loss["pf"] == float("inf")
    empty = mod.aggregate([{"error": "x"}])
    assert empty["n"] == 0 and empty["pf"] == 0.0


# --- ledger ----------------------------------------------------------------

@pytest.fixture()
def ledger_path(tmp_path, monkeypatch):
    p = tmp_path / "OVERNIGHT_RESEARCH_LOG.md"
    monkeypatch.setattr(mod, "LEDGER_PATH", p)
    return p


def _session(tag: str, verdict: str) -> tuple[dict, dict]:
    windows = [("2026-05-18", "2026-06-16"), ("2026-06-17", "2026-07-16")]
    base = [cell(-50, 20, 30, 80), cell(-10, 15, 40, 50)]
    good = [cell(5, 22, 60, 55), cell(8, 18, 70, 62)]
    result = {
        "tag": tag, "windows": windows,
        "baseline_cells": base, "variant_cells": good,
        "deltas": mod.delta_per_window(base, good),
        "aggregate_baseline": mod.aggregate(base),
        "aggregate_variant": mod.aggregate(good),
        "verdict": verdict, "reasons": ["test reason"],
    }
    session = {
        "family": "flush_fade",
        "span": {"start": "2026-05-18", "end": "2026-08-07", "split_days": 30},
        "symbols": ["BTC", "ETH"], "windows": windows,
        "baseline_tag": "delay=0 stopout=ON",
        "results": [result],
    }
    return session, result


def test_ledger_morning_report_and_block(ledger_path):
    session, _ = _session("delay=0 stopout=OFF", "KEEP")
    artifact = mod.ARTIFACT_DIR / "fake_session.json"  # under ROOT: relative_to works
    mod.write_ledger(session, artifact)
    text = ledger_path.read_text(encoding="utf-8")
    assert text.startswith("# Overnight research ledger")
    assert "## Morning report" in text
    assert "KEEP → shadow candidate" in text
    assert "### " in text and "— flush_fade/delay=0 stopout=OFF — KEEP" in text
    assert "audit line" in text and "advisory" in text
    assert "per-window delta" in text


def test_ledger_is_append_only_across_sessions(ledger_path):
    artifact = mod.ARTIFACT_DIR / "fake_session.json"
    mod.write_ledger(_session("delay=0 stopout=OFF", "KEEP")[0], artifact)
    first = ledger_path.read_text(encoding="utf-8")
    mod.write_ledger(_session("delay=5 stopout=OFF", "DISCARD")[0], artifact)
    second = ledger_path.read_text(encoding="utf-8")
    # Old experiment block survives; only one (newest) morning report remains.
    assert "stopout=OFF — KEEP" in second
    assert "stopout=OFF — DISCARD" in second
    assert second.count("## Morning report") == 1
    # Count real blocks only — the reference header's worked example also
    # contains '### ' lines inside its code fence.
    body = second.split("\n---\n", 1)[1]
    assert body.count("### ") == 2


# --- cell selection (budget-capped sessions) -------------------------------

def test_experiment_block_shows_baseline_tag():
    """The baseline line names the baseline cell — never empty parens."""
    session = {"family": "flush_fade",
               "span": {"start": "2026-08-09", "end": "2026-09-09", "split_days": 10},
               "windows": [("2026-08-09", "2026-08-18")],
               "baseline_tag": "delay=0 stopout=ON"}
    base = [cell(-50.0, 20, 30, 80)]
    var = [cell(-30.0, 20, 35, 65)]
    result = {"tag": "delay=0 stopout=OFF", "windows": session["windows"],
              "baseline_cells": base, "variant_cells": var,
              "deltas": [20.0],
              "aggregate_baseline": mod.aggregate(base),
              "aggregate_variant": mod.aggregate(var),
              "verdict": "DISCARD", "reasons": ["test"]}
    # Session-level fallback (legacy artifacts predate per-result baseline_tag):
    blk = mod.experiment_block(result, session)
    assert "baseline (delay=0 stopout=ON):" in blk
    # Per-result tag wins when present (current runner output):
    blk2 = mod.experiment_block(dict(result, baseline_tag="explicit"), session)
    assert "baseline (explicit):" in blk2


def test_select_cells_defaults_to_all():
    assert mod.select_cells(6, None) == [0, 1, 2, 3, 4, 5]
    assert mod.select_cells(6, "") == [0, 1, 2, 3, 4, 5]


def test_select_cells_baseline_always_included_and_sorted():
    assert mod.select_cells(6, "3,1") == [0, 1, 3]
    assert mod.select_cells(6, "0,4") == [0, 4]


def test_select_cells_rejects_out_of_range_and_garbage():
    with pytest.raises(ValueError):
        mod.select_cells(6, "6")
    with pytest.raises(ValueError):
        mod.select_cells(6, "-1")
    with pytest.raises(ValueError):
        mod.select_cells(6, "a,b")


# --- vwap_thresholds family (Night 2) ---------------------------------------

class TestVwapThresholdsFamily:
    """The per-symbol z_threshold sweep preregistered in QUEUE.md."""

    def test_resolve_z_precedence(self):
        # Explicit symbol wins over '*', which wins over the production base.
        ov = {"HYPE": 3.0}
        assert mod.resolve_z(ov, "HYPE", 2.5) == 3.0
        assert mod.resolve_z(ov, "BTC", 2.5) == 2.5
        assert mod.resolve_z({"*": 3.0}, "BTC", 2.5) == 3.0
        assert mod.resolve_z({"HYPE": 3.0, "*": 2.8}, "HYPE", 2.5) == 3.0
        assert mod.resolve_z({"*": 2.8}, "HYPE", 2.5) == 2.8
        assert mod.resolve_z({}, "BTC", 2.5) == 2.5

    def test_grid_is_preregistered_three_cells(self):
        # QUEUE.md Night 2: baseline (production everywhere) vs HYPE-only 3.0σ
        # vs everywhere 3.0σ. The baseline must be cell 0 and untouched.
        assert len(mod.VWAP_THRESHOLDS_GRID) == 3
        assert mod.VWAP_THRESHOLDS_GRID[0] == {}
        assert mod.VWAP_THRESHOLDS_GRID[1] == {"HYPE": 3.0}
        assert mod.VWAP_THRESHOLDS_GRID[2] == {"*": 3.0}

    def test_family_registered(self):
        assert "vwap_thresholds" in mod.FAMILIES
        assert "flush_fade" in mod.FAMILIES


def test_sweep_dispatches_dict_params_to_family(tmp_path, monkeypatch):
    """sweep() must pass the real vwap_thresholds grid dicts straight through
    to run_one — params is a z_overrides dict, not a tuple and not an index."""
    seen: list = []

    def fake_run_one(start, end, symbols, params):
        seen.append((start, end, tuple(symbols), dict(params)))
        if params == {"HYPE": 3.0}:
            c = cell(40.0, 20, 60.0, 20.0)          # variant improves every window
            c["trade_pnls"] = [2.0] * 20
        elif params == {}:                           # baseline
            c = cell(-20.0, 20, 10.0, 30.0)
            c["trade_pnls"] = [-1.0] * 20
        else:                                        # "*": 3.0 loses everywhere
            c = cell(-40.0, 20, 20.0, 60.0)
            c["trade_pnls"] = [-2.0] * 20
        return c

    tags = ["z=2.5 (baseline)", "z=2.5+HYPE:3.0", "z=3.0 all"]
    # Swap the family factory, keep sweep()'s own grid branch: this proves the
    # dict params move from VWAP_THRESHOLDS_GRID to run_one untouched.
    monkeypatch.setitem(mod.FAMILIES, "vwap_thresholds",
                        lambda: (tags, fake_run_one, None))

    session = mod.sweep("vwap_thresholds", "2026-05-18", "2026-09-08",
                        ["BTC", "HYPE"], split_days=30)

    assert session["family"] == "vwap_thresholds"
    assert len(session["windows"]) == 4
    # 3 cells × 4 windows = 12 run_one calls, params passed through verbatim.
    assert len(seen) == 12
    baseline_calls = [s for s in seen if s[3] == {}]
    hype_calls = [s for s in seen if s[3] == {"HYPE": 3.0}]
    assert len(baseline_calls) == 4 and len(hype_calls) == 4
    assert all(s[2] == ("BTC", "HYPE") for s in seen)

    by_tag = {r["tag"]: r for r in session["results"]}
    improved = by_tag["z=2.5+HYPE:3.0"]
    assert improved["aggregate_variant"]["pnl"] == 160.0
    assert improved["noise_gate"]["evaluated"] is True
    # 4/4 windows improved → sign-flip p=2^-4=0.0625 ≤ 0.10.
    assert improved["verdict"] == "KEEP"
    worse = by_tag["z=3.0 all"]
    assert worse["verdict"] == "DISCARD"


def test_experiment_block_carries_preregistered_vwap_hypotheses():
    """Night 2 ledger blocks must state the hypothesis fixed a priori,
    not a post-hoc rationalisation."""
    windows = [["2026-05-18", "2026-06-16"], ["2026-06-17", "2026-07-16"],
               ["2026-07-17", "2026-08-15"], ["2026-08-16", "2026-09-08"]]

    def result_for(tag: str) -> dict:
        return {
            "tag": tag, "verdict": "INCONCLUSIVE",
            "windows": windows, "deltas": [1.0, 1.0, 1.0, None],
            "aggregate_baseline": {"pnl": -100.0, "n": 40, "pf": 0.8},
            "aggregate_variant": {"pnl": -96.0, "n": 38, "pf": 0.82},
            "reasons": ["n=38 (gate >=30)"],
            "baseline_tag": "z=2.5 (baseline)",
        }

    session = {"family": "vwap_thresholds", "baseline_tag": "z=2.5 (baseline)",
               "span": {"start": "2026-05-18", "end": "2026-09-08",
                        "split_days": 30}}

    hype = mod.experiment_block(result_for("z=2.5+HYPE:3.0"), session)
    assert "wider 3.0" in hype
    assert "same animal" in hype

    all_sym = mod.experiment_block(result_for("z=3.0 all"), session)
    assert "uniformly stricter" in all_sym

    fallback = mod.experiment_block(result_for("z=9.9 unknown-tag"), session)
    assert "parameter variant" in fallback


# --- artifact shape --------------------------------------------------------

def test_artifact_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ARTIFACT_DIR", tmp_path)
    session, _ = _session("delay=0 stopout=OFF", "KEEP")
    path = mod.write_artifact(session)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["family"] == "flush_fade"
    assert loaded["results"][0]["verdict"] == "KEEP"
