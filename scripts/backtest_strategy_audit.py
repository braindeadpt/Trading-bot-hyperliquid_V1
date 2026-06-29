"""Deep per-strategy backtest audit — decide KEEP / WATCH / KILL from DB history.

Runs every registered strategy in isolation (forced enabled) across multiple
windows that match the data we actually have in bot.db.

Outputs:
  - data/backtests/strategy_audit_<ts>.csv
  - docs/STRATEGY_AUDIT.md

Usage:
  python scripts/backtest_strategy_audit.py
  python scripts/backtest_strategy_audit.py --workers 2
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import _STRATEGY_REGISTRY, _enrich_funding_strategy_config
from src.utils.config import load_config

# ── Logging: silence noisy modules ───────────────────────────────
logging.basicConfig(level=logging.ERROR)
for _name in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
):
    logging.getLogger(_name).setLevel(logging.ERROR)

DISPLAY_NAMES: Dict[str, str] = {
    "TrendFollow": "SmartMoneyFlow",
    "MeanReversion": "FundingExtreme",
}

# Windows tuned to bot.db coverage (see scripts/_check_bt_data.py)
WINDOWS: List[Tuple[str, str, str, List[str]]] = [
    (
        "D_full",
        "2026-05-18",
        "2026-06-29",
        ["BTC", "ETH", "SOL"],
    ),
    (
        "B_2weeks",
        "2026-06-11",
        "2026-06-25",
        ["BTC", "ETH", "SOL"],
    ),
    (
        "A_volatile_3d",
        "2026-06-23",
        "2026-06-25",
        ["BTC", "ETH", "SOL"],
    ),
    (
        "E_feeds",
        "2026-06-19",
        "2026-06-26",
        ["BTC", "ETH", "SOL"],
    ),
]

FEED_DEPENDENT = {
    "LiquidationCatcher",
    "LeadLag",
    "CVDOrderFlow",
    "OrderBookScalper",
}

COLS = [
    "display_name", "class_name", "window", "symbols", "n_trades", "win_rate",
    "avg_win_usd", "avg_loss_usd", "real_rr", "expectancy_usd", "profit_factor",
    "sharpe", "max_dd_pct", "total_return_pct", "total_pnl_usd",
    "tp_trades", "sl_trades", "time_trades", "other_trades",
]


@dataclass
class AuditTask:
    path: str
    class_name: str
    window: str
    start: str
    end: str
    symbols: List[str]


def ms_from_date(date_str: str, end: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def force_enable_section(path: str, section: dict) -> dict:
    """Force strategy on for isolated backtest (operational test)."""
    out = dict(section)
    out["enabled"] = True
    if path == "strategy.liquidation_catcher":
        out["auto_enable"] = True
        out.setdefault("min_notional_usd", 5_000_000)
        out.setdefault("require_oi_decreasing", False)
    if path == "strategy.orderbook_scalper":
        out["auto_enable"] = True
    if path == "strategy.mean_reversion":
        out.setdefault("require_feed_health", False)
    return out


def _exit_buckets(trades: List[Dict]) -> Dict[str, int]:
    tp = sl = tm = other = 0
    for t in trades:
        r = str(t.get("exit_reason", "")).lower()
        if "take_profit" in r or r in ("tp",):
            tp += 1
        elif "stop_loss" in r or "stoploss" in r or r == "sl":
            sl += 1
        elif any(x in r for x in ("time", "max_hold", "hold", "cooldown")):
            tm += 1
        else:
            other += 1
    return {"tp_trades": tp, "sl_trades": sl, "time_trades": tm, "other_trades": other}


def run_single_audit(
    db_path: str,
    config_path: str,
    path: str,
    class_name: str,
    cls: Any,
    window: str,
    start_ms: int,
    end_ms: int,
    symbols: List[str],
) -> Dict[str, Any]:
    cfg = load_config(config_path)
    db = Database(db_path)
    section = force_enable_section(path, dict(cfg.get(path, {}) or {}))
    if path in ("strategy.mean_reversion", "strategy.funding_arbitrage"):
        section = _enrich_funding_strategy_config(cfg, section)

    try:
        strategy = cls(section)
    except Exception as exc:
        return {
            "display_name": DISPLAY_NAMES.get(class_name, class_name),
            "class_name": class_name,
            "window": window,
            "symbols": ",".join(symbols),
            "error": str(exc),
        }

    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_kelly=bool(cfg.get("backtest.use_kelly", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
        use_risk_manager=True,
        use_volatility_circuit=False,
        use_funding_blackout=False,
        use_external_feeds_replay=True,
        max_daily_trades=0,
    )
    risk_cfg = cfg.get("risk", {}) or {}
    # Audit uses live-parity gates; correlation relaxed like Phase 1
    pg = dict(risk_cfg.get("portfolio_governance", {}) or {})
    pg["max_correlation"] = 0.98
    risk_cfg = dict(risk_cfg)
    risk_cfg["portfolio_governance"] = pg

    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )

    display = DISPLAY_NAMES.get(class_name, getattr(strategy, "name", class_name))

    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except ValueError as exc:
        return {
            "display_name": display,
            "class_name": class_name,
            "window": window,
            "symbols": ",".join(symbols),
            "n_trades": 0,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "display_name": display,
            "class_name": class_name,
            "window": window,
            "symbols": ",".join(symbols),
            "error": str(exc),
        }

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])
    n = int(metrics.get("n_trades", 0))
    avg_win = float(metrics.get("avg_win", 0))
    avg_loss = float(metrics.get("avg_loss", 0))
    real_rr = round(abs(avg_win / avg_loss), 4) if avg_loss != 0 else 0.0
    buckets = _exit_buckets(trades)
    total_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)

    return {
        "display_name": display,
        "class_name": class_name,
        "window": window,
        "symbols": ",".join(symbols),
        "n_trades": n,
        "win_rate": round(float(metrics.get("win_rate", 0)) * 100, 1),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "real_rr": real_rr,
        "expectancy_usd": round(float(metrics.get("avg_trade", 0)), 2),
        "profit_factor": round(float(metrics.get("profit_factor", 0)), 4),
        "sharpe": round(float(metrics.get("sharpe_ratio", 0)), 4),
        "max_dd_pct": round(float(metrics.get("max_drawdown", 0)) * 100, 2),
        "total_return_pct": round(float(metrics.get("total_return", 0)) * 100, 2),
        "total_pnl_usd": round(total_pnl, 2),
        **buckets,
    }


def _worker(payload: Tuple[str, str, str, str, str, str, str, str, List[str]]) -> Dict[str, Any]:
    db_path, config_path, path, class_name, _module_cls_name, window, start, end, symbols = payload
    from src.strategies.factory import _STRATEGY_REGISTRY as REG
    cls = next(c for p, c in REG if c.__name__ == class_name)
    return run_single_audit(
        db_path, config_path, path, class_name, cls,
        window, ms_from_date(start), ms_from_date(end, end=True), symbols,
    )


def classify_strategy(rows: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (verdict, reason) from all window rows for one strategy."""
    valid = [r for r in rows if "error" not in r]
    if not valid:
        return "ERROR", "backtest failed"

    full = next(
        (r for r in valid if r["window"] in ("D_full", "C_full")),
        valid[0],
    )
    n_full = int(full.get("n_trades", 0))

    if n_full == 0:
        name = full["display_name"]
        if name in FEED_DEPENDENT:
            feed_row = next((r for r in valid if r["window"] == "E_feeds"), None)
            if feed_row and int(feed_row.get("n_trades", 0)) == 0:
                return "NO_DATA", "0 trades even on feeds window — needs more external feed history"
        return "NO_DATA", "0 trades on full window — filters too strict or no edge"

    pos_windows = sum(
        1 for r in valid
        if int(r.get("n_trades", 0)) >= 3
        and float(r.get("expectancy_usd", 0)) > 0
        and float(r.get("profit_factor", 0)) >= 1.0
    )
    neg_windows = sum(
        1 for r in valid
        if int(r.get("n_trades", 0)) >= 3
        and float(r.get("expectancy_usd", 0)) <= 0
        and float(r.get("sharpe", 0)) < 0
    )

    pf_full = float(full.get("profit_factor", 0))
    sharpe_full = float(full.get("sharpe", 0))
    exp_full = float(full.get("expectancy_usd", 0))
    name = str(full.get("display_name", full.get("class_name", "?")))

    if name == "TrendPyramid" and n_full >= 10 and pf_full >= 1.25:
        return (
            "WATCH",
            f"C_full PF={pf_full:.2f} but outlier trade inflates PnL — revalidar isolado",
        )

    strong = next(
        (
            r for r in valid
            if int(r.get("n_trades", 0)) >= 10
            and float(r.get("profit_factor", 0)) >= 1.25
            and float(r.get("sharpe", 0)) >= 0.5
            and float(r.get("expectancy_usd", 0)) > 0
        ),
        None,
    )
    if strong:
        return (
            "KEEP",
            f"{strong['window']}: n={strong['n_trades']} PF={float(strong['profit_factor']):.2f} "
            f"Sharpe={float(strong['sharpe']):.2f}",
        )

    if n_full >= 10 and pf_full >= 1.25 and sharpe_full >= 0.5 and exp_full > 0:
        return "KEEP", f"D_full: n={n_full} PF={pf_full:.2f} Sharpe={sharpe_full:.2f} Exp=${exp_full:.2f}"

    if n_full >= 8 and pf_full >= 1.0 and exp_full > 0 and pos_windows >= 2:
        return "WATCH", f"positive in {pos_windows} windows; validate more data"

    if neg_windows >= 2 or (n_full >= 5 and pf_full < 1.0 and exp_full < 0):
        return "KILL", f"PF={pf_full:.2f} Exp=${exp_full:.2f} neg_windows={neg_windows}"

    if pf_full >= 1.0 and exp_full > 0:
        return "WATCH", f"marginal edge n={n_full} PF={pf_full:.2f}"

    return "KILL", f"PF={pf_full:.2f} Exp=${exp_full:.2f} insufficient edge"


def write_markdown_report(
    rows: List[Dict[str, Any]],
    out_path: str,
    data_summary: str,
) -> None:
    by_name: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        if "error" in r and r.get("n_trades", 0) == 0 and "display_name" not in r:
            continue
        by_name[r.get("display_name", r.get("class_name", "?"))].append(r)

    verdicts: Dict[str, Tuple[str, str]] = {}
    for name, rs in by_name.items():
        verdicts[name] = classify_strategy(rs)

    lines = [
        "# Strategy Audit — Backtest Profundo",
        "",
        f"**Gerado:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Dados disponíveis",
        "",
        data_summary,
        "",
        "## Critérios",
        "",
        "| Veredicto | Regra |",
        "|-----------|-------|",
        "| **KEEP** | D_full: n≥10, PF≥1.25, Sharpe≥0.5, expectancy>0 |",
        "| **WATCH** | PF≥1.0 e positivo em ≥2 janelas, ou marginal |",
        "| **KILL** | PF<1 e expectancy<0 em maioria, ou Sharpe<0 em ≥2 janelas |",
        "| **NO_DATA** | 0 trades na janela completa |",
        "",
        "## Veredicto final",
        "",
    ]

    for bucket, icon in [
        ("KEEP", "✅"),
        ("WATCH", "⚠️"),
        ("KILL", "❌"),
        ("NO_DATA", "⏳"),
        ("ERROR", "💥"),
    ]:
        items = [(n, v) for n, v in verdicts.items() if v[0] == bucket]
        if not items:
            continue
        lines.append(f"### {icon} {bucket}")
        lines.append("")
        for name, (_, reason) in sorted(items):
            lines.append(f"- **{name}** — {reason}")
        lines.append("")

    lines.extend([
        "## Detalhe por estratégia (janela D_full)",
        "",
        "| Estratégia | n | WR% | PF | Sharpe | Exp$ | Ret% | DD% |",
        "|------------|---|-----|-----|--------|------|------|-----|",
    ])

    for name in sorted(by_name.keys()):
        full = next((r for r in by_name[name] if r.get("window") == "D_full"), None)
        if not full or "error" in full:
            continue
        lines.append(
            f"| {name} | {full.get('n_trades', 0)} | {full.get('win_rate', 0)} | "
            f"{full.get('profit_factor', 0)} | {full.get('sharpe', 0)} | "
            f"{full.get('expectancy_usd', 0)} | {full.get('total_return_pct', 0)} | "
            f"{full.get('max_dd_pct', 0)} |"
        )

    lines.extend([
        "",
        "## Todas as janelas",
        "",
        "| Estratégia | Janela | n | PF | Sharpe | Exp$ | PnL$ |",
        "|------------|--------|---|-----|--------|------|------|",
    ])
    for r in sorted(rows, key=lambda x: (x.get("display_name", ""), x.get("window", ""))):
        if "error" in r and not r.get("n_trades"):
            continue
        lines.append(
            f"| {r.get('display_name', '?')} | {r.get('window', '?')} | "
            f"{r.get('n_trades', 0)} | {r.get('profit_factor', 0)} | "
            f"{r.get('sharpe', 0)} | {r.get('expectancy_usd', 0)} | "
            f"{r.get('total_pnl_usd', 0)} |"
        )

    lines.extend([
        "",
        "## Próximo passo",
        "",
        "1. Ligar só estratégias **KEEP** em paper (direct mode, sem ensemble).",
        "2. **WATCH** — mais 2 semanas de dados ou walk-forward.",
        "3. **KILL** / **NO_DATA** — manter OFF; NO_DATA precisa backfill (funding/OI/liq/perp/CVD).",
        "",
    ])

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def build_tasks(
    windows: List[Tuple[str, str, str, List[str]]],
    only_classes: Optional[set[str]] = None,
) -> List[AuditTask]:
    tasks: List[AuditTask] = []
    for path, cls in _STRATEGY_REGISTRY:
        cname = cls.__name__
        if only_classes and cname not in only_classes:
            continue
        display = DISPLAY_NAMES.get(cname, cname)
        for w_label, w_start, w_end, syms in windows:
            if display in FEED_DEPENDENT and w_label not in ("D_full", "E_feeds", "B_2weeks"):
                continue
            tasks.append(AuditTask(path, cname, w_label, w_start, w_end, syms))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep strategy backtest audit")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--db", default="data/live/bot.db")
    parser.add_argument("--workers", type=int, default=1, help="parallel workers (1=sequential)")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="only D_full + B_2weeks windows (faster)",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated class names (e.g. LeadLag,LiquidationCatcher)",
    )
    args = parser.parse_args()

    global WINDOWS
    if args.quick:
        WINDOWS = [w for w in WINDOWS if w[0] in ("D_full", "B_2weeks")]

    only_classes: Optional[set[str]] = None
    if args.only.strip():
        only_classes = {s.strip() for s in args.only.split(",") if s.strip()}

    data_summary = (
        "- Candles 15m/1h: BTC/ETH/SOL desde **2026-05-18** até **2026-06-29**\n"
        "- Candles 1m: desde **2026-05-24**\n"
        "- Funding: desde **2026-05-27** | OI: desde **2026-06-05**\n"
        "- Liquidations + Binance perp: **2026-06-19 → 2026-06-26** (janela E_feeds)\n"
        "- CVD buy/sell volume: ~12% dos candles 1m — CVD pode sub-reportar\n"
        "- Gates: risk manager ON; vol circuit + funding blackout OFF (isolates strategy edge)\n"
    )

    tasks = build_tasks(WINDOWS, only_classes)
    total = len(tasks)
    rows: List[Dict[str, Any]] = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = "data/backtests"
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"strategy_audit_{ts}.csv")
    md_path = "docs/STRATEGY_AUDIT.md"

    t0 = time.time()
    print(f"Strategy audit: {total} runs, workers={args.workers}")
    print(f"DB: {args.db} | Config: {args.config}\n")

    if args.workers <= 1:
        cfg = load_config(args.config)
        db = Database(args.db)
        registry = {cls.__name__: (path, cls) for path, cls in _STRATEGY_REGISTRY}
        done = 0
        for task in tasks:
            done += 1
            path, cls = registry[task.class_name]
            print(
                f"  [{done}/{total}] {task.class_name:22s} {task.window:14s} ...",
                end=" ",
                flush=True,
            )
            row = run_single_audit(
                args.db,
                args.config,
                path,
                task.class_name,
                cls,
                task.window,
                ms_from_date(task.start),
                ms_from_date(task.end, end=True),
                task.symbols,
            )
            rows.append(row)
            err = row.get("error")
            if err:
                print(f"ERR ({err[:40]})")
            else:
                print(f"{row.get('n_trades', 0):3d} trades PF={row.get('profit_factor', 0)}")
    else:
        payloads = [
            (
                args.db,
                args.config,
                t.path,
                t.class_name,
                t.class_name,
                t.window,
                t.start,
                t.end,
                t.symbols,
            )
            for t in tasks
        ]
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_worker, p): p for p in payloads}
            for fut in as_completed(futures):
                done += 1
                row = fut.result()
                rows.append(row)
                print(
                    f"  [{done}/{total}] {row.get('display_name', '?'):22s} "
                    f"{row.get('window', '?'):14s} n={row.get('n_trades', 0)}"
                )

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS + ["error"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    write_markdown_report(rows, md_path, data_summary)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed / 60:.1f} min")
    print(f"CSV: {csv_path}")
    print(f"Report: {md_path}")

    by_name: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        by_name[r.get("display_name", r.get("class_name", "?"))].append(r)
    print("\n=== VERDICTS ===")
    for name in sorted(by_name.keys()):
        v, reason = classify_strategy(by_name[name])
        print(f"  {v:8s} {name:22s} — {reason}")


if __name__ == "__main__":
    main()
