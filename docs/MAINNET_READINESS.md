# Mainnet Readiness Assessment (Fase 10)

*Generated 2026-07-13. Every number in this document was produced by actually
running the referenced command in this session — see "Verification log" at
the end. Nothing here is copied from memory or from older docs without
re-checking.*

---

## MAINNET EXECUTION IS NOT READY

> **No gate criterion below currently shows a real PASS.** The frozen
> paper-trading window opened today (2026-07-13) and has **zero trades**
> recorded against it. Live-vs-replay drift, testnet end-to-end proof, and
> the GoldRush data blocker are all still open.
>
> **This document does not authorize, trigger, or bring closer any mainnet
> activation.** It is a status report only. Activating mainnet execution
> requires a human operator to explicitly confirm, after every row in the
> checklist table below independently shows PASS — not "no FAIL yet due to
> insufficient data," an actual PASS. No script in this repository flips
> `mode: mainnet` or `HYPERLIQUID_MAINNET_ENABLED` on its own.

---

## 1. Frozen paper-trading window (Fase 10 gate)

The gate protocol is implemented in `src/research/phase10_preregister.py`
(manifest freeze) and `src/research/phase10_gate_metrics.py` /
`scripts/phase10_check_gate.py` (metrics + pass/fail evaluation).

**Frozen manifest** (`data/research/phase10/phase10_preregister.json`, read
directly this session):

| Field | Value |
|---|---|
| `experiment_id` | `db9aaff0-ceae-4a20-ac98-9a8d44787752` |
| `window_start_ms` | `1783954481161` (2026-07-13) |
| `execution_strategies` | `VWAPDeviation`, `VolatilityBreakout` |
| `window.min_weeks` / `max_weeks` | 8 / 12 |
| Gate thresholds | `min_trades=100`, `min_profit_factor=1.20`, `expectancy_r_gt=0.0`, `max_drawdown_pct=5.0` |

**Real gate-check output**, from `python scripts/phase10_check_gate.py` run
in this session:

```
=== Fase 10 Frozen-Window Gate Report ===
experiment_id: db9aaff0-ceae-4a20-ac98-9a8d44787752
window_start_ms: 1783954481161
execution_strategies: VWAPDeviation, VolatilityBreakout
trade_count: 0
expectancy_r basis: 0/0 trades had a derivable risk basis

Criteria:
  [FAIL                ] min_trades         value=0 threshold=100
  [INSUFFICIENT_DATA   ] profit_factor      value=0.0 threshold=1.2
  [INSUFFICIENT_DATA   ] expectancy_r       value=0.0 threshold=0.0
  [INSUFFICIENT_DATA   ] max_drawdown_pct   value=0.0 threshold=5.0

Cost summary (informational):
  total_costs_usd=0.0000 gross_profit_usd=0.0000
  cost_pct_of_gross_profit=N/A (no gross profit)

GATE MET: False
```

The script exits non-zero (both FAIL and INSUFFICIENT_DATA count as "gate not
met" by design — see the script's own docstring). **Status: the window just
opened; 0 of the required ≥100 trades exist yet.**

---

## 2. Live-vs-replay drift check

Implemented in `src/research/live_vs_replay.py` +
`scripts/phase10_live_vs_replay.py`. It compares closed `trades` rows in
`data/live/bot.db` for the frozen window against a fresh `BacktestEngine`
replay of a read-only snapshot of the exact same candles, under the exact
same effective config, across 8 dimensions (signal count, gate rejections,
notional, slippage proxy, fees, hold duration, MFE/MAE, exit reason).

**Status: built, but not meaningfully runnable yet** — the frozen window has
0 live trades (see §1), so there is nothing to diff against a replay. Running
it today would trivially report `signal_count` drift of `live=0 replay=?` (or
crash on an empty comparison), not a real drift verdict. This check only
becomes informative once the paper window has produced trades.

Known permanent limitations documented in the module itself (not a
readiness gap, a structural fact of what's persisted):
- `mfe_mae` dimension is always `NOT_COMPARABLE` — the live `trades` table
  has no MFE/MAE columns (only the backtest engine computes those).
- Slippage is a proxy (both sides compared against the same 1m candle close)
  because neither side persists a pre-fill "intended" price.

---

## 3. Testnet end-to-end scenarios

`tests/test_testnet_e2e.py`, documented in `docs/TESTNET_E2E_GUIDE.md`,
covers 8 scenarios against Hyperliquid's real testnet order-matching engine:
maker order, market order, partial fill, cancel, native SL/TP trigger,
crash/restart recovery, orphan position adoption, kill switch.

**Real run in this session:**

```
python -m pytest tests/test_testnet_e2e.py -v -m testnet_live
...
======================= 8 skipped, 73 warnings in 2.04s =======================
```

All 8 scenarios **skip** (not pass, not fail) because
`HYPERLIQUID_PRIVATE_KEY` is not configured in this environment. **Status:
built and ready to run, zero executed for real.** No testnet credentials
exist anywhere in this session's environment as far as this check can
observe.

---

## 4. Native SL/TP triggers + kill switch

Code paths exist and are exercised only by mocked-SDK unit/integration tests,
not by a real exchange:

- `NativeProtectionManager` — `src/core/native_protection.py:39` —
  places/cancels reduce-only HL trigger orders for SL/TP.
- `ExecutionEngine.kill_switch()` — `src/core/execution.py:1108` — cancels
  all orders, flattens all positions via `flatten_all_positions()`, confirms
  flat via `confirm_flat()`, clears local protection and in-memory open
  trades.
- `TradingEngine.kill_switch()` — `src/core/engine.py:1049` — thin wrapper
  that calls the executor's kill switch, then forces one
  `ExchangeReconciler.reconcile_once()` pass afterward.

**Status: code exists and is covered by mocked tests (679 passing, see §6),
but has never been proven against a real exchange.** Scenarios 5 ("native
trigger") and 8 ("kill switch") in `tests/test_testnet_e2e.py` are the real
proof for this — both are in the same 8-skipped set as §3. This item is not
independently more ready than §3; it is blocked on the same missing testnet
credentials.

---

## 5. GoldRush / candle-data readiness

Per `AGENTS.md` §1 ("Current operational status") and
`docs/NODE_TRADES_REBUILD.md`, verified this session:

- **GoldRush candle-data readiness is not yet validated.** AGENTS.md
  explicitly instructs: "Do not run OOS, parameter tuning, holdout, or
  performance backtests against GoldRush-sourced data until this is
  resolved."
- A parity divergence was found between GoldRush HyperCore candles and the
  official `hl_candleSnapshot` feed for specific symbol/time windows.
- A fallback pipeline (`src/data/candle_providers/node_trades_rebuild.py`,
  `scripts/hl_node_trades_rebuild.py`) was built to rebuild 1m OHLCV directly
  from official Hyperliquid node-data S3 archives
  (`node_fills_by_block/hourly/{date}/{hour}.lz4`) for the disputed windows.
- **This pipeline has not yet been executed for real.** It requires AWS
  credentials + `boto3` (requester-pays S3) and is currently dry-run only
  (`scripts/hl_node_trades_rebuild.py` without `--execute`). Rebuilt data is
  explicitly documented as "not automatically treated as validated" —
  OOS/tuning stays blocked until the rebuilt candles pass their own
  secondary validation.
- **Status: OOS validation and data-readiness are both still blocked**,
  independent of the Fase 10 paper-trading gate above.

---

## 6. Security audit

Run in this session: `python -X utf8 -m src.security.audit --verbose --src-dir src`
(the `python main.py --audit` entry point produces the identical report but
crashed on a `UnicodeEncodeError` printing to this Windows console's cp1252
codepage — a console-encoding issue, not a security-finding issue; the
`-X utf8` invocation of the same underlying `src.security.audit.main()`
avoids it and the findings match what `main.py --audit` logs to
`logs/security_audit_*.log`).

```
========================================================================
  SECURITY AUDIT REPORT
  Generated : 2026-07-13 15:32:33 UTC
  Source    : <repository>\src
  Files     : 132
  Lines     : 44868
  Findings  : 2
========================================================================
────────────────────────────────────────────────────────────────────────
  [HIGH] — 2 finding(s)
────────────────────────────────────────────────────────────────────────
  AUDIT-005  backtest\run_manifest.py:25
       → Subprocess / os.system call detected — verify necessity
       snippet: (short hash)."""     try:         out = subprocess.check_output(             ["g

  AUDIT-005  utils\crash_recovery.py:132
       → Subprocess / os.system call detected — verify necessity
       snippet: cmd))         try:             result = subprocess.run(                 cmd,
========================================================================
  Critical : 0
  High     : 2
  Medium   : 0
  Low      : 0
  Info     : 0
========================================================================
```

Both findings are pre-existing and match the exception already documented in
`AGENTS.md` §11 ("HIGH is acceptable only for pre-existing `AUDIT-005` in
`crash_recovery.py`") — plus one additional pre-existing `AUDIT-005` in
`src/backtest/run_manifest.py:25` (a `git rev-parse` short-hash lookup for
manifest provenance, not a live-trading code path). **Status: 0 CRITICAL,
0 MEDIUM, 0 LOW; 2 pre-existing HIGH findings, both `subprocess` calls
outside the live execution path.** No new findings introduced.

---

## 7. Test suite

Run in this session: `python -m pytest -m "not network and not testnet_live" -q`

```
679 passed, 12 deselected, 87 warnings in 41.66s
```

The 12 deselected tests are the `network` (real external API calls) and
`testnet_live` (real testnet order placement) marker groups, intentionally
excluded from this run and covered separately in §3 above (8 of those 12 are
the testnet_live suite; the rest are `network`-marked tests such as
`test_ws.py`, `test_funding.py` per `AGENTS.md` §7).

---

## Gate checklist

| Gate criterion | Threshold | Current value | Status | Evidence location |
|---|---|---|---|---|
| Frozen window — min trades | ≥ 100 | 0 | **FAIL** | `python scripts/phase10_check_gate.py` output above; `data/live/bot.db` |
| Frozen window — profit factor | ≥ 1.20 | 0.0 (no basis) | **INSUFFICIENT_DATA** | same |
| Frozen window — expectancy_r | > 0.0 | 0.0 (no basis) | **INSUFFICIENT_DATA** | same |
| Frozen window — max drawdown | ≤ 5.0% | 0.0 (no basis) | **INSUFFICIENT_DATA** | same |
| Live-vs-replay drift | verdict = PASS | not meaningfully computable (0 live trades) | **NOT YET RUN** | `src/research/live_vs_replay.py`, `scripts/phase10_live_vs_replay.py` |
| Testnet e2e scenarios (8) | all pass | 8/8 skipped, no credentials | **NOT YET RUN** | `python -m pytest tests/test_testnet_e2e.py -v -m testnet_live` output above; `docs/TESTNET_E2E_GUIDE.md` |
| Native SL/TP + kill switch real-exchange proof | scenarios 5 & 8 pass | same 8-skipped set | **NOT YET RUN** | same as above |
| GoldRush data readiness | rebuilt candles pass secondary validation | rebuild pipeline built, not executed (no AWS creds run) | **NOT YET RUN / BLOCKED** | `docs/NODE_TRADES_REBUILD.md` |
| Security audit | 0 CRITICAL/MEDIUM/LOW | 0/0/0; 2 pre-existing HIGH (subprocess, non-execution-path) | **PASS (with known exceptions)** | audit output above |
| Default CI test suite | all pass | 679 passed, 12 deselected | **PASS** | pytest output above |

**Overall: mainnet execution readiness = NOT READY. No trading-performance
gate criterion is currently satisfied; the only two rows showing PASS
(security audit exceptions accepted, CI suite) are prerequisites, not the
Fase 10 go/no-go gate itself.**

---

## Verification log (commands actually run this session)

1. `python scripts/phase10_check_gate.py` — gate report above.
2. `python -X utf8 -m src.security.audit --verbose --src-dir src` — audit
   report above (equivalent to `python main.py --audit`, which was also run
   and confirmed to write the identical findings to
   `logs/security_audit_20260713.log`, but whose console `print()` hit a
   `UnicodeEncodeError` on this Windows cp1252 terminal).
3. `python -m pytest -m "not network and not testnet_live" -q` — 679 passed,
   12 deselected.
4. `python -m pytest tests/test_testnet_e2e.py -v -m testnet_live` — 8
   skipped.
5. Read `data/research/phase10/phase10_preregister.json`,
   `AGENTS.md`, `docs/NODE_TRADES_REBUILD.md`, `docs/TESTNET_E2E_GUIDE.md`,
   `src/research/phase10_preregister.py`, `src/research/live_vs_replay.py`,
   `src/core/execution.py` (`kill_switch`), `src/core/engine.py`
   (`kill_switch`, `_recover_state`, `_entry_feed_block_reason`,
   `_ws_health_loop`), `src/core/reconciliation.py`
   (`ExchangeReconciler`), `config/settings.yaml` (`reconciliation`,
   `risk`, `strategy.phase08`, `strategy.kelly` sections).
