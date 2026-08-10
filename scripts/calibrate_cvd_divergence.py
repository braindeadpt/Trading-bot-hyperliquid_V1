"""Measure CVD |divergence| distribution and calibrate CVDOrderFlow_p90 threshold."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import Database  # noqa: E402
from src.strategies.base import MarketEvent  # noqa: E402
from src.strategies.cvd_orderflow import CVDOrderFlow  # noqa: E402
from src.utils.config import load_config  # noqa: E402

OUT = ROOT / "data" / "backtests" / "parity_diag"


def _pctile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * p / 100.0))
    return ys[i]


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(snap if snap.exists() else ROOT / "data" / "live" / "bot.db"))

    probe = CVDOrderFlow(
        {
            "enabled": True,
            "min_divergence_strength": 0.0,
            "min_price_move_pct": 0.0,
            "min_volume_usd": 0.0,
            "require_oir_confirm": False,
            "min_adx": 0.0,
            "max_adx": 100.0,
            "signal_throttle_ms": 0,
        }
    )

    by_sym: Dict[str, List[float]] = {s: [] for s in symbols}
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - 90 * 86400 * 1000

    for sym in symbols:
        candles = db.get_candles(
            sym, "1m", limit=200_000, start_ms=start_ms, end_ms=end_ms
        )
        print(f"  {sym}: {len(candles)} 1m bars", flush=True)
        state = probe._get_state(sym)
        for c in candles:
            ev = MarketEvent(
                symbol=sym,
                price=float(c.close),
                timestamp_ms=int(c.timestamp_ms),
                candle_1m=c,
            )
            bar = probe._extract_bar(ev)
            if bar is None:
                continue
            if state.bars_1m and state.bars_1m[-1].timestamp_ms == bar.timestamp_ms:
                continue
            state.bars_1m.append(bar)
            if len(state.bars_1m) < probe.WINDOW_LONG:
                continue
            stats_m = probe._window_stats(state.bars_1m, probe.WINDOW_MED)
            if stats_m is None or abs(stats_m.divergence) <= 0:
                continue
            by_sym[sym].append(abs(stats_m.divergence))

    all_abs: List[float] = []
    for xs in by_sym.values():
        all_abs.extend(xs)

    def dist(xs: List[float]) -> Dict[str, Any]:
        return {
            "n": len(xs),
            "p50": _pctile(xs, 50),
            "p75": _pctile(xs, 75),
            "p90": _pctile(xs, 90),
            "p95": _pctile(xs, 95),
            "p99": _pctile(xs, 99),
            "max": max(xs) if xs else None,
        }

    p90 = _pctile(all_abs, 90)
    payload = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": 90,
        "method": (
            "Replay 1m candles through CVDOrderFlow._window_stats (medium=15). "
            "Collect |divergence| wherever divergence != 0. "
            "CVDOrderFlow_p90 threshold = aggregate p90."
        ),
        "legacy_threshold": 0.35,
        "aggregate": dist(all_abs),
        "by_symbol": {s: dist(xs) for s, xs in by_sym.items()},
        "chosen": {
            "variant": "CVDOrderFlow_p90",
            "percentile": 90,
            "min_divergence_strength": p90,
            "rationale": (
                "p90 of observed non-zero |div| — top decile of real divergence "
                "events. Legacy 0.35 sat above the live max (~0.34) and was untestable."
            ),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    jpath = OUT / "cvd_divergence_distribution.json"
    jpath.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md = [
        "# CVD divergence calibration → CVDOrderFlow_p90",
        "",
        f"Created: {payload['created_utc']}",
        "",
        f"Aggregate: `{json.dumps(payload['aggregate'])}`",
        "",
        f"Legacy thr **0.35** vs max **{payload['aggregate'].get('max')}**.",
        Chosen **0.275** (aggregate p90 of 118 573 non-zero medium-window |div| values).
Hist max=4.47; legacy 0.35 ≈ p93 of this distribution.

## Baseline gate result (FINAL)

| Fold | n | PF | B1 %ile | Verdict |
|------|--:|---:|--------:|---------|
| W2 | 53 | 0.805 | 86 | **FAIL** (B1 + not_profitable) |
| W3 | 71 | 0.374 | 86 | **FAIL** (B1 + not_profitable) |

The variant **fires** (powered sample) but **does not have edge**. Per protocol:
**stop** — do not start another percentile hunt. The CVD signal family fails the
three-condition gate; the old 0.35 threshold was not the root problem.
    print(json.dumps(payload["aggregate"], indent=2))
    print(f"chosen p90={p90}")
    print(f"Wrote {jpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
