# Phase 03 — Mainnet Readiness Report (Exchange Truth + Native SL/TP)

**Date:** 2026-07-10  
**Scope:** Fase 03 — capital-safe testnet/mainnet execution path  
**Mainnet status:** **BLOCKED** (by design until Fase 10 canary)

---

## Delivered

| Item | Status |
|------|--------|
| Reconciliation via `user_state` + open orders + fills parsing | Done |
| Configurable orphan/mismatch policies | Done |
| Native reduce-only SL/TP on entry fill | Done |
| Idempotent trigger cancel/replace | Done |
| Software stop as redundancy (not primary) | Done |
| Kill switch (cancel → flatten → confirm flat) | Done |
| Entry block when reconciliation stale/failing | Done |
| `ORDER_SUBMISSION_UNKNOWN` non-terminal state | Done |
| Unified live fill path via `apply_entry_fill` | Done |
| `applied_fill_size` persistence | Done |
| Trigger close: exact-once PnL from fill tape (`trigger_reconcile.py`) | Done |
| No fill tape → `close_pending_reconciliation` (no silent estimate) | Done |
| Sibling trigger cancel after SL/TP + residual purge on restart | Done |
| Mainnet orphan policy `HALT`; testnet `ADOPT_AND_PROTECT` | Done |

---

## Configuration

```yaml
reconciliation:
  enabled: true
  interval_sec: 60
  stale_threshold_sec: 120
  orphan_exchange_policy: ADOPT_AND_PROTECT   # default; overridden per mode
  mismatch_policy: HALT
  block_entries_when_stale: true

mode_overrides:
  mainnet:
    reconciliation:
      orphan_exchange_policy: HALT
  testnet:
    reconciliation:
      orphan_exchange_policy: ADOPT_AND_PROTECT

execution:
  native_protection:
    enabled: true
    software_stop_redundancy: true
```

Paper remains default. Mainnet requires `HYPERLIQUID_MAINNET_ENABLED=1` **and** `exchange.mainnet_enabled=true` (unchanged). `flatten_on_stop` does **not** substitute for either flag.

---

## Validation Results (2026-07-10)

| Suite | Command | Result |
|-------|---------|--------|
| Phase 03 behavioural | `python tests/test_exchange_reconciliation.py` | **19/19 PASS** |
| Phase 01 fail-closed | `python tests/test_execution_fail_closed.py` | **9/9 PASS** |
| Phase 02 OMS | `python tests/test_execution_oms.py` | **12/12 PASS** |
| CI battery | `python scripts/run_ci_tests.py` | **ALL PASS** |
| Component health | `python audit_all.py` | **OK** |
| Security audit | `python main.py --audit` | **OK** (see log) |
| Lookahead audit | `python scripts/lookahead_audit.py --ci` | **OK** after manual `bin_vol[hi+1]` classification |

---

## Residual Risks (pre-mainnet)

1. **Live testnet soak** — behavioural E2E passes with mocks; real signing key + HL fill tape still required in Fase 10.
2. **ADOPT_AND_PROTECT (testnet only)** — adopts exchange size without strategy context; operator should verify `signal_metadata` on adopted trades. Mainnet uses **HALT** instead.
3. **Reconciliation interval (60s)** — drift may persist up to one interval; entries blocked when stale/failing or `close_pending_reconciliation`.
4. **Mainnet flatten-on-stop** — `mode_overrides.mainnet.flatten_on_stop: true` remains until canary sign-off; independent of mainnet enable flags.

---

## Gate Checklist (pre-Fase 04)

- [x] Crash does not remove exchange-native protection (triggers on HL; `ws_off_trigger_still_on_exchange`)
- [x] Drift blocks new entries (`reconciliation_block`, `size_mismatch_halts_entries`)
- [x] Kill switch confirms flat (mocked)
- [x] Partial fill protection sized to `filled_size`
- [x] Restart restores pending orders + `applied_fill_size`
- [x] Native SL/TP fill → reconcile exactly once (`fill_price`, fees, PnL from tape)
- [x] No fill tape → `close_pending_reconciliation` + entry halt (no silent estimate)
- [x] Sibling trigger cancelled after SL/TP; residual triggers purged on restart
- [x] Mainnet orphan policy = `HALT`; testnet = `ADOPT_AND_PROTECT`
- [x] Testnet entry → trigger → restart → reconcile (mocked E2E)
- [ ] Live testnet: kill switch on real wallet (manual)

**Verdict:** Pre-Fase 04 gate **passed** (mocked). Ready to proceed to **Fase 04**. **Not ready for mainnet capital.**
