"""ResMom per-arm card/detail/feed separation + APY reliability flag.

Presentation-layer only (src/research/paper_trades.py reads ledgers in
mode=ro). The registered ResMom arm is A; B is descriptive and BIN is a
Binance reference — they must never leak into the headline numbers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.research.paper_trades as pt
from src.resmom_shadow import ledger as rm_ledger

MS_WEEK = 7 * 86_400_000
# A Monday 00:00 UTC (week keys are Monday-aligned in resmom read.py)
T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)


def _mk_db(path: Path, nav: dict, positions: list, rebs: dict) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(rm_ledger._SCHEMA)
    for arm, navs in nav.items():
        for i, v in enumerate(navs):
            con.execute(
                "INSERT INTO resmom_nav (ts_ms, arm, nav_usd, gross_usd,"
                " n_positions, dd_pct) VALUES (?,?,?,?,?,?)",
                (T0 + i * MS_WEEK + 3_600_000, arm, v, v, 0, -1.5))
    for arm, n in rebs.items():
        for i in range(n):
            con.execute(
                "INSERT INTO resmom_rebalances (week_key_ms, arm,"
                " created_at_ms, universe_json, signals_json, weights_json,"
                " turnover_usd, cost_usd, inputs_sha256)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (T0 + i * MS_WEEK, arm, T0, "{}", "{}", "{}", 0.0, 0.0, "x"))
    for (arm, sym, status, pnl, exit_ms) in positions:
        con.execute(
            "INSERT INTO resmom_positions (rebalance_id, arm, symbol, side,"
            " weight, qty, entry_px, entry_ms, exit_px, exit_ms, status,"
            " funding_usd, pnl_usd, close_reason)"
            " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (arm, sym, "long", 0.1, 1.0, 100.0, T0,
             100.0 if status == "closed" else None, exit_ms, status,
             0.5, pnl if status == "closed" else None,
             "weekly" if status == "closed" else None))
    con.commit()
    con.close()


@pytest.fixture()
def three_arms(tmp_path, monkeypatch):
    nav = {
        "A": [10_000.0, 10_100.0, 10_200.0],
        "B": [10_000.0, 9_000.0, 8_000.0, 7_000.0],
        "BIN": [10_000.0, 12_000.0],
    }
    positions = [
        ("A", "BTC", "open", None, None),
        ("A", "ETH", "closed", 5.0, T0 + MS_WEEK),
        ("B", "SOL", "open", None, None),
        ("B", "DOGE", "open", None, None),
        ("B", "ADA", "closed", -3.0, T0 + MS_WEEK),
        ("BIN", "BTCUSDT", "open", None, None),
        ("BIN", "ETHUSDT", "closed", 9.0, T0 + 2 * MS_WEEK),
    ]
    _mk_db(tmp_path / "resmom_shadow.db", nav, positions,
           {"A": 2, "B": 3, "BIN": 1})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    return tmp_path


@pytest.mark.unit
def test_card_headline_numbers_are_arm_a_only(three_arms):
    card = pt._resmom_card(T0 + 10 * MS_WEEK)
    assert card["n_open"] == 1                       # A only (B has 2)
    assert card["wr"] == 100.0                       # A's single winner
    assert [v for _t, v in card["equity"]] == [10_000.0, 10_100.0, 10_200.0]


@pytest.mark.unit
def test_card_arms_breakdown_matches_each_arm(three_arms):
    arms = pt._resmom_card(T0 + 10 * MS_WEEK)["arms"]
    assert set(arms) == {"A", "B", "BIN"}
    assert arms["A"]["nav"] == 10_200.0
    assert arms["A"]["cum_return_pct"] == 2.0
    assert arms["A"]["n_open"] == 1 and arms["A"]["n_rebalances"] == 2
    assert arms["A"]["registered"] is True
    assert arms["B"]["nav"] == 7_000.0
    assert arms["B"]["cum_return_pct"] == -30.0
    assert arms["B"]["n_open"] == 2 and arms["B"]["wr"] == 0.0
    assert arms["B"]["n_rebalances"] == 3 and arms["B"]["registered"] is False
    assert arms["BIN"]["nav"] == 12_000.0
    assert arms["BIN"]["cum_return_pct"] == 20.0
    assert arms["BIN"]["n_open"] == 1 and arms["BIN"]["n_rebalances"] == 1


@pytest.mark.unit
def test_arm_a_without_positions_is_visible_zero(tmp_path, monkeypatch):
    nav = {"A": [10_000.0, 10_000.0], "B": [10_000.0, 10_500.0],
           "BIN": [10_000.0, 10_700.0]}
    positions = [
        ("B", "SOL", "open", None, None),
        ("BIN", "BTCUSDT", "open", None, None),
        ("BIN", "ETHUSDT", "open", None, None),
    ]
    _mk_db(tmp_path / "resmom_shadow.db", nav, positions,
           {"A": 0, "B": 1, "BIN": 1})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    card = pt._resmom_card(T0 + 2 * MS_WEEK)
    assert card["n_open"] == 0
    assert card["arms"]["A"]["n_open"] == 0
    assert card["arms"]["A"]["n_rebalances"] == 0
    assert card["arms"]["B"]["n_open"] == 1
    assert card["arms"]["BIN"]["n_open"] == 2


@pytest.mark.unit
def test_missing_arm_present_with_nones(tmp_path, monkeypatch):
    _mk_db(tmp_path / "resmom_shadow.db", {"A": [10_000.0, 10_050.0]}, [],
           {"A": 1})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    arms = pt._resmom_card(T0 + MS_WEEK)["arms"]
    assert arms["B"]["nav"] is None and arms["B"]["n_open"] == 0
    assert arms["BIN"]["cum_return_pct"] is None


@pytest.mark.unit
def test_detail_marks_arm_and_equity_is_a(three_arms):
    d = pt._resmom_detail(T0 + 10 * MS_WEEK)
    assert {o["arm"] for o in d["open"]} == {"A", "B", "BIN"}
    assert {c["arm"] for c in d["closed"]} == {"A", "B", "BIN"}
    assert [v for _t, v in d["equity"]] == [10_000.0, 10_100.0, 10_200.0]
    assert len(d["dd"]) == 3


@pytest.mark.unit
def test_feed_labels_each_arm(three_arms):
    feed = pt.build_trades_feed(now_ms=T0 + 10 * MS_WEEK)
    by_sym = {t["symbol"]: t["strat"] for t in feed
              if t["symbol"] in ("ETH", "ADA", "ETHUSDT")}
    assert by_sym == {"ETH": "ResMom·A", "ADA": "ResMom·B (desc.)",
                      "ETHUSDT": "ResMom·BIN (ref.)"}


@pytest.mark.unit
def test_arm_cum_return_base_is_notional_not_first_mark(tmp_path, monkeypatch):
    """The first NAV mark already discounts entry cost — using it as the
    base hides that cost. cum_return_pct must be NAV / NOTIONAL_USD - 1."""
    # first mark 9 990 (10 entry cost already paid) -> honest return is
    # +1.90 % on the 10 000 notional, not +2.10 % vs the first mark.
    _mk_db(tmp_path / "resmom_shadow.db",
           {"A": [9_990.0, 10_100.0], "B": [9_990.0, 9_500.0],
            "BIN": [9_990.0, 10_500.0]}, [],
           {"A": 1, "B": 1, "BIN": 1})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    arms = pt._resmom_card(T0 + MS_WEEK)["arms"]
    assert arms["A"]["cum_return_pct"] == pytest.approx(1.0)
    assert arms["B"]["cum_return_pct"] == pytest.approx(-5.0)
    assert arms["BIN"]["cum_return_pct"] == pytest.approx(5.0)


@pytest.mark.unit
def test_apy_indicative_when_few_closed_weeks(tmp_path, monkeypatch):
    # 3 marks -> 2 closed weeks
    _mk_db(tmp_path / "resmom_shadow.db",
           {"A": [10_000.0, 10_100.0, 10_200.0]}, [], {"A": 3})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    card = pt._resmom_card(T0 + 3 * MS_WEEK)
    assert card["n"] == 2
    assert card["apy"] is not None                  # number is kept
    assert card["apy_indicative"] is True
    assert card["apy_n"] == 2


@pytest.mark.unit
def test_apy_not_indicative_with_enough_weeks(tmp_path, monkeypatch):
    # 13 weekly marks -> 12 closed weeks
    navs = [10_000.0 + 50.0 * i for i in range(13)]
    _mk_db(tmp_path / "resmom_shadow.db", {"A": navs}, [], {"A": 13})
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    card = pt._resmom_card(T0 + 14 * MS_WEEK)
    assert card["n"] == 12
    assert card["apy"] is not None
    assert card["apy_indicative"] is False
    assert card["apy_n"] == 12


@pytest.mark.unit
def test_mark_apy_reliability_threshold_and_no_apy():
    assert pt.APY_MIN_N == 10
    c = {"apy": -552.0, "n": 2}
    pt._mark_apy_reliability(c)
    assert c["apy"] == -552.0 and c["apy_indicative"] is True
    c = {"apy": 5.0, "n": 10}
    pt._mark_apy_reliability(c)
    assert c["apy_indicative"] is False
    c = {"apy": 5.0, "n": None}
    pt._mark_apy_reliability(c)
    assert c["apy_indicative"] is True and c["apy_n"] == 0
    c = {"apy": None, "n": 2}
    pt._mark_apy_reliability(c)
    assert "apy_indicative" not in c
