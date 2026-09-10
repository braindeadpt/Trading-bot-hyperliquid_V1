"""Tests for scripts/research/overnight_runner.py — the gated overnight sweep.

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
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from scripts.research import overnight_runner as mod  # noqa: E402


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

class TestCandleSourceProvenance:
    """A verdict must be traceable to which DB its candles actually came
    from (research_db vs a live-bot.db fallback), not just that a fallback
    happened somewhere in the run."""

    def test_no_field_returns_none(self):
        assert mod.summarize_candle_sources([cell(1, 1, 1, 1)]) is None

    def test_single_source_collapses_to_one_group(self):
        cells = [
            {**cell(1, 1, 1, 1), "candle_source": {
                "BTC": {"1m": "research_db", "5m": "research_db",
                        "15m": "research_db", "1h": "research_db"}}},
        ]
        assert mod.summarize_candle_sources(cells) == "research_db (1m,5m,15m,1h)"

    def test_mixed_sources_grouped_per_timeframe(self):
        cells = [
            {**cell(1, 1, 1, 1), "candle_source": {
                "BTC": {"1m": "research_db", "5m": "research_db",
                        "15m": "live_bot_db", "1h": "live_bot_db"}}},
        ]
        assert mod.summarize_candle_sources(cells) == (
            "research_db (1m,5m) / live_bot_db (15m,1h)"
        )

    def test_disagreeing_cells_flagged_mixed(self):
        cells = [
            {**cell(1, 1, 1, 1), "candle_source": {"BTC": {"1m": "research_db"}}},
            {**cell(1, 1, 1, 1), "candle_source": {"BTC": {"1m": "live_bot_db"}}},
        ]
        assert mod.summarize_candle_sources(cells) == "mixed (1m)"

    def test_experiment_block_includes_candle_source_line(self):
        session = {"family": "flush_fade", "span": {"start": "2026-01-01", "end": "2026-01-10", "split_days": 5}}
        variant_cells = [
            {**cell(5, 10, 20, 15), "candle_source": {
                "BTC": {"1m": "research_db", "15m": "live_bot_db"}}},
        ]
        result = {
            "tag": "delay=0 stopout=OFF", "verdict": "DISCARD",
            "aggregate_baseline": {"pnl": -10.0, "n": 10, "pf": 0.5},
            "aggregate_variant": {"pnl": 5.0, "n": 10, "pf": 1.2},
            "windows": [("2026-01-01", "2026-01-05")], "deltas": [15.0],
            "reasons": ["windows improved 1/1 (majority=yes)"],
            "variant_cells": variant_cells,
        }
        block = mod.experiment_block(result, session)
        assert "- candle source: research_db (1m) / live_bot_db (15m)" in block


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


def test_discard_low_pf_beats_n_gate_even_below_n30():
    # PF<=1 with n<30: the ordering fix — a negative result (still losing
    # money) is DISCARD regardless of n, never parked as INCONCLUSIVE.
    base = [cell(-50, 10, 30, 80), cell(-40, 8, 20, 60)]
    small_losing = [cell(-10, 9, 20, 30), cell(-8, 7, 15, 23)]
    v, reasons = mod.decide(base, small_losing)
    assert v == "DISCARD"
    assert not any("n<30" in r for r in reasons)


def test_discard_catastrophic_beats_n_gate_even_below_n30():
    # Catastrophic window with n<30: still DISCARD, not INCONCLUSIVE — a
    # catastrophic result doesn't need n>=30 to be disqualifying.
    base = [cell(-50, 10, 30, 80), cell(-10, 8, 40, 50)]
    catastro_small = [cell(5, 9, 20, 15), cell(-120, 9, 20, 140)]
    v, reasons = mod.decide(base, catastro_small)
    assert v == "DISCARD"
    assert any("catastrophic" in r for r in reasons)


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


def test_families_defined_exactly_once():
    """FAMILIES must be assigned exactly once in the module source — a
    second later assignment would silently shadow the first (dead code),
    and any family only wired into the shadowed dict would vanish from
    the sweep without an error."""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assignments = re.findall(r"^FAMILIES\s*=\s*\{", src, flags=re.MULTILINE)
    assert len(assignments) == 1, (
        f"expected exactly one top-level `FAMILIES = {{` assignment, found "
        f"{len(assignments)}"
    )
    for name in ("flush_fade", "vwap_thresholds", "iv_thresholds", "hype_vwap_refine"):
        assert name in mod.FAMILIES, f"family {name!r} missing from FAMILIES"


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
    # 4/4 windows improved → sign-flip p=2^-4=0.0625. With 2 variants the
    # Bonferroni family correction sets alpha_eff=0.10/2=0.05, so
    # p=0.0625 no longer clears the bar → INCONCLUSIVE (the correction is
    # the point: N cells fishing one lucky window must not promote).
    assert improved["alpha_eff"] == 0.05
    assert improved["n_family_variants"] == 2
    assert improved["verdict"] == "INCONCLUSIVE"
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


# --- hype_vwap_refine family (Q6) -------------------------------------------

class TestHypeVwapRefineFamily:
    """The HYPE-only VWAP refinement sweep preregistered in QUEUE.md (Q6)."""

    def test_grid_is_preregistered_four_cells(self):
        # QUEUE.md Q6: baseline (production everywhere) vs HYPE-only z=3.5 /
        # z=4.0 / volume_surge=2.0. Baseline must be cell 0 and untouched.
        assert len(mod.HYPE_VWAP_REFINE_GRID) == 4
        assert mod.HYPE_VWAP_REFINE_GRID[0] == {}
        assert mod.HYPE_VWAP_REFINE_GRID[1] == {"HYPE": {"z_threshold": 3.5}}
        assert mod.HYPE_VWAP_REFINE_GRID[2] == {"HYPE": {"z_threshold": 4.0}}
        assert mod.HYPE_VWAP_REFINE_GRID[3] == {"HYPE": {"volume_surge": 2.0}}

    def test_resolve_symbol_overrides_precedence(self):
        # Nested per-symbol dict: only the named symbol's keys move, and the
        # wildcard '*' is NOT part of this family — a variant can never leak
        # onto the BTC/ETH control.
        section = {"z_threshold": 2.5, "volume_surge": 1.5, "enabled": False}
        h = mod.resolve_symbol_overrides(
            section, {"HYPE": {"z_threshold": 3.5}}, "HYPE")
        assert h["z_threshold"] == 3.5
        assert h["volume_surge"] == 1.5          # untouched
        assert h["enabled"] is False             # untouched
        b = mod.resolve_symbol_overrides(
            section, {"HYPE": {"z_threshold": 3.5}}, "BTC")
        assert b == section                       # control is byte-identical
        # volume_surge cell: z stays production.
        s = mod.resolve_symbol_overrides(
            section, {"HYPE": {"volume_surge": 2.0}}, "HYPE")
        assert s["volume_surge"] == 2.0 and s["z_threshold"] == 2.5
        # Unknown symbol + empty overrides -> untouched.
        assert mod.resolve_symbol_overrides(section, {}, "SOL") == section

    def test_db_for_window_seam(self):
        # Preregistered one-source-per-window rule via the GENERIC seam
        # resolver: research DB through the 2026-07-10 seam (W4 seals it),
        # live bot.db through the end of the Q6 window set (W5/W6).
        research, live = "E:/research/hyperliquid.db", "data/live/bot.db"
        specs = ((mod.HYPE_VWAP_SEAM_DATE, research),
                 (mod.HYPE_WINDOW_SPAN_END, live))
        for w_end in ("2026-04-11", "2026-05-11", "2026-06-10", "2026-07-10"):
            assert mod.db_for_window(specs, w_end) == research
        for w_end in ("2026-07-11", "2026-08-09", "2026-09-08"):
            assert mod.db_for_window(specs, w_end) == live
        assert mod.HYPE_VWAP_SEAM_DATE == "2026-07-10"
        assert mod.HYPE_WINDOW_SPAN_END == "2026-09-08"

    def test_family_registered(self):
        assert "hype_vwap_refine" in mod.FAMILIES


# --- generic per-window DB plans --------------------------------------------

class TestWindowDbPlan:
    """The generic multi-DB per-window seam mechanism (any family can mix
    DB sources at preregistered seams; sweep() validates and records it)."""

    def test_first_matching_seam_wins(self):
        specs = (("2026-07-10", "R"), ("2026-09-08", "L"))
        assert mod.db_for_window(specs, "2026-04-11") == "R"
        assert mod.db_for_window(specs, "2026-07-10") == "R"   # seam inclusive
        assert mod.db_for_window(specs, "2026-07-11") == "L"
        assert mod.db_for_window(specs, "2026-09-08") == "L"

    def test_three_source_plan(self):
        # Generic beyond two sources: a mid-history archive DB between the
        # early and the current one.
        specs = (("2026-03-31", "archive_a"), ("2026-06-30", "archive_b"),
                 ("2026-09-08", "live"))
        assert mod.db_for_window(specs, "2026-03-13") == "archive_a"
        assert mod.db_for_window(specs, "2026-05-11") == "archive_b"
        assert mod.db_for_window(specs, "2026-09-08") == "live"

    def test_uncovered_window_is_none_not_fallback(self):
        # No implicit catch-all: a window after the last seam resolves to
        # None — the sweep raises, it never silently uses another DB.
        specs = (("2026-07-10", "R"), ("2026-09-08", "L"))
        assert mod.db_for_window(specs, "2026-09-09") is None
        assert mod.db_for_window([], "2026-07-01") is None

    def test_window_db_plan_resolves_every_window(self):
        specs = (("2026-07-10", "R"), ("2026-09-08", "L"))
        windows = [("2026-03-13", "2026-04-11"), ("2026-04-12", "2026-05-11"),
                   ("2026-05-12", "2026-06-10"), ("2026-06-11", "2026-07-10"),
                   ("2026-07-11", "2026-08-09"), ("2026-08-10", "2026-09-08")]
        plan = mod.window_db_plan(specs, windows)
        assert len(plan) == 6
        assert all("db_path" in w for w in plan)
        assert [w["db_path"] for w in plan] == ["R"] * 4 + ["L"] * 2

    def test_hype_family_declares_the_q6_plan(self):
        _, _, _, db_specs = mod.hype_vwap_refine_family()
        assert len(db_specs) == 2
        assert db_specs[0][0] == "2026-07-10"       # research seam
        assert db_specs[1][0] == "2026-09-08"       # live seam = span end

    def test_sweep_records_window_dbs_and_fails_on_gap(self, monkeypatch):
        def fake_run_one(start, end, symbols, params):
            c = cell(0.0, 0, 0.0, 0.0)
            c["trade_pnls"] = []
            return c

        # Plan covering only the first 4 windows of the 6-window span.
        specs = (("2026-07-10", "R"),)     # W5/W6 uncovered
        monkeypatch.setitem(
            mod.FAMILIES, "gap_family",
            lambda: (["base"], fake_run_one, None, specs))
        with pytest.raises(ValueError, match="no preregistered DB seam"):
            mod.sweep("gap_family", "2026-03-13", "2026-09-08",
                      ["HYPE"], split_days=30)

        # Full plan: the session records the objective per-window source.
        specs_full = (("2026-07-10", "R"), ("2026-09-08", "L"))
        monkeypatch.setitem(
            mod.FAMILIES, "gap_family",
            lambda: (["base"], fake_run_one, None, specs_full))
        session = mod.sweep("gap_family", "2026-03-13", "2026-09-08",
                            ["HYPE"], split_days=30)
        assert [w["db_path"] for w in session["window_dbs"]] == ["R"] * 4 + ["L"] * 2



def test_sweep_dispatches_nested_dict_params_to_family(monkeypatch):
    """sweep() must pass the real hype_vwap_refine nested config dicts
    straight through to run_one — params is a per-symbol config dict, not a
    tuple and not an index."""
    seen: list = []

    def fake_run_one(start, end, symbols, params):
        seen.append((start, end, tuple(symbols), dict(params)))
        if params == {"HYPE": {"z_threshold": 3.5}}:
            c = cell(40.0, 20, 60.0, 20.0)        # improves every window
            c["trade_pnls"] = [2.0] * 20
        elif params == {}:                         # baseline
            c = cell(-20.0, 20, 10.0, 30.0)
            c["trade_pnls"] = [-1.0] * 20
        else:                                      # worse everywhere
            c = cell(-40.0, 20, 20.0, 60.0)
            c["trade_pnls"] = [-2.0] * 20
        return c

    tags = ["baseline (production 2.5σ)", "HYPE z_threshold=3.5",
            "HYPE z_threshold=4.0"]
    monkeypatch.setitem(mod.FAMILIES, "hype_vwap_refine",
                        lambda: (tags, fake_run_one, None))

    session = mod.sweep("hype_vwap_refine", "2026-03-13", "2026-09-08",
                        ["HYPE", "BTC", "ETH"], split_days=30)

    assert session["family"] == "hype_vwap_refine"
    assert len(session["windows"]) == 6           # the Q6 window set
    # 3 cells x 6 windows = 18 run_one calls, nested dicts passed verbatim.
    assert len(seen) == 18
    baseline_calls = [s for s in seen if s[3] == {}]
    hype_calls = [s for s in seen if s[3] == {"HYPE": {"z_threshold": 3.5}}]
    assert len(baseline_calls) == 6 and len(hype_calls) == 6
    assert all(s[2] == ("HYPE", "BTC", "ETH") for s in seen)

    by_tag = {r["tag"]: r for r in session["results"]}
    improved = by_tag["HYPE z_threshold=3.5"]
    assert improved["aggregate_variant"]["pnl"] == 240.0
    assert improved["noise_gate"]["evaluated"] is True
    # 6/6 windows improved -> sign-flip p=2^-6=0.0156 <= 0.10, n=120 >= 30.
    assert improved["verdict"] == "KEEP"
    assert by_tag["HYPE z_threshold=4.0"]["verdict"] == "DISCARD"


# --- vwap_exit_econ / vwap_exhaustion / vwap_deceleration (Q7/Q4/Q5) ----------

class TestVwapExitEconFamily:
    def test_family_registered(self):
        for name in ("vwap_exit_econ", "vwap_exhaustion", "vwap_deceleration"):
            assert name in mod.FAMILIES, f"family {name!r} missing from FAMILIES"

    def test_grids_match_queue_preregistration(self):
        # Q7: 5 cells (baseline + 4 exit variants) — fixed a priori in QUEUE.md
        assert len(mod.VWAP_EXIT_ECON_GRID) == 5
        assert mod.VWAP_EXIT_ECON_GRID[0] == {}
        assert {"exit_z_threshold": 0.5} in mod.VWAP_EXIT_ECON_GRID
        assert {"take_profit_r_multiple": 1.5} in mod.VWAP_EXIT_ECON_GRID
        assert {"max_hold_hours": 2} in mod.VWAP_EXIT_ECON_GRID
        # Q4/Q5: 4 cells each, cell 0 is always the untouched baseline
        assert len(mod.VWAP_EXHAUSTION_GRID) == 4
        assert len(mod.VWAP_DECELERATION_GRID) == 4
        assert mod.VWAP_EXHAUSTION_GRID[0]["gate"] is None
        assert mod.VWAP_DECELERATION_GRID[0]["gate"] is None
        # gate kinds used by the grids must be wired in _vwap_fade_run_one
        gates = ({c.get("gate") for c in mod.VWAP_EXHAUSTION_GRID} |
                 {c.get("gate") for c in mod.VWAP_DECELERATION_GRID}) - {None}
        for g in gates:
            assert g in ("decay", "below_mean", "retrace_0.15",
                         "retrace_0.30", "counter_close")

    def test_sweep_dispatches_exit_econ_dicts_verbatim(self, monkeypatch):
        """sweep() passes VWAP_EXIT_ECON_GRID dicts straight to run_one."""
        seen: list = []

        def fake_run_one(start, end, symbols, params):
            seen.append(dict(params))
            return cell(-20.0, 20, 10.0, 30.0)

        tags = [f"cell{i}" for i in range(len(mod.VWAP_EXIT_ECON_GRID))]
        monkeypatch.setitem(mod.FAMILIES, "vwap_exit_econ",
                            lambda: (tags, fake_run_one, None))
        session = mod.sweep("vwap_exit_econ", "2026-05-18", "2026-09-08",
                            ["BTC"], split_days=30)
        assert len(session["windows"]) == 4
        assert len(seen) == 20  # 5 cells x 4 windows
        assert seen[0] == {}
        assert {"take_profit_r_multiple": 1.5} in seen

    def test_sweep_dispatches_gate_cells_verbatim(self, monkeypatch):
        """Q4/Q5 grids pass {overrides, gate} dicts through untouched."""
        seen: list = []

        def fake_run_one(start, end, symbols, params):
            seen.append(dict(params))
            return cell(-20.0, 20, 10.0, 30.0)

        for fam, grid in (("vwap_exhaustion", mod.VWAP_EXHAUSTION_GRID),
                          ("vwap_deceleration", mod.VWAP_DECELERATION_GRID)):
            seen.clear()
            tags = [f"c{i}" for i in range(len(grid))]
            monkeypatch.setitem(mod.FAMILIES, fam,
                                lambda t=tags, f=fake_run_one: (t, f, None))
            session = mod.sweep(fam, "2026-05-18", "2026-09-08",
                                ["BTC"], split_days=30)
            assert len(seen) == len(grid) * 4
            assert seen[0]["gate"] is None
            assert any(s.get("gate") for s in seen)


class TestEntryGates:
    def _mk_event(self, sym, ts, close=100.0, open_=100.0,
                  buy=None, sell=None, c1h=None):
        from src.strategies.base import MarketEvent
        from src.strategies.indicators import Candle
        bar = Candle(open=open_, high=max(open_, close), low=min(open_, close),
                     close=close, volume=1.0, timestamp_ms=ts,
                     buy_volume=buy, sell_volume=sell)
        return MarketEvent(symbol=sym, price=close, timestamp_ms=ts,
                           candle_15m=bar, candle_1h=c1h)

    def _mk_sig(self, sym, side, z=3.0):
        from src.strategies.base import Signal
        return Signal(strategy="VWAPDeviation", symbol=sym, side=side,
                      confidence=0.7, size_pct=0.01, entry_price=100.0,
                      metadata={"zscore": z})

    def test_exhaustion_decay_vetoes_rising_aggressor(self):
        g = mod._ExhaustionGate("decay")
        sym = "BTC"
        # sellers still accelerating on a fade-LONG -> veto; decaying -> allow
        g.update(self._mk_event(sym, 1, buy=40.0, sell=60.0))
        ev = self._mk_event(sym, 2, buy=40.0, sell=80.0)
        g.update(ev)
        assert g.allow(ev, self._mk_sig(sym, "long")) is False
        ev2 = self._mk_event(sym, 3, buy=40.0, sell=50.0)
        g.update(ev2)
        assert g.allow(ev2, self._mk_sig(sym, "long")) is True

    def test_exhaustion_missing_volume_is_veto_not_pass(self):
        g = mod._ExhaustionGate("decay")
        ev = self._mk_event("BTC", 1, buy=None, sell=None)
        g.update(ev)
        ev2 = self._mk_event("BTC", 2, buy=None, sell=None)
        g.update(ev2)
        assert g.allow(ev2, self._mk_sig("BTC", "long")) is False

    def test_decel_retrace_blocks_fresh_extreme(self):
        g = mod._DecelGate(retrace=0.15)
        g.set_threshold(2.5)
        g._extreme["BTC"] = 3.2   # excursion already extended to 3.2
        ev = self._mk_event("BTC", 1)
        # z=3.1 is a retrace of only 0.10 -> still vetoed
        assert g.allow(ev, self._mk_sig("BTC", "long", z=-3.1)) is False
        # z=3.0 retraced 0.20 -> allowed
        assert g.allow(ev, self._mk_sig("BTC", "long", z=-3.0)) is True

    def test_decel_counter_close(self):
        g = mod._DecelGate(retrace=None)
        g.set_threshold(2.5)
        # fade-LONG needs a bar that closed UP (against the dump)
        ev_up = self._mk_event("BTC", 1, open_=99.0, close=100.0)
        ev_dn = self._mk_event("BTC", 2, open_=101.0, close=100.0)
        assert g.allow(ev_up, self._mk_sig("BTC", "long")) is True
        assert g.allow(ev_dn, self._mk_sig("BTC", "long")) is False
        # and mirror for shorts
        assert g.allow(ev_dn, self._mk_sig("BTC", "short")) is True


# --- iv_thresholds family (Night 3) ------------------------------------------

class TestIvThresholdsFamily:
    """The high/low-IV cut sweep preregistered in QUEUE.md (Night 3)."""

    def test_grid_is_preregistered_four_cells(self):
        # QUEUE.md Night 3: baseline (no gate) vs 63.3 / 66.7 / 70. The
        # baseline must be cell 0 (None = no gate).
        assert len(mod.IV_THRESHOLDS_GRID) == 4
        assert mod.IV_THRESHOLDS_GRID[0] is None
        assert mod.IV_THRESHOLDS_GRID[1] == 63.3
        assert mod.IV_THRESHOLDS_GRID[2] == 66.7
        assert mod.IV_THRESHOLDS_GRID[3] == 70.0

    def test_span_is_dvol_bounded(self):
        # The preregistered span starts where the DVOL feed starts. If DVOL
        # history is ever extended/rebased, this pin must be revisited
        # deliberately (it fixes the K=3 accumulator reality).
        assert mod.IV_THRESHOLDS_SPAN == ("2026-06-14", "2026-09-08")

    def test_family_registered(self):
        assert "iv_thresholds" in mod.FAMILIES


def test_apply_iv_gate_semantics():
    """Pure gate: keep iff pct > cut; None NEVER survives; baseline keeps all."""
    raw = [
        {"pnl_usd": 10.0, "_iv_pct": 80.0},   # above every cut
        {"pnl_usd": -5.0, "_iv_pct": 70.0},   # above 63.3/66.7, blocked at 70
        {"pnl_usd": -8.0, "_iv_pct": 40.0},   # blocked everywhere
        {"pnl_usd": 3.0, "_iv_pct": None},    # unclassifiable — blocked everywhere
    ]
    kept, blocked = mod.apply_iv_gate(raw, None)
    assert len(kept) == 4 and not blocked          # baseline: no gate at all
    kept, blocked = mod.apply_iv_gate(raw, 63.3)
    assert [t["pnl_usd"] for t in kept] == [10.0, -5.0]
    assert {t["pnl_usd"] for t in blocked} == {-8.0, 3.0}
    kept, blocked = mod.apply_iv_gate(raw, 70.0)
    assert [t["pnl_usd"] for t in kept] == [10.0]  # 70.0 is not > 70.0
    # Unclassifiable never survives a gate — it is not evidence.
    kept, _ = mod.apply_iv_gate([{"pnl_usd": 99.0, "_iv_pct": None}], 1.0)
    assert kept == []


def test_iv_cell_from_raw_contract():
    """Cell dicts carry the full runner contract incl. per-trade PnLs for
    the noise gate, plus the IV-specific forensics."""
    raw = [
        {"pnl_usd": 10.0, "_iv_pct": 80.0, "_strategy": "VWAPDeviation"},
        {"pnl_usd": -4.0, "_iv_pct": 50.0, "_strategy": "VWAPDeviation"},
        {"pnl_usd": -1.0, "_iv_pct": None, "_strategy": "VolatilityBreakout"},
    ]
    kept, blocked = mod.apply_iv_gate(raw, 66.7)
    c = mod.iv_cell_from_raw(raw, kept, blocked, 66.7, ["VolatilityBreakout", "VWAPDeviation"])
    assert c["iv_cut"] == 66.7
    assert c["n_trades"] == 1 and c["total_pnl_usd"] == 10.0
    assert c["trade_pnls"] == [10.0]
    assert c["n_raw"] == 3 and c["n_no_iv"] == 1 and c["n_blocked"] == 2
    assert c["blocked_pnl_usd"] == -5.0
    assert c["n_by_strategy"] == {"VolatilityBreakout": 0, "VWAPDeviation": 1}


def test_sweep_dispatches_iv_cuts_to_family(monkeypatch):
    """sweep() must hand the real IV_THRESHOLDS_GRID values (None/63.3/66.7/70)
    to run_one — cuts, not indices (the Night 2 dispatch lesson)."""
    seen: list = []

    def fake_run_one(start, end, symbols, params):
        seen.append((start, end, tuple(symbols), params))
        if params is None:                    # baseline bleeds
            c = cell(-30.0, 30, 10.0, 40.0)
            c["trade_pnls"] = [-1.0] * 30
        else:                                 # every cut 'improves' by pruning
            c = cell(12.0, 10, 20.0, 8.0)
            c["trade_pnls"] = [1.2] * 10
        return c

    tags = ["no gate (baseline)", "high_iv>63.3", "high_iv>66.7", "high_iv>70"]
    monkeypatch.setitem(mod.FAMILIES, "iv_thresholds",
                        lambda: (tags, fake_run_one, None))

    session = mod.sweep("iv_thresholds", "2026-06-14", "2026-09-08",
                        ["BTC"], split_days=30)

    assert session["family"] == "iv_thresholds"
    cuts_seen = {s[3] for s in seen}
    assert cuts_seen == {None, 63.3, 66.7, 70.0}
    # 4 cells × 3 windows = 12 calls
    assert len(seen) == 12
    # Pruning losses can 'improve' every window (and even carry PF>1), but
    # n=10 < 30 -> INCONCLUSIVE, never KEEP: the gate cannot be promoted on a
    # shrunken sample. (And at K=3 the sign-flip floor is 0.125 > alpha — a
    # second, independent reason this session can never KEEP.)
    for r in session["results"]:
        assert r["verdict"] == "INCONCLUSIVE"
        assert any("n=" in x for x in r["reasons"])


def test_experiment_block_carries_preregistered_iv_hypothesis():
    windows = [["2026-06-14", "2026-07-13"], ["2026-07-14", "2026-08-12"],
               ["2026-08-13", "2026-09-08"]]
    result = {
        "tag": "high_iv>66.7", "verdict": "INCONCLUSIVE", "windows": windows,
        "deltas": [1.0, 1.0, None],
        "aggregate_baseline": {"pnl": -100.0, "n": 40, "pf": 0.8},
        "aggregate_variant": {"pnl": -90.0, "n": 20, "pf": 0.9},
        "reasons": ["n=20 (gate >=30)"], "baseline_tag": "no gate (baseline)",
    }
    session = {"family": "iv_thresholds", "baseline_tag": "no gate (baseline)",
               "span": {"start": "2026-06-14", "end": "2026-09-08", "split_days": 30}}
    blk = mod.experiment_block(result, session)
    assert "high_iv regime concentrates" in blk
    assert "63.3/66.7/70" in blk


def test_iv_run_one_caches_engine_pass(monkeypatch):
    """The engine pass is identical for every cut — run_one must reuse the
    cached raw trades (1 engine run per window, not 1 per cell)."""
    calls = {"n": 0}

    class FakeDB:
        def close(self):
            pass

    import scripts.research.iv_high_only_ab_split as ivmod
    from src.data.dvol_feed import DVOL_WINDOW_DAYS, build_iv_percentile

    # Deterministic DVOL: 60 flat days then 3 extreme-high closes. A trade
    # entering the day AFTER a high close carries that day's percentile
    # (iv_pct_at uses the last completed DVOL day) — >70 for all three.
    base = 1_740_000_000_000
    day = 86_400_000
    closes = [(base + i * day, 50.0) for i in range(60)]
    closes += [(base + 60 * day, 100.0), (base + 61 * day, 90.0),
               (base + 62 * day, 80.0)]
    iv_series = build_iv_percentile(closes, DVOL_WINDOW_DAYS)

    raw_trades = [
        {"symbol": "BTC", "entry_time": base + 61 * day + 12 * 3_600_000,
         "pnl_usd": 5.0},
        {"symbol": "BTC", "entry_time": base + 62 * day + 12 * 3_600_000,
         "pnl_usd": -2.0},
        {"symbol": "ETH", "entry_time": base + 63 * day + 12 * 3_600_000,
         "pnl_usd": 3.0},
    ]

    monkeypatch.setattr(ivmod, "SPECS",
                        [("VWAPDeviation", object, "strategy.vwap_deviation")])

    import scripts.research.regime_router_a_b_test as rr

    def fake_run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols):
        calls["n"] += 1
        return [dict(t) for t in raw_trades]

    monkeypatch.setattr(rr, "run_strategy", fake_run_strategy)

    # DB constructor + DVOL loader stubbed; loader returns one shared series.
    # **_kw: the research call sites pass read_only=True (the live trading DB
    # is opened mode=ro so a sweep can never block on the bot's writer lock).
    monkeypatch.setattr("src.data.database.Database", lambda _p, **_kw: FakeDB())
    monkeypatch.setattr(
        mod, "_load_dvol_series",
        lambda symbols, s_ms, e_ms: ({s: iv_series for s in symbols}, iv_series))

    mod._IV_RAW_CACHE.clear()
    tags, run_one, _cfg = mod.iv_thresholds_family()
    assert tags[0] == "no gate (baseline)"

    c1 = run_one("2026-06-14", "2026-07-13", ["BTC", "ETH"], None)
    c2 = run_one("2026-06-14", "2026-07-13", ["BTC", "ETH"], 66.7)
    c3 = run_one("2026-06-14", "2026-07-13", ["BTC", "ETH"], 70.0)
    assert calls["n"] == 1, "engine pass must be cached across cuts"
    assert c1["n_trades"] == 3 and c2["n_trades"] == 3 and c3["n_trades"] == 3
    assert c2["n_no_iv"] == 0 and c3["n_no_iv"] == 0
    assert c2["n_raw"] == 3 and c2["n_blocked"] == 0


# --- per-symbol noise slices (advisory) ---------------------------------------

def _sym_cell(pnl: float, n: int, gw: float, gl: float,
              sym_pnls: dict) -> dict:
    c = cell(pnl, n, gw, gl)
    c["trade_pnls"] = [x for v in sym_pnls.values() for x in v]
    syms = []
    for s, v in sym_pnls.items():
        syms += [s] * len(v)
    c["trade_symbols"] = syms
    return c


class TestSymbolNoiseGate:
    """The cell-level sign-flip test restricted to one symbol — advisory."""

    def test_all_positive_k4_passes_and_reports_symbol(self):
        base = [_sym_cell(-20.0, 10, 5.0, 25.0, {"BTC": [-2.0] * 10}),
                _sym_cell(-10.0, 10, 4.0, 14.0, {"BTC": [-1.0] * 10}),
                _sym_cell(-15.0, 10, 5.0, 20.0, {"BTC": [-1.5] * 10}),
                _sym_cell(-12.0, 10, 4.0, 16.0, {"BTC": [-1.2] * 10})]
        good = [_sym_cell(10.0, 10, 20.0, 10.0, {"BTC": [1.0] * 10}),
                _sym_cell(12.0, 10, 22.0, 10.0, {"BTC": [1.2] * 10}),
                _sym_cell(8.0, 10, 18.0, 10.0, {"BTC": [0.8] * 10}),
                _sym_cell(9.0, 10, 19.0, 10.0, {"BTC": [0.9] * 10})]
        g = mod.symbol_noise_gate(base, good, "BTC")
        assert g["evaluated"] is True and g["pass"] is True
        assert g["p_value"] == 0.0625            # 2^-4: all deltas positive
        assert g["symbol"] == "BTC" and g["advisory_only"] is True
        assert g["n_variant"] == 40 and g["n_windows"] == 4

    def test_one_negative_window_raises_p_like_cell_gate(self):
        base = [_sym_cell(-20.0, 10, 5.0, 25.0, {"BTC": [-2.0] * 10}),
                _sym_cell(-10.0, 10, 4.0, 14.0, {"BTC": [-1.0] * 10}),
                _sym_cell(-15.0, 10, 5.0, 20.0, {"BTC": [-1.5] * 10}),
                _sym_cell(-12.0, 10, 4.0, 16.0, {"BTC": [-1.2] * 10})]
        good = [_sym_cell(10.0, 10, 20.0, 10.0, {"BTC": [1.0] * 10}),
                _sym_cell(12.0, 10, 22.0, 10.0, {"BTC": [1.2] * 10}),
                _sym_cell(8.0, 10, 18.0, 10.0, {"BTC": [0.8] * 10}),
                _sym_cell(-30.0, 10, 5.0, 35.0, {"BTC": [-3.0] * 10})]
        g = mod.symbol_noise_gate(base, good, "BTC")
        assert g["evaluated"] is True and g["pass"] is False
        assert g["p_value"] == 0.125             # ties count in the null

    def test_missing_per_symbol_data_is_skipped_not_invented(self):
        base = [cell(-20.0, 10, 5.0, 25.0),       # no trade_pnls at all
                cell(-10.0, 10, 4.0, 14.0)]
        var = [_sym_cell(10.0, 10, 20.0, 10.0, {"BTC": [1.0] * 10}),
               cell(12.0, 10, 22.0, 10.0)]        # second window lacks the map
        g = mod.symbol_noise_gate(base, var, "BTC")
        assert g["evaluated"] is False and g["pass"] is None
        assert "skipped" in g["reason"]

    def test_double_zero_window_is_no_evidence_not_improvement(self):
        base = [_sym_cell(-20.0, 10, 5.0, 25.0, {"BTC": [-2.0] * 10}),
                _sym_cell(0.0, 0, 0.0, 0.0, {"BTC": []})]
        var = [_sym_cell(10.0, 10, 20.0, 10.0, {"BTC": [1.0] * 10}),
               _sym_cell(0.0, 0, 0.0, 0.0, {"BTC": []})]
        g = mod.symbol_noise_gate(base, var, "BTC")
        assert g["evaluated"] is True
        assert g["n_windows"] == 1               # only the traded pair


def test_no_weakening_cell_gate_ignores_symbol_slices():
    """THE invariant: decide() output is byte-identical with and without
    per-symbol data, and a slice that looks great can never flip a cell
    verdict. The cell-level gate stays the only promotion gate."""
    base = [dict(cell(-20.0, 20, 10.0, 30.0), trade_pnls=[-1.0] * 20,
                 trade_pnls_by_symbol={"BTC": [-1.0] * 20, "HYPE": []}),
            dict(cell(-10.0, 15, 6.0, 16.0), trade_pnls=[-0.7] * 15,
                 trade_pnls_by_symbol={"BTC": [-0.7] * 15, "HYPE": []}),
            dict(cell(-15.0, 18, 8.0, 23.0), trade_pnls=[-0.8] * 18,
                 trade_pnls_by_symbol={"BTC": [-0.8] * 18, "HYPE": []}),
            dict(cell(-12.0, 16, 7.0, 19.0), trade_pnls=[-0.75] * 16,
                 trade_pnls_by_symbol={"BTC": [-0.75] * 16, "HYPE": []})]
    # Variant: HYPE slice is stellar (all windows positive, would pass the
    # sign-flip at K=4) — but the CELL aggregate loses and PF<1.
    var = [dict(cell(-30.0, 25, 12.0, 42.0), trade_pnls=[-1.2] * 25,
                trade_pnls_by_symbol={"BTC": [-2.0] * 25,
                                      "HYPE": [1.0] * 5}),
           dict(cell(-25.0, 22, 10.0, 35.0), trade_pnls=[-1.1] * 22,
                trade_pnls_by_symbol={"BTC": [-1.8] * 22,
                                      "HYPE": [0.9] * 4}),
           dict(cell(-28.0, 24, 11.0, 39.0), trade_pnls=[-1.15] * 24,
                trade_pnls_by_symbol={"BTC": [-1.9] * 24,
                                      "HYPE": [1.1] * 5}),
           dict(cell(-22.0, 20, 9.0, 31.0), trade_pnls=[-1.05] * 20,
                trade_pnls_by_symbol={"BTC": [-1.7] * 20,
                                      "HYPE": [0.8] * 4})]
    without = mod.decide(base, var, noise_model=True)
    hype_slice = mod.symbol_noise_gate(base, var, "HYPE")
    assert hype_slice["evaluated"] is True and hype_slice["pass"] is True
    with_slices = mod.decide(base, var, noise_model=True)
    assert without == with_slices
    assert without[0] != "KEEP"                # cell gate says no — slices can't overrule


def test_sweep_attaches_symbol_gates_and_decide_reasons_stay_clean(monkeypatch):
    """sweep() attaches evaluated slices to the result; decide() reasons
    never mention them (advisory stays out of the verdict vocabulary)."""
    def fake_run_one(start, end, symbols, params):
        if params == {"HYPE": 3.0}:
            c = _sym_cell(40.0, 20, 60.0, 20.0,
                          {"BTC": [2.0] * 10, "HYPE": [2.0] * 10})
        else:
            c = _sym_cell(-20.0, 20, 10.0, 30.0,
                          {"BTC": [-1.0] * 10, "HYPE": [-1.0] * 10})
        return c

    tags = ["z=2.5 (baseline)", "z=2.5+HYPE:3.0"]
    monkeypatch.setitem(mod.FAMILIES, "vwap_thresholds",
                        lambda: (tags, fake_run_one, None))
    session = mod.sweep("vwap_thresholds", "2026-05-18", "2026-09-08",
                        ["BTC", "HYPE"], split_days=30)
    r = session["results"][0]
    syms = {g["symbol"] for g in r["symbol_gates"] if g.get("evaluated")}
    assert syms == {"BTC", "HYPE"}
    for g in r["symbol_gates"]:
        assert g.get("advisory_only") is True
    assert all("symbol" not in x.lower() for x in r["reasons"])


def test_ledger_block_renders_advisory_symbol_line():
    windows = [["2026-05-18", "2026-06-16"], ["2026-06-17", "2026-07-16"],
               ["2026-07-17", "2026-08-15"], ["2026-08-16", "2026-09-08"]]
    result = {
        "tag": "z=2.5+HYPE:3.0", "verdict": "DISCARD", "windows": windows,
        "deltas": [1.0, 1.0, -1.0, -1.0],
        "aggregate_baseline": {"pnl": -100.0, "n": 40, "pf": 0.8},
        "aggregate_variant": {"pnl": -96.0, "n": 38, "pf": 0.82},
        "reasons": ["windows improved 2/4"], "baseline_tag": "z=2.5 (baseline)",
        "symbol_gates": [
            {"evaluated": True, "symbol": "HYPE", "observed_delta": 5.34,
             "n_variant": 39, "p_value": 0.125, "pass": False},
            {"evaluated": False, "symbol": "SOL", "pass": None,
             "reason": "per-symbol PnL unavailable — slice skipped"},
        ],
    }
    session = {"family": "vwap_thresholds", "baseline_tag": "z=2.5 (baseline)",
               "span": {"start": "2026-05-18", "end": "2026-09-08",
                        "split_days": 30}}
    blk = mod.experiment_block(result, session)
    assert "symbol slices (ADVISORY" in blk
    assert "HYPE delta=+5.34" in blk and "p=0.125" in blk
    assert "SOL" not in blk                    # skipped slices stay out
    # And the advisory marker is present so no one reads it as a gate.
    assert "the cell verdict is the only gate" in blk


def test_trade_pnls_by_symbol_extractor():
    good = {"total_pnl_usd": 3.0, "n_trades": 2,
            "trade_pnls": [1.0, 2.0], "trade_symbols": ["BTC", "HYPE"]}
    maps = mod.trade_pnls_by_symbol([good])
    assert maps[0] == {"BTC": [1.0], "HYPE": [2.0]}
    assert good["trade_pnls_by_symbol"] == maps[0]   # attached in place
    bad = {"total_pnl_usd": 1.0, "n_trades": 1,
           "trade_pnls": [1.0], "trade_symbols": ["BTC", "ETH"]}  # misaligned
    maps = mod.trade_pnls_by_symbol([bad])
    assert maps[0] is None and "trade_pnls_by_symbol" not in bad
    legacy = cell(1.0, 1, 1.0, 0.0)            # no symbol lists at all
    maps = mod.trade_pnls_by_symbol([legacy])
    assert maps[0] is None and "trade_pnls_by_symbol" not in legacy


# --- DVOL coverage-gated span (Night 3 reopen path) ------------------------

def test_dvol_span_from_rows_bounds():
    rows = [(1_788_000_000_000, 42.0), (1_788_086_400_000, 41.5),
            (1_788_172_800_000, 43.0)]
    start, end = mod.dvol_span_from_rows(rows)
    assert start == "2026-08-29" and end == "2026-08-31"


def test_dvol_span_from_rows_empty_raises():
    with pytest.raises(RuntimeError, match="no closes"):
        mod.dvol_span_from_rows([])


def test_coverage_window_count_matches_night3_math():
    # the accumulator's K=3 (DVOL starts 06-14; 88d of coverage)
    assert mod.coverage_window_count("2026-06-14", "2026-09-10") == 3
    # ~120d of coverage clears the K=4 noise-gate floor
    assert mod.coverage_window_count("2026-06-14", "2026-10-12") >= 4
    assert mod.IV_COVERAGE_MIN_WINDOWS == 4


# --- artifact shape --------------------------------------------------------

def test_artifact_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ARTIFACT_DIR", tmp_path)
    session, _ = _session("delay=0 stopout=OFF", "KEEP")
    path = mod.write_artifact(session)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["family"] == "flush_fade"
    assert loaded["results"][0]["verdict"] == "KEEP"
