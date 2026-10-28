# Hyperliquid Trading Bot

A research trading bot for Hyperliquid perpetual futures, built to run
preregistered experiments under a strict evidence protocol. It currently
runs **paper-only**; mainnet execution is blocked pending out-of-sample
validation and market-data readiness. It is not a product, a service, or
an investment vehicle — it is the runtime for a personal research
programme.

## Current state

The roster below is read from `config/settings.yaml`
(`strategy.phase08.*`) and the compose services; it is kept honest by the
config-hash check in CI.

**Execution (paper, EXPERIMENT promotion — zero evidence claimed):**

| Strategy | Status | Preregistration | Read |
|---|---|---|---|
| JevJudge | paper, owner-declared EXPERIMENT | [PREREGISTER_JEV_OOS_KILL_2026-10-08](docs/ops/PREREGISTER_JEV_OOS_KILL_2026-10-08.md) | at `indep_n ≥ 100`; net PF ≤ 1.0 → off, no retune |

**Active shadow evaluations (signal-tracked, never executed):**

| Name | Preregistration | Read |
|---|---|---|
| CarryA1 (spot–perp carry) | [PREREGISTER_CARRY_SHADOW](docs/ops/PREREGISTER_CARRY_SHADOW.md) | ≥10 closed clusters or 2027-12-26 |
| UnlockShort | [PREREGISTER_UNLOCK_SHADOW](docs/ops/PREREGISTER_UNLOCK_SHADOW.md) | n ≥ 15 eligible events or 2027-10-14 |
| Boros spread | [PREREGISTER_BOROS_SPREAD](docs/ops/PREREGISTER_BOROS_SPREAD.md) | n ≥ 10 clusters or 2027-06-30 |
| Jev v2 (shadow feed) | same Jev prereg above | counts feed the final read |
| VWAPDeviation iv_gate | [PREREGISTER_VWAP_IV_GATE_2026-10-08](docs/ops/PREREGISTER_VWAP_IV_GATE_2026-10-08.md) | sealed board; read on expiry/60 |

**Pending deploy (code and prereg committed; not running anywhere yet):**

| Name | Preregistration | Read |
|---|---|---|
| ResMom forward | [resmom-forward-prereg-2026-10-28](notes/resmom-forward-prereg-2026-10-28.md) | 26 rebalances from 2026-11-02 → ~2027-05-03 |
| Latency probe (measurement, not a strategy) | [latency-measure-prereg-2026-10-28](notes/latency-measure-prereg-2026-10-28.md) | ≥10 sessions + ≥100 events, ≤4 weeks |

The engine's `shadow_strategies` list (VolatilityBreakout,
FundingMomentum, SpotPerpCarry, LeadLag, LiquidationCatcher, and
VWAPDeviation for the sealed iv_gate sample only) is signal-tracked
telemetry, not an evaluation roster.

## Methodology

- **Preregistration before data.** Every evaluation starts from a frozen
  document committed before any forward data is seen
  (`docs/PREREGISTER_*.md`, `notes/*-prereg-*.md`).
- **Amendments are forward-only.** A rule change applies to data not yet
  collected; nothing is retrofitted.
- **Costs are stated, not assumed away.** Paper and backtest use HL
  tier-0 fees — taker 4.5 bps / maker 1.5 bps per side — plus slippage
  and funding where applicable.
- **A single final read.** Verdicts happen once, at the preregistered
  sample size or expiry. No interim peeking to decide stopping.
- **PASS means consistency, not proof.** Samples this small carry little
  statistical power; a PASS promotes at most to the next stage.

## Closed strategies

| Strategy / family | Verdict | Reference |
|---|---|---|
| VWAPDeviation reversion | KILL — −221 USD / 44 live trades | [STRATEGY_AUDIT §0](docs/ops/STRATEGY_AUDIT.md) |
| ChecklistMeta | KILL — −905 USD / 184 live trades | [STRATEGY_AUDIT §0](docs/ops/STRATEGY_AUDIT.md) |
| VolatilityBreakout | demoted — −454 USD / 26 trades | [STRATEGY_AUDIT §0](docs/ops/STRATEGY_AUDIT.md) |
| OrderBookScalper | KILL — net PF 0.21 over 25k evals | [STRATEGY_AUDIT §0](docs/ops/STRATEGY_AUDIT.md) |
| TopTraderFlow | closed — net PF 0.31 over 3.8k evals | [STRATEGY_AUDIT §0](docs/ops/STRATEGY_AUDIT.md) |
| TrendFollow / SmartMoneyFlow | KILL | [STRATEGY_AUDIT](docs/ops/STRATEGY_AUDIT.md) |
| MeanReversion, DonchianBreakout, RangeGrid, TrendPyramid, SFPReversion, VARejection | KILL / closed | [STRATEGY_AUDIT](docs/ops/STRATEGY_AUDIT.md) |
| FundingArbitrage | KILL — not a real arb | [STRATEGY_AUDIT](docs/ops/STRATEGY_AUDIT.md) |
| CVDOrderFlow (+P90) | closed | [STRATEGY_AUDIT](docs/ops/STRATEGY_AUDIT.md) |
| XS momentum, market-making, funding-extreme, S/R, pivots, Pairs 4H, DipGrid, Kronos | research closed — code archived in `archive/research_closed/` | [dep-report-tape-wallet-2026-10-28](notes/dep-report-tape-wallet-2026-10-28.md) |

## Deployment

The production target is a small Linux host running Docker Compose —
see [docs/ops/LINUX_DEPLOY.md](docs/ops/LINUX_DEPLOY.md). One image, one
container per process, systemd timers for daily Restic backups to OCI
Object Storage and weekly prune, healthchecks.io pings on every
scheduled job. The dashboard is reachable only over the tailnet and
requires `BOT_DASHBOARD_TOKEN` (header `X-Dashboard-Token` or `?token=`).

### Legacy local runs (Windows / macOS)

```bash
pip install -r requirements.txt
python main.py --mode paper        # paper trading, dashboard :5000
python main.py --mode testnet      # testnet, fake funds
python main.py --audit             # static security audit
```

Windows launchers (`start.bat`, `quickstart.bat`, `service.bat`) and the
macOS PM2 scripts (`deploy/macos/`) still work but are not the supported
path.

## Development

```bash
python scripts/ops/run_ci_tests.py            # unit + integration_offline
python scripts/ops/run_ci_tests.py --network  # + network suite (opt-in)
python -m pytest -m unit                      # unit only
```

- Pre-push gate: CI must be green; the security audit
  (`python main.py --audit`) must stay clean.
- `config/settings.yaml` is hashed into a config manifest; CI fails if
  the hash drifts from the registered value. Re-register deliberate
  config changes with the ops scripts, don't edit the hash.
- Project rules for agents: [AGENTS.md](AGENTS.md).

## Disclaimer

This repository is a personal research tool. Nothing here is financial
advice, a solicitation, or a promise of performance. Paper results do
not transfer to live markets.
