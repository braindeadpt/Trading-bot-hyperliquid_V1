"""ResidualMomentum parity tests (prereg 2026-10-28 §Regra/§Nulos).

Two layers:

* ``test_signal_path_is_shared`` (unit, offline) — the daemon's
  descriptive Binance arm (``_binance_pass``) must produce exactly the
  weights that the bare signal pipeline produces on the same candles:
  same ``select_universe`` -> ``compute_signals`` -> ``build_portfolio``
  code path, no second implementation.

* ``test_binance_parity_golden_week`` (``network``, opt-in) — the same
  pipeline against REAL Binance USDT-M daily klines for the closed
  rebalance week 2026-10-19 (data through Sunday 2026-10-18 close).
  Weights are asserted equal to the golden recorded on 2026-10-28 when
  this test was generated. Daily klines for closed days are immutable,
  so the golden is deterministic.

  The golden weights were produced by THIS code, not by the 2026-10-27
  historical backtest artifact (that artifact lives on the offline Mac);
  parity vs the original backtest output is a separate pending check —
  see the post-cutover report.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from src.resmom_shadow import spec
from src.resmom_shadow.daemon import ResmomShadowDaemon
from src.resmom_shadow.signal import (build_portfolio, compute_signals,
                                      log_returns)
from src.resmom_shadow.universe import select_universe

# Candidate pool for the golden week — 35 liquid USDT-M perps, BTC kept
# in the ranking (legs exclude it downstream).
GOLDEN_WEEK_MS = int(
    dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc).timestamp() * 1000)

CANDIDATES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "TRXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "NEARUSDT", "UNIUSDT", "AAVEUSDT", "ETCUSDT", "FILUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT",
    "ATOMUSDT", "ALGOUSDT", "ICPUSDT", "HBARUSDT", "XLMUSDT",
    "RENDERUSDT", "ENAUSDT", "PENGUUSDT", "WIFUSDT", "JUPUSDT",
    "ONDOUSDT",
]

# Recorded 2026-10-28 from live Binance klines (interval=1d, cutoff
# 2026-10-19T00:00Z). 25-name universe (top-25 by 30d notional), legs =
# floor(0.2*25) = 5/side, beta-neutral gross split 0.45433/0.54567.
GOLDEN_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT", "DOGEUSDT",
    "SUIUSDT", "UNIUSDT", "ENAUSDT", "AVAXUSDT", "ADAUSDT", "LINKUSDT",
    "ONDOUSDT", "BCHUSDT", "FILUSDT", "AAVEUSDT", "LTCUSDT", "ARBUSDT",
    "HBARUSDT", "INJUSDT", "TIAUSDT", "PENGUUSDT", "XLMUSDT", "DOTUSDT",
    "APTUSDT",
]
GOLDEN_WEIGHTS = {
    "APTUSDT": 0.10726257498792992,
    "ETHUSDT": -0.17537755108726813,
    "FILUSDT": 0.09952869075648335,
    "INJUSDT": 0.09994022446437004,
    "LTCUSDT": -0.08917525116938967,
    "NEARUSDT": 0.0751344851889287,
    "SOLUSDT": -0.1344589836103119,
    "TIAUSDT": 0.0724643688595815,
    "UNIUSDT": -0.061325297560820405,
    "XLMUSDT": -0.08533257231491649,
}
GOLDEN_GROSS_L = 0.45433034425729346
GOLDEN_FLAGS: list = []


def _pipeline(candles, cutoff):
    """select_universe -> compute_signals -> build_portfolio (BTC legs
    excluded exactly as _arm_pass does)."""
    uni, _young = select_universe(candles, cutoff)
    syms = [e.symbol for e in uni]
    btc = "BTCUSDT"
    rets = {s: log_returns([c["c"] for c in candles.get(s, [])])
            for s in set(syms) | {btc}}
    r_btc = log_returns([c["c"] for c in candles.get(btc, [])])
    sigs = compute_signals(rets, r_btc)
    score = {k: v.s for k, v in sigs.items()}
    ranked = sorted((s for s in syms if s in score), key=lambda s: -score[s])
    legs = [s for s in ranked if s != btc]
    return uni, build_portfolio(legs, sigs, len(syms))


@pytest.mark.unit
def test_signal_path_is_shared(tmp_path):
    """Daemon BIN arm == bare pipeline on identical candles (offline)."""
    candles = {}
    for j, s in enumerate(CANDIDATES):
        rng = np.random.default_rng(1000 + j)
        path = [80.0 + j]
        for _ in range(129):
            path.append(path[-1] * (1.0 + float(rng.normal(0.0, 0.02))))
        candles[s] = [
            {"t": GOLDEN_WEEK_MS - (130 - i) * spec.MS_D,
             "o": p, "h": p, "l": p, "c": p, "v": 1e6 + j * 1e4}
            for i, p in enumerate(path)]

    d = ResmomShadowDaemon(db_path=str(tmp_path / "p.db"),
                           fetch={"meta": lambda: [],
                                  "c1d": lambda *a: [],
                                  "fill": lambda *a: None,
                                  "funding": lambda *a: [],
                                  "mids": lambda: {},
                                  "bn_meta": lambda: list(CANDIDATES),
                                  "bn_klines": lambda s, days, end: [
                                      c for c in candles.get(s, [])
                                      if c["t"] < end][-days:],
                                  "bn_funding": lambda *a: []})
    d._bn_fills = lambda syms, t: {}      # fills irrelevant to weights
    stats = d._binance_pass(GOLDEN_WEEK_MS, GOLDEN_WEEK_MS)
    row = d.ledger.rebalance_get(GOLDEN_WEEK_MS, "BIN")
    d.shutdown()
    assert stats.get("rebalance_id") and row is not None

    import json as _json
    uni, port = _pipeline(candles, GOLDEN_WEEK_MS)
    umeta = _json.loads(row["universe_json"])
    assert [m["sym"] for m in umeta] == [e.symbol for e in uni]
    got = _json.loads(row["weights_json"])
    assert got == {s: w for s, w in port.weights.items()}
    assert _json.loads(row["flags_json"]) == port.flags


@pytest.mark.network
def test_binance_parity_golden_week():
    """Same code vs real Binance USDT-M 1d klines, week 2026-10-19."""
    from src.resmom_shadow import prices

    cutoff = GOLDEN_WEEK_MS
    candles = {}
    for s in CANDIDATES:
        candles[s] = prices.binance_klines_1d(s, 130, cutoff)

    uni, port = _pipeline(candles, cutoff)
    assert [e.symbol for e in uni] == GOLDEN_UNIVERSE
    assert port.flags == GOLDEN_FLAGS
    assert port.gross_long == pytest.approx(GOLDEN_GROSS_L, rel=1e-9)
    assert set(port.weights) == set(GOLDEN_WEIGHTS)
    for s, w in GOLDEN_WEIGHTS.items():
        assert port.weights[s] == pytest.approx(w, rel=1e-9)
