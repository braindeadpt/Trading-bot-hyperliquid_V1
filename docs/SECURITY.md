# Hyperliquid Bot Premium — Security Documentation

> **Version:** 1.0  
> **Last updated:** 2026-05-06  
> **Classification:** Internal — Trading Infrastructure  
> **Owner:** Security & Vault Specialist

---

## 1. How Credentials Are Stored

### 1.1 Vault Architecture (`src/security/vault.py`)

All API keys, secrets, and sensitive configuration values are stored in an **encrypted vault file** (`data/vault.enc`) using symmetric encryption provided by the `cryptography` library (Fernet: AES-128-CBC with HMAC-SHA256).

```
┌─────────────────────────────────────┐
│  User Password  │  OS Keyring       │
│       ↓         │       ↓           │
│  PBKDF2-HMAC    │  keyring.get_     │
│  SHA256         │  password()       │
│  480k rounds    │                   │
│       ↓         │       ↓           │
│  32-byte salt   │  32-byte key      │
│       ↓         │       ↓           │
│  base64url key ─┴─→ Fernet instance │
│                     ↓               │
│              encrypt(JSON blob)     │
│                     ↓               │
│              data/vault.enc         │
└─────────────────────────────────────┘
```

**Key derivation parameters:**

| Parameter | Value |
|-----------|-------|
| Algorithm | PBKDF2-HMAC-SHA256 |
| Iterations | 480,000 |
| Salt length | 32 bytes (random, per-vault) |
| Key length | 32 bytes |
| Encoding | Base64URL |

### 1.2 Vault File Format

The encrypted payload is a JSON blob with this structure:

```json
{
  "salt": "<base64>",
  "created_at": 1715000000,
  "entries": {
    "hyperliquid_api_key": "abc123...",
    "hyperliquid_api_secret": "xyz789..."
  }
}
```

- `salt` is only present when a password-derived key is used.
- `created_at` is a Unix timestamp for audit purposes.
- `entries` is a flat string-to-string map.

### 1.3 Key Storage Modes

| Mode | How it works | Use case |
|------|--------------|----------|
| **Password-derived** | User provides a password at runtime; salt is stored in the vault file. | Production — maximum security, password required on every start. |
| **OS Keyring** | A random 32-byte key is generated and stored in the OS credential store (Windows Credential Manager / macOS Keychain / Linux Secret Service). | Development / unattended — convenient, relies on OS security. |
| **Auto-generated** | If no password is given and the keyring is empty, a random key is generated and persisted to the keyring automatically. | First-run convenience. |

### 1.4 Fallback to Environment Variables

If the vault file does not exist, `Vault.retrieve()` falls back to environment variables using the upper-cased key name (e.g., `hyperliquid_api_key` → `HYPERLIQUID_API_KEY`).

This allows the bot to run in containerised environments (Docker, Kubernetes) where secrets are injected via env vars without needing a vault file.

**Recommendation:** In production, always initialise the vault with a strong password and disable env fallback where possible.

### 1.5 Atomic Writes

The vault is always written atomically:

1. Encrypt payload in memory.
2. Write to a temporary file in the same directory.
3. `shutil.move()` the temp file over the target (atomic on POSIX; best-effort on Windows).
4. Clean up temp file on failure.

This prevents corruption if the process crashes mid-write.

---

## 2. Threat Model

### 2.1 Assumptions

| # | Assumption |
|---|------------|
| A1 | The host OS is not compromised at boot time. |
| A2 | The Python interpreter and `cryptography` wheels are unmodified. |
| A3 | The operator (you) is not actively malicious. |
| A4 | Network traffic to Hyperliquid and Binance is over TLS (handled by the exchange clients). |

### 2.2 Threats & Mitigations

| Threat ID | Threat | Severity | Mitigation | Owner |
|-----------|--------|----------|------------|-------|
| T-001 | **Hardcoded API keys** leaked via source code or Git history | 🔴 Critical | Static audit rule `AUDIT-002` scans for secret-like assignments. Vault stores secrets outside source tree. | Audit engine |
| T-002 | **Malicious code injection** via `eval` / `exec` / `compile` | 🔴 Critical | Audit rule `AUDIT-001` forbids these builtins entirely. No `eval`/`exec` in the codebase. | Audit engine |
| T-003 | **Unsafe deserialization** via `pickle.loads` | 🔴 Critical | Audit rule `AUDIT-006` blocks `pickle.loads`. All persistence uses JSON or SQLite. | Audit engine |
| T-004 | **Dynamic imports** loading untrusted modules | 🟠 High | Audit rule `AUDIT-007` flags `__import__` usage. Only explicit static imports are permitted. | Audit engine |
| T-005 | **Data exfiltration** via HTTP to unknown domains | 🟠 High | Audit rule `AUDIT-003` flags all `urllib` / `requests` calls. Domain allowlist (`_ALLOWED_DOMAINS`) gates permitted destinations. | Audit engine |
| T-006 | **Subprocess abuse** for code execution or privilege escalation | 🟠 High | Audit rule `AUDIT-005` flags `os.system` and `subprocess.*` calls. One accepted call site remains (crash recovery — see §2.4). | Audit engine |
| T-007 | **Vault file theft** — attacker reads `data/vault.enc` | 🟠 High | File is encrypted with Fernet. Key is NOT stored in the vault file (derived from password or OS keyring). Attacker needs the key OR the OS session. | Vault module |
| T-008 | **Memory dump** — secrets visible in RAM | 🟡 Medium | Secrets are decrypted on demand and held as plain strings in memory. Mitigated by running on a trusted host and using full-disk encryption. | Operator |
| T-009 | **Path traversal** via unsafe file operations | 🟡 Medium | `validate_safe_path()` rejects paths outside the project directory. Audit rule `AUDIT-004` flags suspicious file writes. | Helpers + Audit |
| T-010 | **ReDoS** via untrusted regex input | 🟡 Medium | All regexes in the project are static, pre-compiled, and bounded. No user input is fed into `re.compile()`. | All modules |
| T-011 | **Commented-out backdoors** | 🟢 Low | Audit rule `AUDIT-008` scans comments for suspicious keywords (`eval`, `exec`, `subprocess`, etc.). | Audit engine |
| T-012 | **Dependency confusion / supply chain** | 🟡 Medium | Pin all dependencies in `requirements.txt`. Verify wheel hashes on install. Review `cryptography` release notes. | Operator |
| T-013 | **Insider threat — operator misconfiguration** | 🟡 Medium | All user input is validated. Config is YAML-based (no executable logic). | Config loader |

### 2.4 Audit findings — decisions (AUDIT-005 subprocess)

Re-evaluated 2026-08-13. The two `AUDIT-005` (subprocess) HIGH findings were
reviewed individually; both are now resolved by remediation or by hardening
+ documented acceptance. Tests: `tests/test_subprocess_remediation.py`.

| Finding | Decision | Rationale |
|---------|----------|-----------|
| `backtest/run_manifest.py:25` — `get_git_commit()` | **REMEDIATED** | Was `subprocess.check_output(["git", "rev-parse", ...])` just to read the current HEAD short hash. Replaced with a pure-file read of `.git/HEAD` (+ the loose ref it points to, with worktree/submodule `gitdir:` handling). Same best-effort `"unknown"` contract, zero external processes, no PATH dependency. Finding gone. |
| `utils/crash_recovery.py:132` — `_run_once()` | **ACCEPTED + HARDENED** | The subprocess is the module's core function: it respawns the bot after a crash — it cannot be removed. Hardened with `_validate_cmd()`: refuses any command whose executable is not the current interpreter, whose script is not `main.py`, or whose `--mode` is not one of `paper`/`testnet`/`live`. No user-controlled argument reaches `subprocess.run` unvalidated. Remains the single accepted HIGH (documented here). |

Residual risk of the accepted finding: an attacker who can already write to
the interpreter or `main.py` on disk could spawn anything — but that is
full host compromise, out of scope for this module's threat model. The
allowlist raises the bar for anything short of that.

#### Scenario: Attacker gains shell access to the bot host

1. **If vault uses password-derived key:**
   - Attacker reads `vault.enc` but cannot decrypt it without the password (not stored on disk).
   - Attacker may read process memory — mitigated by running on an encrypted volume and locking the screen.

2. **If vault uses OS keyring:**
   - Attacker with the same OS session can read the key from the keyring.
   - Mitigation: Use full-disk encryption + screen lock + separate non-privileged OS account for the bot.

#### Scenario: Attacker submits a malicious PR

1. CI runs `python -m src.security.audit` before merge.
2. If the PR introduces `eval`, `pickle.loads`, hardcoded secrets, or unknown-domain HTTP calls, the audit fails (exit code 1).
3. Merge is blocked until fixed.

#### Scenario: Attacker tricks the bot into writing files outside the project

1. `validate_safe_path()` rejects paths that resolve outside the project root.
2. `safe_write_file()` only accepts validated paths or explicitly constructed paths inside the project.
3. Audit rule `AUDIT-004` logs any suspicious file operation for review.

---

## 3. Deployment Security Checklist

Use this checklist before every production deployment.

### 3.1 Pre-Deployment

- [ ] Run the security audit: `python -m src.security.audit --verbose`
  - [ ] Zero CRITICAL findings.
  - [ ] Zero HIGH findings (or each is explicitly documented and approved).
- [ ] Verify no secrets in Git history: `git log --all --source --remotes --patch | grep -iE "(api_key|secret|password|token)"`
- [ ] Ensure `.gitignore` covers: `data/vault.enc`, `data/live/`, `logs/`, `config/settings.yaml`.
- [ ] Confirm `cryptography` wheel hash matches lockfile.
- [ ] Review `requirements.txt` for new/unexpected dependencies.

### 3.2 Host Hardening

- [ ] Run the bot under a dedicated, unprivileged OS user (e.g., `hlbot`).
- [ ] Disable password-based SSH; use key-based auth only.
- [ ] Enable OS firewall (ufw/iptables/Windows Defender) — only outbound 443 is needed.
- [ ] Enable full-disk encryption (BitLocker / LUKS / FileVault).
- [ ] Set up automatic OS security updates.
- [ ] Lock screen / session timeout when unattended.

### 3.3 Vault Setup

- [ ] Initialise the vault with a strong password (≥16 chars, mixed case, numbers, symbols):
  ```python
  from src.security.vault import Vault
  vault = Vault(password="YourVeryStrongPassword123!")
  vault.store("hyperliquid_api_key", "your_key_here")
  vault.store("hyperliquid_api_secret", "your_secret_here")
  ```
- [ ] Confirm `data/vault.enc` exists and is NOT in version control.
- [ ] (Optional) Import from env first, then delete env vars:
  ```python
  vault.import_from_env("hyperliquid_api_key", "hyperliquid_api_secret")
  ```
- [ ] Set restrictive permissions on `data/`:
  - Linux/macOS: `chmod 700 data/`
  - Windows: Remove all non-owner permissions on `data\`.

### 3.4 Runtime

- [ ] Bot runs in paper-trade mode first for ≥24 hours.
- [ ] Dashboard is behind HTTP Basic Auth or VPN-only access.
- [ ] Log files are rotated and purged after 30 days (avoid accumulating secrets in logs).
- [ ] Set up alerting for:
  - [ ] Unusual order sizes (risk manager triggers).
  - [ ] High API error rates.
  - [ ] Disk space / memory exhaustion.

### 3.5 Post-Deployment

- [ ] Re-run security audit after any hotfix or config change.
- [ ] Rotate vault master key quarterly:
  ```python
  vault.rotate(new_password="NewStrongPassword456!")
  ```
- [ ] Review GitHub / GitLab security advisories for `cryptography`, `pandas`, `numpy`, `websockets`.

### 3.6 Feed Silence Contracts — Decision by Deployment

The feed-silence watchdog raises a `degraded` flag when a feed goes silent
past its threshold. Which feeds are actually **contracted** is decided per
deployment by `feed_silence_contracts()` (`src/core/engine.py`), so
`degraded` reflects only feeds that are expected to deliver in **this**
deployment — never feeds that are disabled, blocked or absent here. A feed
that cannot deliver must never be able to force a false `degraded` state
(fstream outage lesson, 2026-06-29).

#### 3.6.1 Contract rules

| Feed | Contracted when | Opt-in mechanism | Default threshold |
|---|---|---|---|
| `binance_perp` | `strategy.lead_lag.enabled` OR `auto_enable` is true (testnet mode override turns it on) | config `strategy.lead_lag` | `binance_perp_max_sec` (1h) |
| `liquidation_binance` | only when the operator opts in | `LIQUIDATION_BINANCE_CONTRACTED=true` in `.env` | `liquidation_binance_max_sec` (6h) |
| `l2_book_recording` | `market_data.l2_recording.enabled` (default true) | config `market_data.l2_recording` | `l2_book_recording_max_sec` (2m) |
| `liquidation_okx`, `liquidation_bybit`, `funding_cex`, `funding_hl`, `taker_split`, `liquidation_coinalyze_check` | **always** — hard contracts | n/a | 6h / 1h / 1h / 1h / 1h / 12h |

#### 3.6.2 The opt-in mechanism (`liquidation_binance`)

Binance's fstream `@forceOrder` channel is blocked on this network, so the
feed cannot deliver here. Contracting it by default would make `degraded`
permanently true. The operator opts the watchdog back in with an env var:

```bash
# .env (gitignored) — re-contract liquidation_binance for THIS deployment
LIQUIDATION_BINANCE_CONTRACTED=true
```

Why `.env` and not `settings.yaml`:

- `.env` is **gitignored** — the contract decision stays deployment-local and
  never leaks into the repository.
- The variable is deliberately **not** `BOT_`-prefixed, so the Fase 10
  `config_hash` (frozen window) stays intact — the hash pins `settings.yaml`
  only, and this opt-in is an operator-side switch, not a strategy change.
- Accepted truthy values: `1`, `true`, `yes` (case-insensitive).

#### 3.6.3 `binance_perp` and the LeadLag bridge

`binance_perp` prices are published only while the LeadLag perp-price bridge
runs (`strategy.lead_lag.enabled` / `auto_enable`; the testnet mode override
enables it). Without the bridge the feed has no writer, so it is contracted
**only** when the bridge is active. If you enable LeadLag in a deployment,
the watchdog automatically starts watching `binance_perp` — no extra step.

#### 3.6.4 What this means operationally

- [ ] Before deploying, confirm which feeds deliver in the target
      environment (network reachability, exchange channel availability).
- [ ] If Binance fstream is reachable and `@forceOrder` is expected:
      set `LIQUIDATION_BINANCE_CONTRACTED=true` in `.env` **before** start.
- [ ] If LeadLag is enabled, verify `binance_perp` appears in the silence
      contract (it will — automatically).
- [ ] After start, check the dashboard `degraded` state reflects only
      contracted feeds; an uncontracted feed must never light it up.
- [ ] The mechanism is covered by tests
      (`tests/test_feed_contamination_fixes.py`) — the contract function is
      the single source of truth; the engine drops uncontracted feeds from
      the monitor at construction.

---

## 4. Incident Response Guide

### 4.1 Severity Classification

| Level | Definition | Example |
|-------|------------|---------|
| **P0 — Critical** | Active exploitation or imminent loss of funds | Private key leaked, unauthorised trade executed, vault decrypted by attacker |
| **P1 — High** | Confirmed compromise indicator but no confirmed loss | Audit detects new `eval` call, unexpected outbound HTTP, suspicious file write |
| **P2 — Medium** | Potential weakness or policy violation | Password < 12 chars, env fallback enabled in production, old dependency version |
| **P3 — Low** | Hygiene issue, no immediate risk | Missing log rotation, comment contains suspicious keyword |

### 4.2 Response Playbook

#### P0 — Credential / Key Compromise

1. **STOP** — Immediately halt the bot process.
   ```bash
   # Linux/macOS
   pkill -f "python main.py"
   ```
2. **REVOKE** — Log in to Hyperliquid (or exchange) and revoke the compromised API key.
3. **ASSESS** — Check exchange trade history for unauthorised orders.
   - Document every suspicious trade (symbol, side, size, timestamp).
4. **ROTATE** — Generate a new API key pair.
   ```python
   from src.security.vault import Vault
   vault = Vault(password="...")
   vault.store("hyperliquid_api_key", "NEW_KEY")
   vault.store("hyperliquid_api_secret", "NEW_SECRET")
   vault.rotate(new_password="...")  # Optional but recommended
   ```
5. **INSPECT** — Run a full security audit and check Git history for accidental commits:
   ```bash
   git log --all -p -S "OLD_KEY_FRAGMENT" --source --remotes
   ```
6. **HARDEN** — Review host access logs (`/var/log/auth.log`, Windows Event Viewer) for intrusion indicators.
7. **REPORT** — Document the incident in `docs/INCIDENTS.md` with timeline, impact, and remediation.

#### P1 — Audit Failure / Suspicious Code

1. **ISOLATE** — Do NOT deploy or merge the flagged code.
2. **ANALYSE** — Read the audit report (`logs/security_audit_YYYYMMDD.log`).
3. **FIX** — Remove or justify every HIGH/CRITICAL finding.
4. **RE-AUDIT** — Re-run until clean.
5. **REVIEW** — Have a second person review the fix before merge.

#### P2 — Policy Violation

1. **TICKET** — Create a tracking issue with deadline.
2. **REMEDIATE** — Apply the fix (e.g., enforce password length, disable env fallback).
3. **VERIFY** — Re-run audit / config validator.

#### P3 — Hygiene

1. **SCHEDULE** — Add to next maintenance sprint.
2. **FIX** — Low-priority cleanup.

### 4.3 Communication Plan

| Audience | When to notify | Method |
|----------|---------------|--------|
| **Operator (you)** | Any P0 or P1 | Immediate — phone / SMS / Slack DM |
| **Exchange support** | Confirmed unauthorised trades | Open ticket with trade IDs |
| **Law enforcement** | Large-scale theft or insider fraud | File report if local regulations require |
| **Public** | Never — trading infrastructure details are confidential |

### 4.4 Forensic Preservation

If you suspect compromise:

1. **Do NOT delete logs** — they are evidence.
2. Snapshot the following before cleanup:
   - `logs/` directory (all `.log` files).
   - `data/vault.enc` (encrypted — safe to copy).
   - Running process list (`ps aux` / Task Manager screenshot).
   - Network connections (`netstat -tunapl` / `ss -tunapl`).
   - Recent shell history (`~/.bash_history`, PowerShell history).
3. Store snapshots in a write-protected location (external drive or read-only cloud bucket).

---

## 5. Quick Reference

### 5.1 Audit Exit Codes

| Exit Code | Meaning |
|-----------|---------|
| `0` | Clean — no critical findings (no high findings if `--fail-on-high` is used). |
| `1` | Critical (or high) findings detected — pipeline blocked. |
| `2` | Invalid arguments (e.g., missing source directory). |

### 5.2 Vault API Cheatsheet

```python
from src.security.vault import Vault, get_vault

# Initialise
vault = Vault(password="MyStrongPassword!")

# CRUD
vault.store("hyperliquid_api_key", "abc123")
value = vault.retrieve("hyperliquid_api_key")
vault.delete("hyperliquid_api_key")
keys = vault.list_keys()

# Bulk import from environment
vault.import_from_env("hyperliquid_api_key", "hyperliquid_api_secret")

# Key rotation
vault.rotate(new_password="EvenStronger456!")

# Lazy singleton (use in app code)
vault = get_vault(password="MyStrongPassword!")
```

### 5.3 Helper API Cheatsheet

```python
from src.utils.helpers import (
    safe_float, safe_int, safe_json_loads, utc_now, utc_timestamp_ms,
    parse_iso_to_utc, format_utc_iso, pct_change, safe_divide,
    moving_average, ema, rsi, atr, vwap_from_ticks, volume_profile,
    safe_write_file, safe_read_file, validate_symbol, clamp,
)

# Safe parsing
val = safe_float("3.14159", default=0.0)      # → 3.14159
val = safe_float("bad", default=0.0)         # → 0.0
val = safe_float(float("nan"), default=0.0)  # → 0.0

data = safe_json_loads('{"price": 123.45}')  # → dict
data = safe_json_loads("not json")            # → None

# Time
now_ms = utc_timestamp_ms()
dt = parse_iso_to_utc("2024-05-06T14:30:00Z")

# Math
r = rsi(close_prices, window=14)
atr_vals = atr(highs, lows, closes, window=14)
vwap = vwap_from_ticks(prices, volumes, timestamps_ms, interval_ms=60_000)
profile = volume_profile(prices, volumes, num_bins=24)

# Safe file I/O
safe_write_file("data/output.json", json.dumps(data))
text = safe_read_file("data/output.json")

# Validation
sym = validate_symbol("ETH-PERP")   # → "ETH-PERP"
sym = validate_symbol("../../../etc/passwd")  # → None
```

---

## 6. Contacts & Escalation

| Role | Responsibility |
|------|---------------|
| **Operator** | Day-to-day bot operation, vault password custody, first responder for incidents |
| **Security Lead** (you) | Audit review, incident investigation, key rotation, threat model updates |
| **Exchange Support** | API key revocation, trade dispute, account lockdown |

---

*End of document.*
