"""Point-in-time unlock calendar — two independent sources (§1, §2).

Source A: DefiLlama emissions (index + per-protocol schedule).
Source B: CryptoRank vesting via CRYPTORANK_API_KEY, else Tokenomist
public; whichever answered is recorded on the snapshot row.

Every snapshot is stored raw + sha256 so the calendar a decision used is
byte-for-byte replayable. Normalization is deliberately tolerant: each
source's payload is walked for dated token-amount records and folded into
RawUnlock rows; unknown shapes yield fewer rows, never fabricated ones.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.research.unlock_shadow import spec

UA = "hl-unlock-shadow/1.0"

_TS_KEYS = ("timestamp", "unlockTime", "unlock_time", "date", "time", "ts")
_AMT_KEYS = ("noOfTokens", "amount", "tokens", "numberOfTokens",
             "tokenAmount", "value", "size")
_KIND_KEYS = ("unlockType", "type", "kind", "vestingType", "eventType")
_CAT_KEYS = ("category", "allocation", "recipient", "group", "description")
_SUPPLY_KEYS = ("unlockedSupply", "circulatingSupply", "unlocked",
                "circulating_supply", "unlocked_supply")


@dataclass(frozen=True)
class RawUnlock:
    """One normalized unlock observation from a single source."""
    token: str
    event_ms: int
    amount_tokens: float
    category: str          # raw source category/description text
    kind: str              # 'cliff' | 'linear' | 'excluded'
    unlocked_supply: Optional[float]  # pct basis if the source provides it
    source: str            # 'defillama' | 'cryptorank' | 'tokenomist'


def sha256_payload(payload: Any) -> str:
    """Canonical hash of the raw payload stored with every snapshot."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def fetch_json(url: str, headers: Optional[Dict[str, str]] = None,
               timeout_s: int = 60) -> Any:
    """Single outbound helper — the frozen prereg's declared public
    calendar endpoints (spec.py constants); no caller-controlled URL."""
    req = urllib.request.Request(  # audit-ok: AUDIT-003 — declared endpoints only
        url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(  # audit-ok: AUDIT-003 — same justification
            req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


# ─── classification (pure — the rule §2 tests pin) ───────────────────────────

def classify_event(kind_raw: str, category: str) -> str:
    """'cliff' | 'linear' | 'excluded'.

    Excluded categories (noncirculating/farming/staking/burned/on-chain-
    followed allocations) are removed before any eligibility math. Anything
    not tagged linear is treated as a discrete dated event — a cliff by
    definition in this taxonomy.
    """
    cat = (category or "").lower()
    if any(bad in cat for bad in spec.EXCLUDED_CATEGORIES):
        return "excluded"
    k = (kind_raw or "").lower()
    if k in ("linear", "continuous", "daily", "monthly", "vesting"):
        return "linear"
    return "cliff"


def _norm_ts(v: Any) -> Optional[int]:
    """Seconds-or-ms timestamps -> ms. Returns None when unparseable."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return int(t * 1000) if t < 1e12 else int(t)


def _amount(v: Any) -> Optional[float]:
    """noOfTokens may be a list aligned to categories — sum is the total."""
    if isinstance(v, list):
        vals = [float(x) for x in v if isinstance(x, (int, float))]
        return sum(vals) if vals else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_key(d: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _unlocked_supply(payload: Dict[str, Any]) -> Optional[float]:
    v = _first_key(payload, _SUPPLY_KEYS)
    if isinstance(v, dict):
        v = _first_key(v, ("value", "amount", "usd"))
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _walk_events(node: Any, depth: int = 0):
    """Yield dicts that look like dated token-amount records."""
    if depth > 6:
        return
    if isinstance(node, dict):
        ts = _first_key(node, _TS_KEYS)
        amt = _first_key(node, _AMT_KEYS)
        if ts is not None and amt is not None:
            yield node
        else:
            for v in node.values():
                yield from _walk_events(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_events(v, depth + 1)


def _normalize_payload(token: str, payload: Any, source: str
                       ) -> List[RawUnlock]:
    """Source-agnostic: walk for dated token-amount records, classify each."""
    if not isinstance(payload, dict):
        return []
    supply = _unlocked_supply(payload)
    out: List[RawUnlock] = []
    seen = set()
    for ev in _walk_events(payload):
        ts = _norm_ts(_first_key(ev, _TS_KEYS))
        amt = _amount(_first_key(ev, _AMT_KEYS))
        if ts is None or amt is None or amt <= 0:
            continue
        if (ts, amt) in seen:
            continue
        seen.add((ts, amt))
        cat = str(_first_key(ev, _CAT_KEYS) or "")
        kind = classify_event(str(_first_key(ev, _KIND_KEYS) or ""), cat)
        out.append(RawUnlock(token=token.upper(), event_ms=ts,
                             amount_tokens=amt, category=cat, kind=kind,
                             unlocked_supply=supply, source=source))
    return out


def normalize_defillama(token: str, payload: Any) -> List[RawUnlock]:
    return _normalize_payload(token, payload, "defillama")


def normalize_source_b(token: str, payload: Any, source_name: str
                       ) -> List[RawUnlock]:
    return _normalize_payload(token, payload, source_name)


# ─── live fetchers (daemon injects stubs in tests) ───────────────────────────

def fetch_defillama_index() -> List[Dict[str, Any]]:
    """emissionsIndex — [{name, slug?, token?, ...}] for every tracked
    protocol; the daemon intersects symbols with the HL perp universe."""
    data = fetch_json(spec.DEFILLAMA_INDEX)
    return data if isinstance(data, list) else []


def fetch_defillama_emissions(slug: str) -> Dict[str, Any]:
    return fetch_json(spec.DEFILLAMA_EMISSIONS.format(slug))


def fetch_source_b(slug: str) -> Tuple[Optional[Any], str]:
    """CryptoRank first when the env key exists; Tokenomist public as the
    documented fallback. Returns (payload|None, source_name) — the name is
    persisted on the snapshot row (§1: 'registar qual')."""
    key = os.getenv(spec.CRYPTORANK_ENV_KEY)
    if key:
        try:
            return fetch_json(spec.CRYPTORANK_VESTING.format(slug),
                              headers={"x-api-key": key}), "cryptorank"
        except Exception:  # noqa: BLE001 — fall through to public source
            pass
    try:
        return fetch_json(spec.TOKENOMIST_VESTING.format(slug)), "tokenomist"
    except Exception:  # noqa: BLE001
        return None, "none"


def index_symbol_slug(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(SYMBOL, slug) from an emissionsIndex row, tolerant to field names."""
    tok = row.get("token") or row.get("symbol") or row.get("ticker")
    if isinstance(tok, str):
        tok = tok.lstrip("$").upper() or None
    else:
        tok = None
    slug = row.get("slug") or row.get("protocolSlug") or row.get("name")
    if isinstance(slug, str):
        slug = slug.lower().replace(" ", "-") or None
    else:
        slug = None
    return tok, slug
