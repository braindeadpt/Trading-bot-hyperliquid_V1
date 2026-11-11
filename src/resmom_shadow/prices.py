"""HL + Binance fetchers — public market data only (AUDIT-003 class).

Sync urllib on purpose: the daemon is a weekly REST job like boros; every
fetcher is injectable for tests (`fetch_fn` / constructor `fetch` dict).

HL daily candle shape kept: {"t","o","h","l","c","v"} (t=open ms).
HL 1m candle: same keys — used for the 00:05 UTC fill price (open).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from src.resmom_shadow import spec

logger = logging.getLogger("resmom_shadow")

FetchFn = Callable[..., Any]
HTTP_TIMEOUT_S = 20
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_BACKOFF_S = 2.0
RETRY_MAX_BACKOFF_S = 30.0
RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

_UA = {"User-Agent": "resmom-shadow-research",
       "Content-Type": "application/json"}


def _http_status_of(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


def _retry_delay_s(exc: BaseException, attempt_idx: int,
                   base_s: float = RETRY_BASE_BACKOFF_S,
                   cap_s: float = RETRY_MAX_BACKOFF_S) -> float:
    hdrs = getattr(exc, "headers", None)
    if hdrs is not None:
        ra = hdrs.get("Retry-After")
        if ra is not None:
            try:
                val = float(ra)
                if val > 0:
                    return min(cap_s, val)
            except (TypeError, ValueError):
                pass
    return min(cap_s, base_s * (2 ** attempt_idx))


def _urlopen_once(url: str, body: Optional[bytes] = None,
                  headers: Optional[Dict[str, str]] = None) -> bytes:
    hdr = dict(_UA)
    if headers:
        hdr.update(headers)
    # audit-ok: AUDIT-003 — public read-only market data & alert webhook, spec-constant URLs
    req = urllib.request.Request(url, data=body, headers=hdr)
    # audit-ok: AUDIT-003 — declared prereg endpoints; module-constant URLs
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read()


def fetch_json(
    url: str,
    body: Optional[bytes] = None,
    *,
    symbol: str = "",
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_backoff_s: float = RETRY_BASE_BACKOFF_S,
    max_backoff_s: float = RETRY_MAX_BACKOFF_S,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch JSON with exponential backoff on HTTP 429 / 5xx / transient network errors.

    Never swallows errors silently: logs ``ERROR`` with *symbol* and HTTP status
    before re-raising when all attempts fail.
    """
    attempts = max(1, int(max_attempts))
    last_exc: Optional[BaseException] = None
    for idx in range(attempts):
        try:
            raw = _urlopen_once(url, body=body)
            return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            code = _http_status_of(exc)
            if code in RETRYABLE_HTTP_CODES and idx + 1 < attempts:
                wait_s = _retry_delay_s(exc, idx, base_backoff_s, max_backoff_s)
                logger.warning(
                    "resmom: HTTP %s symbol=%s attempt=%d/%d backoff=%.1fs url=%s",
                    code, symbol or "-", idx + 1, attempts, wait_s, url,
                )
                sleep_fn(wait_s)
                continue
            logger.error(
                "resmom: HTTP request failed symbol=%s status=%s attempts=%d url=%s err=%s",
                symbol or "-", code, idx + 1, url, exc,
            )
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if idx + 1 < attempts:
                wait_s = _retry_delay_s(exc, idx, base_backoff_s, max_backoff_s)
                logger.warning(
                    "resmom: network error symbol=%s attempt=%d/%d backoff=%.1fs err=%s",
                    symbol or "-", idx + 1, attempts, wait_s, exc,
                )
                sleep_fn(wait_s)
                continue
            logger.error(
                "resmom: network request failed symbol=%s status=network_error attempts=%d url=%s err=%s",
                symbol or "-", idx + 1, url, exc,
            )
            raise
    assert last_exc is not None
    raise last_exc


def hl_info(payload: Dict[str, Any],
            fetch_fn: Optional[FetchFn] = None,
            *,
            symbol: str = "") -> Any:
    raw_body = json.dumps(payload).encode()
    if fetch_fn is not None:
        return fetch_fn(spec.HL_INFO, body=raw_body)
    return fetch_json(spec.HL_INFO, body=raw_body, symbol=symbol)


def hl_meta_universe(fetch_fn: Optional[FetchFn] = None) -> List[str]:
    """Main-dex perp names — perpMeta universe entries."""
    d = hl_info({"type": "meta"}, fetch_fn, symbol="meta")
    universe = (d or {}).get("universe") or []
    return [str(u.get("name") or "") for u in universe if u.get("name")]


def hl_candles(coin: str, interval: str, start_ms: int, end_ms: int,
               fetch_fn: Optional[FetchFn] = None) -> List[Dict[str, Any]]:
    """candleSnapshot rows -> [{"t","o","h","l","c","v"}] (t=open ms)."""
    d = hl_info({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval,
                         "startTime": start_ms, "endTime": end_ms}},
                fetch_fn,
                symbol=coin)
    out: List[Dict[str, Any]] = []
    for r in d or []:
        try:
            out.append({"t": int(r["t"]), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"]), "v": float(r["v"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: c["t"])


def hl_candles_1d(coin: str, days: int, end_ms: int,
                  fetch_fn: Optional[FetchFn] = None) -> List[Dict[str, Any]]:
    return hl_candles(coin, "1d", end_ms - days * spec.MS_D, end_ms, fetch_fn)


def first_1m_open(coin: str, not_before_ms: int,
                  fetch_fn: Optional[FetchFn] = None) -> Optional[float]:
    """Open of the first 1m candle with t >= not_before_ms (paper fill).

    Wait for the candle to close before trusting its existence: a 1m bar
    starting at ``r['t']`` closes at ``r['t'] + 60_000``. Always returns the
    exact ``open`` of that first bar ``t >= not_before_ms``, never a later spot
    or fetch-time price.
    """
    rows = hl_candles(coin, "1m", not_before_ms, not_before_ms + 10 * 60_000,
                      fetch_fn)
    rows = [r for r in rows if r["t"] >= not_before_ms]
    return rows[0]["o"] if rows else None


def hl_funding_events(coin: str, t0: int, t1: int,
                      fetch_fn: Optional[FetchFn] = None
                      ) -> List[tuple]:
    """(ts_ms, rate) realized funding — reuses the pinned boros fetcher."""
    from src.boros_shadow.funding import fetch_funding
    ev = fetch_funding("hyperliquid", coin, t0, t1, fetch_fn=fetch_fn)
    return ev or []


def hl_all_mids(fetch_fn: Optional[FetchFn] = None) -> Dict[str, float]:
    d = hl_info({"type": "allMids"}, fetch_fn, symbol="allMids")
    out: Dict[str, float] = {}
    for k, v in (d or {}).items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def binance_klines_1d(symbol: str, days: int, end_ms: int,
                      fetch_fn: Optional[FetchFn] = None
                      ) -> List[Dict[str, Any]]:
    """Binance USDT-M daily closes for the descriptive reference + parity.

    Returns the same {"t","o","h","l","c","v"} shape as hl_candles so the
    signal pipeline consumes them identically.
    """
    start = end_ms - days * spec.MS_D
    url = (f"{spec.BINANCE_KLINES}?symbol={symbol}&interval=1d"
           f"&startTime={start}&endTime={end_ms - 1}&limit=1500")
    if fetch_fn is not None:
        rows = fetch_fn(url)
    else:
        rows = fetch_json(url, symbol=symbol)
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        try:
            out.append({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[5])})    # r[5] = base vol — keeps c*v = notional
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda c: c["t"])


def notify_rebalance_failure(arm: str, week_key_ms: int, message: str) -> bool:
    """Best-effort alert on 0-fill failed rebalance (Telegram or healthcheck /fail)."""
    import os
    sent = False
    bot_tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if bot_tok and chat_raw:
        chat_id = chat_raw.split(",")[0].strip()
        tg_url = f"https://api.telegram.org/bot{bot_tok}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": f"🚨 ResMom rebalance FAILED (arm={arm}, week={week_key_ms}): {message}",
        }).encode("utf-8")
        try:
            _urlopen_once(tg_url, body=payload)
            sent = True
        except Exception as exc:  # noqa: BLE001
            logger.error("resmom: telegram failure alert send error: %s", exc)

    hc_url = (os.getenv("HL_RESMOM_HC_URL") or "").strip()
    if hc_url:
        fail_url = hc_url.rstrip("/") + "/fail"
        try:
            _urlopen_once(fail_url, body=message.encode("utf-8"))
            sent = True
        except Exception as exc:  # noqa: BLE001
            logger.error("resmom: healthcheck /fail ping error: %s", exc)
    return sent
