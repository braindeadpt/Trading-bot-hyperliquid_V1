"""Carry-shadow: live L2 evidence for the A1 spot–perp funding carry.

Implements docs/PREREGISTER_CARRY_SHADOW.md (frozen). Standalone daemon —
subscribes public HL feeds (l2Book/trades/funding), simulates hypothetical
delta-neutral carry episodes, and measures REAL maker fillability, margin
events (proportional deleverage), and realized vs predicted funding.

Isolation contract (enforced + tested):
- Imports NOTHING from src.core / src.strategies / engine paths.
- Writes ONLY data/research/carry_shadow.db (own schema, append-only).
- Places ZERO orders — hypothetical ledger only.
"""
