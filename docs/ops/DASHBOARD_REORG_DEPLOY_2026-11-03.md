# Dashboard reorganization deploy — 2026-11-03

Deployed `feat/post-cutover` @ `0d8c551` to `hl-paper` (bots-01) via
`compose up -d --no-deps hyperliquid`. Dashboard-only change: zero edits
to strategies, risk, execution, or writers.

## What shipped

- **Unified research view** (`src/research/paper_trades.py`, new,
  read-only): one card per ledger — JevJudge (execution), CarryA1,
  UnlockShort, Boros, ResMom, VWAP iv_gate (shadow). Card detail modal
  (open positions, live unrealized, equity+DD, closed trades DESC) and a
  merged cross-strategy trade feed. Endpoints: `/api/topbar`,
  `/api/paper_strategies`, `/api/paper_strategy/<id>`,
  `/api/paper_trades_feed` — all GET, read-only.
- **Topbar restored**: live BTC/ETH/SOL/HYPE prices (live_data), FEED and
  CANDLE age, DISCO %, SERVIÇOS ok/total. Price/latency render cells were
  dropped in `de1901e` (ops split); data always existed.
- **Seal display policy (owner decision)**: preregistration `sealed`
  state is shown as a factual footer label, but all metrics are
  displayed — n, net, APY, WR, open positions, unrealized, readout n/date.
  Presentation layer only; `paper_ledger.py` and writers untouched.
- **SERVIÇOS source**: local observable writers (per-ledger heartbeat
  freshness), replacing healthchecks.io (`HC_API_KEY` absent in prod).
- **Staleness thresholds match real cadence**: jev 90min (hourly
  verdicts), carry/boros 90min (hourly bursts), unlock **30h** (daily
  ~00:30 UTC — verified vs distinct batch timestamps), resmom 30h,
  ivgate 12h. Locks/heartbeat misses serve last-good card, never a
  flapping error.
- **Latency badge fix**: server now answers socket.io `ping` — the
  template emitted it every 5s with no handler, so RTT stayed `--ms`.
- **SQLite handle leak fix** (`shadow_panel._load_persisted_boards`):
  `ResearchDatabase` was opened and never closed (leaked per call on
  `/api/hypotheses` and `paper_ledger`). `finally: rdb.close()`.

## Ops notes

- Deploy path: repo `/srv/hl/app/repo` is root-owned — use `sudo git`.
  Rebuild image, recreate **only** `hl-paper`. Avoid boros `:05 UTC`
  window.
- `hl-unlock-shadow` was restarted once during verification — turned out
  unnecessary (daily cadence, healthy all along); harmless, next snapshot
  ~00:30 UTC.
- Scratch review instance (`hl-dash-scratch`, `/dash` tailscale route)
  fully removed post-review. 12 compose containers only.
- Recurring WARNING noise: `Coinalyze /open-interest` transient errors
  (~9/h observed), pre-existing, external API. Not a deploy regression.

## Post-deploy verification (15 min)

- `config_hash` = `4077927fec6a880c` (Fase-10 frozen, matches manifest)
- `/health`: tick_age ~2.5s, ws_healthy true; engine 4.0 t/s
- `/api/topbar`: services 7/7; feed ~600ms; live prices
- `jev_decisions` 3781 → 3784 (growing)
- 0 ERROR/exception in `hl-paper` logs over 15 min
- Screenshot: all 6 cards with metrics, latency badge 273ms, feed unified
