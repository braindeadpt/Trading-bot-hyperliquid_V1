"""UnlockShadow: forward-registered shadow study of token cliff unlocks.

Implements docs/PREREGISTER_UNLOCK_SHADOW.md (frozen 2026-10-14).
Standalone daemon — daily point-in-time snapshots of two independent
unlock calendars (DefiLlama + CryptoRank/Tokenomist), deterministic
eligibility, and hypothetical beta-hedged short windows priced on public
Hyperliquid data.

Isolation contract (enforced + tested):
- Imports NOTHING from src.core / src.strategies / engine paths.
- Writes ONLY data/research/unlock_shadow.db (own unlock_* tables).
- Places ZERO orders — hypothetical ledger only.
- No config-file or env-file reads — config-hash neutral by construction.
"""
