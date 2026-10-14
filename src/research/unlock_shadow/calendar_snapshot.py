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
import re
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


# ─── source A: DefiLlama emissions (backtest parity, §2) ─────────────────────
#
# The live payload contract (probed 2026-10-14):
#   emissionsIndex        -> {"data": [{tokenPrice:[{symbol,...}], protocolSlug,
#                           token:"source:id", ...}]}   (never a plain ticker)
#   emissions/{slug}      -> metadata.events[] = the DECLARED cliff calendar
#                            ({timestamp, noOfTokens[], category, unlockType,
#                            description}); metadata.notes[] = free-text
#                            methodology notes; documentedData.data[].data[]
#                            = the realized daily 'unlocked' series used as
#                            the % denominator. documentedData is NEVER an
#                            event source.

_RECIPIENT_RES = tuple(re.compile(p, re.IGNORECASE)
                       for p in spec.RECIPIENT_RES)
_NOTES_ONCHAIN_RE = re.compile(spec.NOTES_ONCHAIN_RE, re.IGNORECASE)


def _recipient(description: str) -> Optional[str]:
    """The allocation a description names as holding the tokens."""
    for rx in _RECIPIENT_RES:
        m = rx.search(description or "")
        if m:
            return m.group(1).strip()
    return None


def _tracked_onchain(recipient: Optional[str], notes: Any) -> bool:
    """§2 ex-post exclusion: a notes[] entry naming the recipient while
    documenting on-chain tracking ('track' + transfer|claim|onchain|
    on-chain|outflow|event) marks the allocation as already followed
    on-chain — out of the eligible set."""
    if not recipient or not isinstance(notes, (list, tuple)):
        return False
    r = recipient.lower()
    for n in notes:
        s = str(n).lower()
        if r in s and "track" in s and _NOTES_ONCHAIN_RE.search(s):
            return True
    return False


def unlocked_denominator(payload: Any) -> Optional[float]:
    """§2 '% of unlocked supply' basis — the backtest's denominator: sum
    'unlocked' across all documentedData sections per timestamp and take
    the last available point (the newest state inside the stored snapshot;
    at read time only the snapshot taken by T-6 exists). supplyMetrics.
    adjustedSupply is deliberately NOT used — it would move the 2%
    threshold off the backtest baseline. None when 0 or absent."""
    dd = (payload or {}).get("documentedData") or {}
    totals: Dict[int, float] = {}
    for block in dd.get("data") or []:
        for pt in (block or {}).get("data") or []:
            ts = _norm_ts(pt.get("timestamp"))
            u = pt.get("unlocked")
            if ts is None or not isinstance(u, (int, float)):
                continue
            totals[ts] = totals.get(ts, 0.0) + float(u)
    if not totals:
        return None
    v = totals[max(totals)]
    return v if v > 0 else None


def normalize_defillama(token: str, payload: Any) -> List[RawUnlock]:
    """Source A — only metadata.events counts (§2).

    cliff + INCL category: kept, aggregated per UTC day (amounts sum).
    other categories / non-cliff types / on-chain-tracked recipients:
    recorded descriptively as kind 'excluded'/'linear' — never eligible.
    """
    out: List[RawUnlock] = []
    md = (payload or {}).get("metadata") or {}
    supply = unlocked_denominator(payload)
    notes = md.get("notes")
    by_day: Dict[int, RawUnlock] = {}
    for e in md.get("events") or []:
        if not isinstance(e, dict):
            continue
        ts = _norm_ts(e.get("timestamp"))
        amts = e.get("noOfTokens")
        if not isinstance(amts, list):
            amts = [amts]
        amt = sum(float(x) for x in amts if isinstance(x, (int, float)))
        if ts is None or amt <= 0:
            continue
        cat = str(e.get("category") or "")
        kind = ("cliff" if str(e.get("unlockType") or "").lower() == "cliff"
                else "linear")
        if cat.lower() not in spec.INCL_CATEGORIES:
            kind = "excluded"
        rec = _recipient(str(e.get("description") or ""))
        if kind == "cliff" and _tracked_onchain(rec, notes):
            kind = "excluded"
        if kind != "cliff":
            out.append(RawUnlock(token=token.upper(), event_ms=ts,
                                 amount_tokens=amt, category=cat, kind=kind,
                                 unlocked_supply=supply, source="defillama"))
            continue
        day = ts - ts % spec.MS_D
        prev = by_day.get(day)
        if prev is None:
            by_day[day] = RawUnlock(token=token.upper(), event_ms=day,
                                    amount_tokens=amt, category=cat,
                                    kind="cliff", unlocked_supply=supply,
                                    source="defillama")
        else:
            cats = (prev.category if cat in prev.category
                    else prev.category + ";" + cat)
            by_day[day] = RawUnlock(
                token=prev.token, event_ms=day,
                amount_tokens=prev.amount_tokens + amt, category=cats,
                kind="cliff", unlocked_supply=supply, source="defillama")
    return out + list(by_day.values())


def normalize_source_b(token: str, payload: Any, source_name: str
                       ) -> List[RawUnlock]:
    return _normalize_payload(token, payload, source_name)


# ─── live fetchers (daemon injects stubs in tests) ───────────────────────────

def fetch_defillama_index() -> List[Dict[str, Any]]:
    """emissionsIndex — one row per tracked protocol. The live endpoint
    wraps the rows: {"data": [...]}; a bare list is tolerated."""
    data = fetch_json(spec.DEFILLAMA_INDEX)
    if isinstance(data, dict):
        data = data.get("data") or []
    return data if isinstance(data, list) else []


def fetch_defillama_emissions(slug: str) -> Dict[str, Any]:
    return fetch_json(spec.DEFILLAMA_EMISSIONS.format(slug))


def build_symbol_map(index_rows: Any, hl_universe: Any
                     ) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """{coin: slug} + miss list — the backtest's mapping rule: s =
    tokenPrice[0].symbol (upper); coin = s when s is an HL perp, else
    'k'+s; else a miss. {BTC, ETH, AI} are excluded (DefiLlama's 'AI' is
    Gensyn, not HL's 'AI'). Rebuilt every snapshot and stored with its
    sha256; misses are logged, never fatal."""
    out: Dict[str, str] = {}
    misses: List[Dict[str, str]] = []
    # upper -> original: matching is case-insensitive but the map value
    # must keep HL's own casing ('kPEPE'), the coin the APIs are fed.
    uni = {str(u).upper(): str(u) for u in (hl_universe or [])}
    for row in index_rows or []:
        if not isinstance(row, dict):
            continue
        tp = row.get("tokenPrice") or []
        s = ""
        if tp and isinstance(tp[0], dict):
            s = str(tp[0].get("symbol") or "").strip().upper()
        slug = row.get("protocolSlug") or row.get("slug")
        slug = str(slug).lower() if slug else None
        label = s or str(row.get("name") or "?")
        if not s or not slug:
            misses.append({"label": label, "reason": "no_symbol_or_slug"})
        elif s in spec.MAP_EXCLUDED_SYMBOLS:
            misses.append({"label": label, "reason": "excluded"})
        else:
            coin = uni.get(s) or uni.get("K" + s)
            if coin is None:
                misses.append({"label": label, "reason": "not_on_hl"})
            else:
                out[coin] = slug
    return out, misses


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
    """(SYMBOL, slug) from an emissionsIndex row — kept for callers that
    still probe single rows; the daemon uses build_symbol_map (the
    tokenPrice[0].symbol backtest rule) instead."""
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
