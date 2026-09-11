---
name: testnet-e2e
description: Run the live testnet E2E suite safely — credentials, env vars, kill-switch caveat. NEVER run with mainnet keys.
triggers:
  - user
allowed-tools:
  - exec
  - read
  - grep
  - glob
---

# Testnet E2E suite

Canonical reference: `docs/TESTNET_E2E_GUIDE.md`. Suite: `tests/test_testnet_e2e.py`
(8 real testnet scenarios: maker rest, market fill, partial fill, cancel,
native SL/TP triggers, crash/restart recovery, orphan adoption, kill-switch flatten).

## Credentials — required before anything runs

- `HYPERLIQUID_PRIVATE_KEY` — an Ethereum-compatible hex key (64 hex chars,
  `0x` prefix) for the **testnet** wallet or agent key.
- `HYPERLIQUID_ACCOUNT_ADDRESS` — the master account being traded when using
  an API/agent wallet (signer = agent key, traded account = this address).
- Generic `HYPERLIQUID_API_KEY`/`API_SECRET` values are NOT valid signing keys.
- Without credentials the suite skips cleanly (8 skipped) — that is correct.

## Safety rules — hard

- **NEVER** run this suite with mainnet keys. Scenario 8 (kill switch) flattens
  ALL positions on the configured account.
- Use a dedicated testnet account or an agent wallet. Account value ~1000 USDC
  testnet.
- If partial-fill fails with "Insufficient margin", rerun with a smaller size:
  `$env:HYPERLIQUID_TESTNET_PARTIAL_SIZE = "0.05"`.

## Run

```powershell
python -X utf8 -c "from src.utils.config import load_config; load_config('config/settings.yaml'); import pytest,sys; sys.exit(pytest.main(['tests/test_testnet_e2e.py','-v','-m','testnet_live']))"
```

## Known landmines (both already fixed — regression signals if they reappear)

- `float_to_wire causes rounding` → meta cache not populated: SDK `Info.meta()`
  returns a dict with `universe`, not a list. Check `build_meta_cache`.
- Trigger orders invisible → `get_open_orders` must use `frontend_open_orders`
  (includes trigger orders); plain `open_orders` omits them.

## After a green run

Report 8/8 with the command used. Do NOT claim mainnet readiness — the suite
proves execution mechanics only; OOS/data gates are separate.
