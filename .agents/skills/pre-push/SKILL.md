---
name: pre-push
description: Run the full pre-push gate — preflight feeds, security audit, config-hash check, CI test battery. Use before committing/pushing non-trivial changes.
argument-hint: "[--preflight] [--skip-audit] [--skip-hash]"
triggers:
  - user
  - model
allowed-tools:
  - exec
  - read
  - grep
  - glob
---

# Pre-push gate

Canonical reference: `README.md` §"Pre-push gate" and `AGENTS.md` §7.

## Sequence

Run from the repo root, in order. Stop at the first failure and report it.

```powershell
python scripts/ops/run_pre_push_gate.py              # audit + config-hash + CI battery
python scripts/ops/run_pre_push_gate.py --preflight  # add stage 0: live feed freshness (NOT while the bot is running live)
```

Stages inside the gate:

0. **Preflight (opt-in)** — `scripts/ops/preflight_feed_check.py`; validates deployment feed freshness. Never run `--preflight` against a live running process.
1. **Security audit** — `python -X utf8 -m src.security.audit --src-dir src` (0 CRITICAL/MEDIUM/LOW required; the 2 known pre-existing HIGHs are subprocess-based scripts off the execution path).
2. **Config-hash check** — verifies `config/settings.yaml` still matches the frozen Fase-10 preregistration manifest. If it fails, the config drifted from the frozen window — do NOT "fix" by re-registering; surface it to the user.
3. **CI battery** — `python scripts/ops/run_ci_tests.py` (unit + integration_offline).

## Escape hatches (use only when justified)

- `--skip-audit` — CI battery only
- `--skip-hash` — skip config-hash check
- `--fail-on-high` — stricter audit (HIGH fails too)

## Reporting

Report per-stage PASS/FAIL with the failing command and the first failing test/finding. Do not commit when the gate is red unless the user explicitly accepts the risk in writing.
