# VPS Migration Runbook — Oracle A1 / Ubuntu 24.04 ARM64

Target: Oracle A1 (ARM64, 2 cores, 12 GB RAM, Ubuntu 24.04), **shared with
the Meridian bot**. This runbook migrates the paper bot + dashboard + the
9.2 GB research DB without rebuilding history — the shadow-outcome
evaluator depends on the full `shadow_decisions`/`candles_1m` history.

**Worktree rule:** all prep happens on branch `feat/vps` in the
`..\hyperliquid-vps` worktree. The live Windows checkout is never touched.

---

## 0. Layout on the VPS

```
/srv/hyperliquid/
├── app/                 # git checkout (deploy.sh swaps refs; code only)
│   ├── .venv/           # Python 3.13 venv (uv) — see §1a for why not 3.14
│   ├── data/live/       # bot.db + jev_latest.json (repo-relative, as today)
│   ├── data/vault.enc   # secret — scp'd, chmod 600
│   └── logs/
├── data/
│   ├── research/hyperliquid.db   # 9.2 GB — BOT_RESEARCH_DATABASE_PATH
│   └── l2_books/                 # BOT_MARKET_DATA_L2_RECORDING_PATH
└── backups/             # staging ONLY — operator PC pulls from here
    ├── runs/<id>/       # manifest.json + hyperliquid.db + bot.db
    └── l2_books/        # incremental mirror
/etc/hyperliquid/bot.env # paths + secrets, chmod 600, owner hyperliquid
```

`bot.env` carries `BOT_RESEARCH_DATABASE_PATH`, `BOT_DATABASE_PATH`,
`BOT_MARKET_DATA_L2_RECORDING_PATH`, `BOT_BACKUP_ROOT` — every runtime path
resolves from config/env; nothing in the tree depends on `E:\` or `C:\`.

## 0a. Shared-machine contract — owned by the Meridian repo

This box runs BOTH bots. Machine-level setup — users `meridian`,
`hyperliquid`, `dev`; group membership so `dev` reads logs/stores (but
never secrets, which stay `chmod 600`); Tailscale; SSH hardening; and the
**CPU/memory weight table** — is specified in the Meridian repo:
`docs/VPS-SETUP.md`. This runbook covers only Hyperliquid-specific pieces
and must stay consistent with it:

- Layout `/srv/hyperliquid/{app,data,backups}` + `/etc/hyperliquid/bot.env`
  matches VPS-SETUP §2/§7 verbatim.
- `hyperliquid-bot.service` carries `CPUWeight=1000` — the top weight on
  the box (meridian-market-data 600, meridian-lane@ 500, rest ≤200); the
  other `hyperliquid-*` units stay ≤100 per the table.
- `dev` reads Hyperliquid logs/stores via membership in the `hyperliquid`
  group — keep app data/logs group-readable, secrets stay 600.

## 1. Provision (once)

```bash
# on the VPS, as a sudo user — repo already cloned to /srv/hyperliquid/app
sudo -u hyperliquid git clone -b feat/vps <repo-url> /srv/hyperliquid/app
sudo bash /srv/hyperliquid/app/deploy/install.sh
```

install.sh creates the `hyperliquid` user if missing (canonical user
setup lives in the machine runbook — see §0a), installs uv +
**Python 3.13** (Ubuntu 24.04 ships 3.12 — too old), a minimal toolchain
as safety net, the venv, and enables (does not start) all units + timers.

### 1a. Python 3.13, not 3.14 — all-binary install (PyPI-verified 2026-09)

3.14 was considered and rejected: numpy 2.2.5, pandas 2.2.3 and scipy
1.15.3 have **no** cp314 aarch64 wheels (they predate 3.14) — scipy alone
is a ~1h gfortran+OpenBLAS build on 2 ARM cores. On **cp313 the same pins
are all wheels**:

| Pin | cp313 aarch64 wheel on PyPI |
|---|---|
| numpy 2.2.5 | `numpy-2.2.5-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` |
| pandas 2.2.3 | `pandas-2.2.3-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.whl` |
| scipy 1.15.3 | `scipy-1.15.3-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` |
| aiohttp 3.14.3, cryptography 50.0.0, frozenlist, multidict, markupsafe, yarl, propcache, cffi | yes — these releases were built while cp313 was current |
| pure-Python stack (websockets, flask, requests, HL SDK, eth-account) | n/a |

Same pins as the Windows box → identical numeric stack for backtests,
zero compilation. The full CI suite passed under 3.13.15 (uv-managed):
`1813 passed, 11 skipped` — no interpreter-specific failures.

**Tripwire still applies on the VPS** (install.sh prints it before
installing): any dep resolving to `sdist`/source on linux-aarch64 cp313
means the audit is stale — stop and report rather than compiling.
```bash
sudo -u hyperliquid uv pip install --python /srv/hyperliquid/app/.venv/bin/python \
    --dry-run -r /srv/hyperliquid/app/requirements.txt
```

## 2. Secrets — scp, never git

From the operator PC:
```powershell
scp .env .\config\.env .\data\vault.enc  # gather what exists locally

# bot.env (paths + secrets) -> /etc/hyperliquid/bot.env
scp .\deploy\bot.env.filled  user@VPS:/tmp/bot.env
ssh user@VPS "sudo install -m 600 -o hyperliquid -g hyperliquid /tmp/bot.env /etc/hyperliquid/bot.env && rm /tmp/bot.env"

# vault -> app data dir
scp .\data\vault.enc user@VPS:/tmp/vault.enc
ssh user@VPS "sudo install -m 600 -o hyperliquid -g hyperliquid /tmp/vault.enc /srv/hyperliquid/app/data/vault.enc && rm /tmp/vault.enc"
```

`.env`/`vault.enc`/`bot.env` are never committed (`.env` and vault already
gitignored; `deploy/bot.env.example` is the only tracked file, no values).

## 3. Research DB migration (~9.2 GB, bot stopped)

The evaluator reads full history (`shadow_outcome_evaluator` `window_ms=None`)
— **never rebuild from raw**. Migrate the file itself, verified.

```powershell
# — on the operator PC —
# 1. Stop the Windows bot (writes must cease for a final consistent file).
#    Then take a verified snapshot with the existing tooling:
python scripts/ops/backup_research_data.py --tag monthly
#    produces a verified copy + sha256 + integrity_check in
#    D:\hyperliquid_backup\runs\<id>\hyperliquid.db — or use that copy
#    directly as the migration source (it is checkpointed/consistent).

# 2. Record source sha256 (of the file you will send):
Get-FileHash D:\hyperliquid_backup\runs\<id>\hyperliquid.db -Algorithm SHA256
```

```bash
# 3. Transfer (resumable via WSL/rsync; plain scp works too):
scp -C D:/hyperliquid_backup/runs/<id>/hyperliquid.db \
    user@VPS:/tmp/hyperliquid.db
#    or:  rsync -avz --partial --progress ... user@VPS:/tmp/hyperliquid.db

# 4. On the VPS — verify before it becomes the live path:
sha256sum /tmp/hyperliquid.db            # MUST equal the source hash
sudo install -m 640 -o hyperliquid -g hyperliquid /tmp/hyperliquid.db \
    /srv/hyperliquid/data/research/hyperliquid.db
sudo -u hyperliquid /srv/hyperliquid/app/.venv/bin/python - <<'PY'
import sqlite3
con = sqlite3.connect("file:/srv/hyperliquid/data/research/hyperliquid.db?mode=ro", uri=True)
print("integrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("trade_tape","l2_snapshots","candles_1m","shadow_decisions"):
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
PY
#    Expect integrity ok + counts equal to the source manifest's snapshot.
```

Rollback: the Windows bot + `E:\hyperliquid_research\hyperliquid.db` remain
untouched until the VPS run is proven. Source backup stays in `D:\` until a
VPS-side verified backup has been pulled home.

## 4. Cutover (human decision — never automatic)

```bash
sudo systemctl start hyperliquid-bot        # paper mode; dashboard separate
sudo systemctl start hyperliquid-dashboard  # 127.0.0.1:5000 read-only
# timers start on schedule (enabled at install); to start feeds now:
sudo systemctl start hyperliquid-jev.timer hyperliquid-wallet-fills.timer \
    hyperliquid-shadow-eval.timer hyperliquid-watchdogs.timer \
    hyperliquid-overnight.timer hyperliquid-backup.timer
```

Bot-Alive is gone — `Restart=always` + `StartLimitBurst=5` replaces it.
Watch: `journalctl -u hyperliquid-bot -f`.

## 5. Dashboard access — no public bind, no ngrok

The dashboard refuses non-loopback binds (`dashboard_server.py` guard).

```powershell
# SSH tunnel (any remote shell):
ssh -L 5000:127.0.0.1:5000 user@VPS
#   -> http://localhost:5000

# or Tailscale (both machines on the tailnet):
ssh -L 5000:127.0.0.1:5000 user@<tailscale-name-or-IP>
#   same — the service still only listens on loopback.
```

`ngrok_tunnel.bat` stays a Windows artifact and is never deployed.

## 6. Backups on the VPS — pull model, destination = operator PC

The VPS disk is staging only. Flow:

```
hyperliquid-backup.timer (day 1, 04:00)
    -> verified run in /srv/hyperliquid/backups/runs/<id>/
       (integrity_check + counts + sha256, ok requires research DB)
    -> incremental L2 mirror /srv/hyperliquid/backups/l2_books/

operator PC, over Tailscale/SSH:
    python scripts/ops/pull_vps_backup.py --host hyperliquid-vps
    -> pulls manifest + DBs + L2 files into D:\hyperliquid_backup\
    -> sha256-verifies every pulled file against the manifest
    -> writes .pulled_ok marker; prunes remote runs beyond --keep-remote
```

Failure behavior: unverified pulls are never marked `.pulled_ok` and never
pruned remotely — a partial pull retries cleanly next run (the L2 mirror is
incremental; DBs re-pull whole). Schedule it on the PC (Task Scheduler,
e.g. day 1 06:00, after the 04:00 VPS run finishes — a ~10 GB pull over
Tailscale takes a while).

## 7. Remaining Windows-only paths (intentionally kept — PC ops only)

| File | Path | Why kept |
|---|---|---|
| `config/settings.yaml:942,1035` | `E:/hyperliquid_research/...` | **This machine's** config — VPS overrides via `BOT_*` env |
| `scripts/ops/backup_research_data.py:60` | `D:/hyperliquid_backup` | Default root for the *PC*; `BOT_BACKUP_ROOT` overrides on VPS |
| `scripts/ops/rereg_hidden_tasks.ps1`, `register_wallet_fills_task.ps1`, `run_hidden.vbs` | `C:\Users\Braindead\...` | Windows Task Scheduler tooling — never deployed |
| `*.bat` (12 launchers) | repo-relative + `C:\` in comments | Windows-only ops; replaced by systemd units |
| `tests/test_task_*.py` etc. | `C:\`/`E:\` literals | test fixtures/docstrings |
| `src/data/research_database.py:33` | docstring `C:/workspace` | comment only |
| docs (`AUDIT_2026-08-14`, `L2_BOOK_RECORDING`, feasibility docs) | `E:\`/`C:\` mentions | historical record |

Fixed in this branch: `toptrader_fade_replay.py` (was `E:\` literal → config),
`iv_gate_shadow_vs_pnl.py` (config silently discarded → ghost DB read).

Note: several research scripts still use `ROOT/"data"/"research"/
"hyperliquid.db"` (repo-relative). On the VPS that path won't exist — same
behavior as reading the ghost DB today. None of the six scheduled timers
use them; fix only if a research workflow needs them remotely.
