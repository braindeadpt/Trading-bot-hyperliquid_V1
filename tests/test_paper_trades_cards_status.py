"""Card status honesty: ivgate stale/sealed, UnlockShort readout, FundCarry.

Synthetic data only (tmp_path DBs/JSON) - never touches real databases.
Presentation layer: src/research/paper_trades.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.research.paper_trades as pt

pytestmark = pytest.mark.unit

DAY = 86_400_000


def _ms(y, m, d) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


# -- ivgate ---------------------------------------------------------------

def _mk_research(tmp_path: Path, boards, ts: int) -> None:
    con = sqlite3.connect(str(tmp_path / "hyperliquid.db"))
    con.execute(
        "CREATE TABLE shadow_outcome_scoreboards (id INTEGER PRIMARY KEY,"
        " evaluated_at_ms INTEGER, strategy TEXT, since_ms INTEGER,"
        " until_ms INTEGER, candle_source TEXT, metrics_json TEXT,"
        " disclaimer TEXT)")
    for b in boards:
        con.execute(
            "INSERT INTO shadow_outcome_scoreboards (evaluated_at_ms,"
            " strategy, candle_source, metrics_json, disclaimer)"
            " VALUES (?,?,?,?,?)",
            (ts, b["strategy"], "x", json.dumps(b), ""))
    con.commit()
    con.close()


SEALED_BOARD = {
    "strategy": "VWAPDeviation", "variant": "iv_gate_confirm",
    "n_evaluated": 20, "n_independent": 13,
    "win_rate": 0.0, "net_hypothetical_pnl_pct": 0.0,
    "sealed": True, "confirmation_min_indep": 30,
    "confirmation_expired": False,
}
OPEN_BOARD = {
    "strategy": "VWAPDeviation", "variant": "iv_gate_confirm",
    "n_evaluated": 40, "n_independent": 35,
    "win_rate": 0.4, "net_hypothetical_pnl_pct": -1.5,
}


def test_ivgate_db_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    c = pt._ivgate_card(_ms(2026, 11, 22))
    assert c["state"] == "error"
    assert c["state_detail"] == "sem dados (research DB ausente)"


def test_ivgate_stale_board_is_explicit_with_date(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    _mk_research(tmp_path, [SEALED_BOARD], _ms(2026, 10, 8))
    c = pt._ivgate_card(_ms(2026, 11, 22))
    assert c["state"] == "stale"
    assert "08/10/2026" in c["state_detail"]
    assert c["state_detail"] != "sem sinal"
    assert c["data_ms"] == _ms(2026, 10, 8)


def test_ivgate_sealed_zeros_are_not_reported_as_results(tmp_path, monkeypatch):
    """Bug: sealed boards carry default 0.0 metrics (not computed). The
    card used to show WR 0% / net 0.00% next to n=13."""
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2026, 11, 22)
    _mk_research(tmp_path, [SEALED_BOARD], now - 3_600_000)
    c = pt._ivgate_card(now)
    assert c["n"] == 13 and c["n_readout"] == 30
    assert c["wr"] is None and c["net"] is None
    assert c["sealed"] is True
    assert c["state"] == "no_signal"
    assert "seladas" in c["state_detail"]


def test_ivgate_fresh_unsealed_board_shows_metrics(tmp_path, monkeypatch):
    """Board without a `sealed` key is NOT sealed (to_dict only emits it
    for confirmation boards); real metrics must show."""
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2026, 11, 22)
    _mk_research(tmp_path, [OPEN_BOARD], now - 3_600_000)
    c = pt._ivgate_card(now)
    assert c["sealed"] is False
    assert c["n"] == 35 and c["wr"] == 40.0 and c["net"] == -1.5


def test_ivgate_zero_independent_stays_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2026, 11, 22)
    b = dict(OPEN_BOARD, n_independent=0, n_evaluated=9)
    _mk_research(tmp_path, [b], now - 1000)
    assert pt._ivgate_card(now)["n"] == 0


# -- unlock ---------------------------------------------------------------

def _mk_unlock(tmp_path: Path, closes: list, hb_ms: int) -> None:
    """closes = list of priced_out_ms for closed events (explicit so tests
    can place events on either side of the readout deadline)."""
    con = sqlite3.connect(str(tmp_path / "unlock_shadow.db"))
    con.execute("CREATE TABLE unlock_sims (kind TEXT, state TEXT,"
                " net_usd REAL, gross_usd REAL, priced_out_ms INTEGER)")
    con.execute("CREATE TABLE unlock_snapshots (ts_ms INTEGER)")
    con.execute("INSERT INTO unlock_snapshots VALUES (?)", (hb_ms,))
    for t in closes:
        con.execute("INSERT INTO unlock_sims VALUES ('event','closed',1,1,?)",
                    (t,))
    con.commit()
    con.close()


def test_unlock_constants_match_ledger():
    from src.research import paper_ledger as pl
    row = pl._unlock_row(0)
    assert row["n_readout"] == pt._UNLOCK_N_READOUT
    assert row["readout_ms"] == pt._UNLOCK_READOUT_MS


def test_unlock_before_readout_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2026, 11, 22)
    _mk_unlock(tmp_path, [], now - 1000)
    c = pt._unlock_card(now)
    assert c["state"] == "no_signal" and "verdict" not in c


def test_unlock_readout_passed_below_min_is_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2027, 10, 15)
    # 3 events closed BEFORE the deadline -> 3/15 at expiry -> INCONCLUSIVO
    _mk_unlock(tmp_path, [pt._UNLOCK_READOUT_MS - i * DAY - 1
                          for i in range(3)], now - 1000)
    c = pt._unlock_card(now)
    assert c["verdict"] == "INCONCLUSIVO"
    assert c["state"] == "verdict"
    assert "14/10/2027" in c["state_detail"] and "3/15" in c["state_detail"]


def test_unlock_n_reached_does_not_invent_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2027, 3, 1)
    _mk_unlock(tmp_path, [now - 1000 - i * DAY for i in range(15)],
               now - 1000)
    c = pt._unlock_card(now)
    assert c["verdict"] == "por registar"
    assert "INCONCLUSIVO" not in c["state_detail"]


def test_unlock_late_close_after_deadline_keeps_inconclusivo(
        tmp_path, monkeypatch):
    """Prereg §6: read at n>=15 OR deadline, whichever FIRST. Once the
    deadline passes with <15 closed, INCONCLUSIVO is final — a 15th close
    after the deadline must not flip it to 'por registar'."""
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2027, 10, 15)
    closes = [pt._UNLOCK_READOUT_MS - i * DAY - 1 for i in range(14)]
    closes.append(pt._UNLOCK_READOUT_MS + DAY)      # 15th: after deadline
    _mk_unlock(tmp_path, closes, now - 1000)
    c = pt._unlock_card(now)
    assert c["n"] == 15                             # raw count still shown
    assert c["verdict"] == "INCONCLUSIVO"
    assert "14/15" in c["state_detail"]


def test_unlock_15_closed_before_deadline_is_due_after_deadline(
        tmp_path, monkeypatch):
    """The n>=15 trigger fired before the deadline — the readout stays
    'por registar' even when evaluated after the deadline."""
    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    now = _ms(2027, 10, 15)
    _mk_unlock(tmp_path, [pt._UNLOCK_READOUT_MS - i * DAY - 1
                          for i in range(15)], now - 1000)
    c = pt._unlock_card(now)
    assert c["verdict"] == "por registar"


# -- FundCarry ------------------------------------------------------------

def test_fundcarry_without_json(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "_FUNDCARRY_JSON", tmp_path / "nope.json")
    c = pt._fundcarry_card(0)
    assert c["kind"] == "historical"
    assert c["state"] == "testing"
    assert "a testar" in c["state_detail"]
    assert "não é paper" in c["prereg_state"]


def test_fundcarry_full_json(tmp_path, monkeypatch):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({
        "verdict": "PASS-provisorio: sobrevivencia nao controlada",
        "K": 8, "cum_return": 0.123, "sharpe": 1.25,
        "decomposition_usd": {"net": 1234.5},
        "sign_flip": {"p": 0.04, "n_pos": 6, "K": 8},
        "nulls": {"real_percentile": 97.5},
        "ex_top3": {"pnl_ex_top3_usd": 321.0},
        "max_drawdown": -0.08,
        "weekly": [{"date": "2026-01-05", "pnl_usd": 10.0},
                   {"date": "2026-01-12", "pnl_usd": -4.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(pt, "_FUNDCARRY_JSON", f)
    c = pt._fundcarry_card(0)
    h = c["hist"]
    assert c["state"] == "historical"
    assert c["badge"][1] == "PASS-provisório"
    assert h["verdict"] == "PASS-provisório"
    assert (h["windows_pos"], h["windows_k"]) == (6, 8)
    assert h["signflip_p"] == 0.04 and h["null_percentile"] == 97.5
    assert h["pnl_ex_top3_usd"] == 321.0 and h["sharpe"] == 1.25
    assert c["net"] == 1234.5 and c["net_unit"] == "usd"
    assert [v for _, v in c["equity"]] == [10.0, 6.0]
    assert pt._fundcarry_detail(0)["equity"] == c["equity"]


def test_fundcarry_detail_dd_matches_artifact_max_drawdown(
        tmp_path, monkeypatch):
    """The modal expects a drawdown series in percentage points; its
    minimum must equal the artifact's max_drawdown (compounded weekly
    returns, drop vs peak — the artifact's own convention)."""
    f = tmp_path / "r.json"
    f.write_text(json.dumps({
        "verdict": "KILL (C)", "max_drawdown": -0.24,
        "weekly": [{"date": "2026-01-05", "ret": 0.10, "pnl_usd": 1000.0},
                   {"date": "2026-01-12", "ret": -0.20, "pnl_usd": -2200.0},
                   {"date": "2026-01-19", "ret": -0.05, "pnl_usd": -440.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(pt, "_FUNDCARRY_JSON", f)
    d = pt._fundcarry_detail(0)
    assert d["dd"] != []
    assert [v for _, v in d["dd"]] == [0.0, -20.0, -24.0]
    assert min(v for _, v in d["dd"]) == pytest.approx(-0.24 * 100)


def test_fundcarry_partial_json_shows_none_not_crash(tmp_path, monkeypatch):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"verdict": "KILL (C)", "cum_return": -0.05}),
                 encoding="utf-8")
    monkeypatch.setattr(pt, "_FUNDCARRY_JSON", f)
    c = pt._fundcarry_card(0)
    assert c["badge"] == ["badge-red", "KILL (C)"]
    assert c["net"] == -5.0 and c["net_unit"] == "pct"
    assert c["hist"]["sharpe"] is None and c["hist"]["signflip_p"] is None
    assert c["equity"] == []


def test_fundcarry_corrupt_json(tmp_path, monkeypatch):
    f = tmp_path / "r.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pt, "_FUNDCARRY_JSON", f)
    assert pt._fundcarry_card(0)["state"] == "error"


def test_fundcarry_registered_in_payload():
    assert "fundcarry" in pt.STRATEGY_IDS
    assert "fundcarry" in pt._CARDS and "fundcarry" in pt._DETAILS


# -- closed section (template) -------------------------------------------

def test_template_has_generic_closed_section():
    html = (Path(pt.ROOT) / "src/dashboard/templates/index.html").read_text(
        encoding="utf-8")
    assert 'id="closed-section"' in html and "Encerradas" in html
    assert 'r.state === "closed"' in html and 'r.state !== "closed"' in html
    assert "jev" not in html.split("function renderClosed")[1].split(
        "function _fmtNet")[0].lower()
