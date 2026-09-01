"""Generate STRATEGY_AUDIT.md from existing per_strategy CSV."""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.backtest_strategy_audit import classify_strategy, DISPLAY_NAMES

CSV_PATH = "data/backtests/per_strategy_20260625_202340.csv"
OUT_PATH = "docs/STRATEGY_AUDIT.md"

# Map CSV strategy names to display names
NAME_MAP = {
    "TrendFollow": "SmartMoneyFlow",
    "MeanReversion": "FundingExtreme",
}


def main() -> None:
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cls = r["strategy"]
            display = NAME_MAP.get(cls, cls)
            rows.append({
                "display_name": display,
                "class_name": cls,
                "window": r["window"],
                "n_trades": int(r["n_trades"]),
                "win_rate": float(r["win_rate"]),
                "profit_factor": float(r["profit_factor"]),
                "sharpe": float(r["sharpe"]),
                "expectancy_usd": float(r["expectancy"]),
                "max_dd_pct": float(r["max_dd_pct"]),
                "total_return_pct": 0.0,
                "total_pnl_usd": 0.0,
            })

    by_name: dict = defaultdict(list)
    for r in rows:
        by_name[r["display_name"]].append(r)

    lines = [
        "# Strategy Audit — Backtest Profundo",
        "",
        f"**Gerado:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Fonte:** `{CSV_PATH}` (backtest isolado por estratégia, 3 janelas)",
        "",
        "## Metodologia",
        "",
        "Cada estratégia corre **sozinha** (forced `enabled: true`) com os mesmos gates de risco",
        "que o live. Janelas:",
        "",
        "| Janela | Período | Nota |",
        "|--------|---------|------|",
        "| A_volatile_3d | 23–25 Jun 2026 | Pico de volatilidade |",
        "| B_2weeks | 11–25 Jun 2026 | 2 semanas recentes |",
        "| C_full | 18 Mai – 25 Jun 2026 | Máximo histórico na DB à data do run |",
        "",
        "Para re-correr com dados atualizados (até 29 Jun):",
        "```bash",
        "python scripts/backtest_strategy_audit.py --quick",
        "```",
        "",
        "## Veredicto final",
        "",
    ]

    verdicts = {}
    for name, rs in sorted(by_name.items()):
        verdicts[name] = classify_strategy(rs)

    for bucket, icon in [("KEEP", "✅"), ("WATCH", "⚠️"), ("KILL", "❌"), ("NO_DATA", "⏳")]:
        items = [(n, v) for n, v in verdicts.items() if v[0] == bucket]
        if not items:
            continue
        lines.append(f"### {icon} {bucket}")
        lines.append("")
        for name, (_, reason) in items:
            lines.append(f"- **{name}** — {reason}")
        lines.append("")

    lines.extend([
        "## Tabela C_full (decisão principal)",
        "",
        "| Estratégia | n | WR% | PF | Sharpe | Exp$ | DD% |",
        "|------------|---|-----|-----|--------|------|-----|",
    ])
    for name in sorted(by_name.keys()):
        full = next((r for r in by_name[name] if r["window"] == "C_full"), None)
        if not full:
            continue
        lines.append(
            f"| {name} | {full['n_trades']} | {full['win_rate']} | "
            f"{full['profit_factor']} | {full['sharpe']} | {full['expectancy_usd']} | "
            f"{full['max_dd_pct']} |"
        )

    lines.extend([
        "",
        "## Config recomendada (paper)",
        "",
        "Com base no backtest + live trades:",
        "",
        "```yaml",
        "strategy:",
        "  volatility_breakout:",
        "    enabled: true",
        "  vwap_deviation:",
        "    enabled: true",
        "  checklist_meta:",
        "    enabled: true          # Phase08 shadow (baseline FAIL — não execution)",
        "  phase08:",
        "    enabled: true",
        "    paper_only: true",
        "    execution_strategies: [VWAPDeviation]",
        "    shadow_strategies: [VolatilityBreakout, ChecklistMeta, ...]",
        "  trend_follow:          # SmartMoneyFlow",
        "    enabled: false",
        "  trend_pyramid:",
        "    enabled: false",
        "  donchian_breakout:",
        "    enabled: false",
        "  orderbook_scalper:",
        "    enabled: false",
        "  mean_reversion:          # FundingExtreme",
        "    enabled: false",
        "  funding_arbitrage:",
        "    enabled: false",
        "  lead_lag:",
        "    enabled: false         # 0 trades — precisa mais histórico perp",
        "  liquidation_catcher:",
        "    enabled: false         # 0 trades — precisa mais histórico liq",
        "  cvd_orderflow:",
        "    enabled: false         # 0 trades — buy/sell volume incompleto",
        "  range_grid:",
        "    enabled: false",
        "  funding_momentum:",
        "    enabled: false",
        "  spot_perp_carry:",
        "    enabled: false",
        "  ensemble:",
        "    enabled: false         # direct mode",
        "```",
        "",
        "## Dados em falta para re-testar",
        "",
        "- **LeadLag / LiquidationCatcher:** liquidations + binance perp só desde 19 Jun",
        "- **CVDOrderFlow:** buy/sell volume em ~12% dos candles 1m",
        "- **SpotPerpCarry:** sem Binance spot backfill",
        "- Correr `scripts/backfill_external_feeds.py` e `scripts/backfill_funding.py` antes do próximo audit",
        "",
    ])

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Wrote {OUT_PATH}")
    for name in sorted(verdicts.keys()):
        v, reason = verdicts[name]
        print(f"  {v:8s} {name:22s} — {reason}")


if __name__ == "__main__":
    main()
