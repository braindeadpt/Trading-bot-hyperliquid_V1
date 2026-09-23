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
│   ├── .venv/           # Python 3.14 venv (uv)
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

## 1. Provision (once)

```bash
# on the VPS, as a sudo user — repo already cloned to /srv/hyperliquid/app
sudo -u hyperliquid git clone -b feat/vps <repo-url> /srv/hyperliquid/app
sudo bash /srv/hyperliquid/app/deploy/install.sh
```

install.sh creates the `hyperliquid` user, installs uv + **Python 3.14**
(Ubuntu 24.04 ships 3.12 — too old), the build toolchain, the venv, and
enables (does not start) all units + timers.

### 1a. ARM64 dependency audit (2026-09, PyPI-verified)

| Pin | aarch64 cp314 wheel? | Consequence |
|---|---|---|
| aiohttp 3.14.3 | **yes** (cp314, musllinux+aarch64 seen; manylinux expected — verify) | OK |
| cryptography 50.0.0 | **yes** (cp39-abi3 manylinux aarch64) | OK |
| websockets 14.2 | pure Python | OK |
| flask/sockets/requests/attr stack | pure Python | OK |
| hyperliquid-python-sdk 0.23.0, eth-account 0.13.7 | pure Python wheels | OK |
| **pandas 2.2.3** | **no** (max cp313) | source build ~15-25 min |
| **numpy 2.2.5** | **no** (max cp313) | source build ~10-15 min |
| **scipy 1.15.3** | **no** (max cp313) | source build — heaviest; gfortran+OpenBLAS installed for it |
| frozenlist 1.6.0, multidict 6.4.4, markupsafe 3.0.2, yarl 1.20.0, propcache 0.5.2 | **no** (max cp313) | small C builds, minutes |
| cffi 2.1.1 | not seen — verify | small C build if missing |
| msgpack, ckzg, bitarray, pydantic-core, pycryptodome | resolution-dependent | **first VPS check** |

**First verification on the VPS** (install.sh already prints it):
```bash
sudo -u hyperliquid uv pip install --python /srv/hyperliquid/app/.venv/bin/python \
    --dry-run -r /srv/hyperliquid/app/requirements.txt
```
Packages listed as `sdist`/`building` will compile — the toolchain
(build-essential, gfortran, libopenblas-dev, meson, ninja, libffi-dev) is
installed for exactly this. If scipy/numpy/pandas source builds fail or are
unacceptable, the fallback is bumping those three pins to versions shipping
cp314 aarch64 wheels (pandas ≥2.3.x, numpy ≥2.3.x, scipy ≥1.16.x) — that is
a separate decision because it changes the numeric stack used by backtests.

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
