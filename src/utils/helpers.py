"""
Safe helper utilities for the Hyperliquid trading bot.

All functions are deterministic, side-effect-free (except file I/O wrappers),
and guard against common runtime hazards: ``None``, ``NaN``, division by
zero, timezone confusion, and unsafe JSON/string parsing.

No ``eval`` / ``exec`` / ``compile`` anywhere in this module.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Safe JSON parsing
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
_MAX_JSON_DEPTH: int = 64
_MAX_JSON_KEYS: int = 10_000
_MAX_JSON_STRING_LEN: int = 100_000


class JSONSafetyError(Exception):
    """Raised when JSON input exceeds safety limits."""


def safe_json_loads(raw: Union[str, bytes], default: Any = None) -> Any:
    """
    Parse JSON with strict safety guards.

    Guards:
      * Maximum nesting depth: 64
      * Maximum total keys: 10_000
      * Maximum string length per value: 100_000

    Args:
        raw: JSON string or bytes to parse.
        default: Value to return on failure (``None`` by default).

    Returns:
        Parsed Python object, or *default* if parsing fails or safety limits
        are exceeded.
    """
    if not isinstance(raw, (str, bytes)):
        logger.warning("safe_json_loads received non-string input: %s", type(raw).__name__)
        return default

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("JSON decode failed: %s", exc)
        return default

    # Depth & key-count check via iterative stack
    depth = 0
    key_count = 0
    stack: List[Tuple[Any, int]] = [(data, 1)]

    while stack:
        obj, current_depth = stack.pop()
        depth = max(depth, current_depth)
        if depth > _MAX_JSON_DEPTH:
            logger.warning("JSON depth limit exceeded (%d)", depth)
            return default

        if isinstance(obj, dict):
            key_count += len(obj)
            if key_count > _MAX_JSON_KEYS:
                logger.warning("JSON key count limit exceeded (%d)", key_count)
                return default
            for v in obj.values():
                if isinstance(v, str) and len(v) > _MAX_JSON_STRING_LEN:
                    logger.warning("JSON string value exceeds max length")
                    return default
                if isinstance(v, (dict, list)):
                    stack.append((v, current_depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and len(item) > _MAX_JSON_STRING_LEN:
                    logger.warning("JSON string value exceeds max length")
                    return default
                if isinstance(item, (dict, list)):
                    stack.append((item, current_depth + 1))

    return data


def safe_json_load(path: Union[str, Path], default: Any = None) -> Any:
    """Read a file and safely parse its contents as JSON."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", p, exc)
        return default
    return safe_json_loads(raw, default=default)


# ---------------------------------------------------------------------------
# Safe numeric conversion
# ---------------------------------------------------------------------------


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Convert *value* to ``float`` safely.

    Handles ``None``, empty strings, ``NaN``, ``inf``, and invalid types.
    """
    if value is None:
        return default
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    if isinstance(value, (int, np.integer)):
        return float(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value == "":
            return default
        try:
            f = float(value)
            if math.isnan(f) or math.isinf(f):
                return default
            return f
        except ValueError:
            return default
    if isinstance(value, np.ndarray):
        if value.size == 1:
            return safe_float(value.item(), default)
    return default


def safe_int(value: Any, default: int = 0) -> int:
    """
    Convert *value* to ``int`` safely.

    Handles ``None``, ``NaN``, ``inf``, empty strings, and invalid types.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return default
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value == "":
            return default
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def safe_decimal_str(value: Any, default: str = "0.0") -> str:
    """Return a clean decimal string representation, or *default*."""
    f = safe_float(value, default=float(default))
    return f"{f:.10f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Percentage & zero-guarded arithmetic
# ---------------------------------------------------------------------------


def pct_change(new: float, old: float) -> float:
    """Percentage change from *old* to *new*, guarded against division by zero."""
    old_f = safe_float(old)
    new_f = safe_float(new)
    if old_f == 0.0:
        return 0.0
    return (new_f - old_f) / old_f


def pct_of(value: float, total: float) -> float:
    """What percentage *value* is of *total*."""
    total_f = safe_float(total)
    if total_f == 0.0:
        return 0.0
    return safe_float(value) / total_f


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Return *numerator / denominator* or *default* if denominator is zero."""
    d = safe_float(denominator)
    if d == 0.0:
        return default
    return safe_float(numerator) / d


# ---------------------------------------------------------------------------
# Timezone handling — UTC everywhere
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current time in UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def utc_timestamp_ms() -> int:
    """Return the current UTC time as a Unix millisecond timestamp."""
    return int(utc_now().timestamp() * 1000)


def parse_iso_to_utc(ts: Union[str, datetime]) -> Optional[datetime]:
    """
    Parse an ISO-8601 string or datetime into a timezone-aware UTC datetime.

    Returns ``None`` if parsing fails.
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if not isinstance(ts, str):
        return None
    ts = ts.strip()
    if not ts:
        return None
    # Fast paths for common suffixes
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def format_utc_iso(dt: Optional[datetime] = None) -> str:
    """Format a UTC datetime as ISO-8601 with 'Z' suffix."""
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_to_interval_ms(timestamp_ms: int, interval_ms: int) -> int:
    """
    Floor a millisecond timestamp to the nearest *interval_ms* boundary.

    Example: ``floor_to_interval_ms(1_234_567, 60_000)`` → ``1_200_000``.
    """
    return (safe_int(timestamp_ms) // safe_int(interval_ms)) * safe_int(interval_ms)


# ---------------------------------------------------------------------------
# Technical indicators (NumPy / Pandas)
# ---------------------------------------------------------------------------

ArrayLike = Union[List[float], np.ndarray, pd.Series]


def _to_series(data: ArrayLike) -> pd.Series:
    """Coerce input to a Pandas Series (float dtype, NaNs preserved)."""
    if isinstance(data, pd.Series):
        return data.astype(float)
    return pd.Series(data, dtype=float)


def moving_average(data: ArrayLike, window: int) -> pd.Series:
    """
    Simple Moving Average (SMA).

    Args:
        data: Price or indicator series.
        window: Rolling window length.

    Returns:
        Pandas Series of the same length (NaN for the first *window-1* rows).
    """
    s = _to_series(data)
    w = safe_int(window, default=1)
    if w <= 0:
        w = 1
    return s.rolling(window=w, min_periods=1).mean()


def ema(data: ArrayLike, span: int) -> pd.Series:
    """
    Exponential Moving Average.

    Args:
        data: Price or indicator series.
        span: EMA span (related to window length).

    Returns:
        Pandas Series of the same length.
    """
    s = _to_series(data)
    sp = safe_int(span, default=1)
    if sp <= 0:
        sp = 1
    return s.ewm(span=sp, adjust=False, min_periods=1).mean()


def rsi(data: ArrayLike, window: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI).

    Standard Wilder's RSI using exponential smoothing.

    Args:
        data: Price series (usually close prices).
        window: Look-back window (default 14).

    Returns:
        Pandas Series of RSI values (0–100). NaN where insufficient data.
    """
    s = _to_series(data)
    w = safe_int(window, default=14)
    if w <= 0:
        w = 14

    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / w, adjust=False, min_periods=w).mean()
    avg_loss = loss.ewm(alpha=1.0 / w, adjust=False, min_periods=w).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_series = 100.0 - (100.0 / (1.0 + rs))
    rsi_series = rsi_series.fillna(100.0)  # All-gain edge case
    return rsi_series


def atr(
    high: ArrayLike,
    low: ArrayLike,
    close: ArrayLike,
    window: int = 14,
) -> pd.Series:
    """
    Average True Range (ATR).

    Args:
        high, low, close: OHLC series.
        window: ATR smoothing window (default 14).

    Returns:
        Pandas Series of ATR values.
    """
    h = _to_series(high)
    l = _to_series(low)
    c = _to_series(close)

    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    w = safe_int(window, default=14)
    if w <= 0:
        w = 14
    return tr.ewm(alpha=1.0 / w, adjust=False, min_periods=1).mean()


def vwap_from_ticks(
    prices: ArrayLike,
    volumes: ArrayLike,
    timestamps_ms: ArrayLike,
    interval_ms: int = 60_000,
) -> pd.DataFrame:
    """
    Calculate VWAP per time interval from tick / trade data.

    Args:
        prices: Trade prices.
        volumes: Trade volumes.
        timestamps_ms: Trade timestamps in milliseconds.
        interval_ms: Bucket size in milliseconds (default 1 minute).

    Returns:
        DataFrame with columns ``timestamp``, ``vwap``, ``total_volume``.
    """
    df = pd.DataFrame({
        "price": _to_series(prices),
        "volume": _to_series(volumes),
        "timestamp_ms": _to_series(timestamps_ms),
    })

    df["bucket"] = (df["timestamp_ms"] // safe_int(interval_ms, default=60_000)) * safe_int(
        interval_ms, default=60_000
    )

    grouped = df.groupby("bucket").agg(
        pv_sum=("price", lambda x: (x * df.loc[x.index, "volume"]).sum()),
        total_volume=("volume", "sum"),
    )
    grouped["vwap"] = safe_divide(grouped["pv_sum"], grouped["total_volume"])
    grouped = grouped.reset_index().rename(columns={"bucket": "timestamp"})
    return grouped[["timestamp", "vwap", "total_volume"]]


def volume_profile(
    prices: ArrayLike,
    volumes: ArrayLike,
    num_bins: int = 24,
) -> pd.DataFrame:
    """
    Build a volume profile histogram.

    Args:
        prices: Price series.
        volumes: Volume series at each price.
        num_bins: Number of price bins (default 24).

    Returns:
        DataFrame with columns ``bin_low``, ``bin_high``, ``volume``,
        ``pct_of_total``.
    """
    p = np.array([safe_float(v) for v in prices], dtype=float)
    v = np.array([safe_float(v) for v in volumes], dtype=float)

    if len(p) == 0 or len(v) == 0:
        return pd.DataFrame(columns=["bin_low", "bin_high", "volume", "pct_of_total"])

    total_volume = float(np.sum(v))
    if total_volume == 0.0:
        return pd.DataFrame(columns=["bin_low", "bin_high", "volume", "pct_of_total"])

    min_p, max_p = float(np.min(p)), float(np.max(p))
    if min_p == max_p:
        # All prices identical — single bin
        return pd.DataFrame({
            "bin_low": [min_p],
            "bin_high": [max_p],
            "volume": [total_volume],
            "pct_of_total": [1.0],
        })

    bins = np.linspace(min_p, max_p, num_bins + 1)
    bin_indices = np.digitize(p, bins) - 1
    bin_indices = np.clip(bin_indices, 0, num_bins - 1)

    vol_by_bin = np.zeros(num_bins, dtype=float)
    for idx, vol in zip(bin_indices, v):
        vol_by_bin[idx] += vol

    df = pd.DataFrame({
        "bin_low": bins[:-1],
        "bin_high": bins[1:],
        "volume": vol_by_bin,
        "pct_of_total": vol_by_bin / total_volume,
    })
    return df


# ---------------------------------------------------------------------------
# Safe file operations
# ---------------------------------------------------------------------------


def safe_write_file(
    path: Union[str, Path],
    content: str,
    encoding: str = "utf-8",
    max_size_bytes: int = 50_000_000,
) -> bool:
    """
    Atomically write *content* to *path* via a temporary file + move.

    Guards:
      * Content length is checked against *max_size_bytes*.
      * Parent directories are created if missing.
      * Existing file is replaced only on success.

    Args:
        path: Target file path.
        content: Text to write.
        encoding: Text encoding.
        max_size_bytes: Reject writes larger than this (default 50 MB).

    Returns:
        ``True`` on success, ``False`` on failure (logged).
    """
    p = Path(path)
    content_bytes = content.encode(encoding)
    if len(content_bytes) > max_size_bytes:
        logger.warning(
            "safe_write_file refused: %d bytes exceeds max %d", len(content_bytes), max_size_bytes
        )
        return False

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(p.parent), prefix=".safe_write_")
        os.write(fd, content_bytes)
        os.close(fd)
        shutil.move(temp, p)
        return True
    except OSError as exc:
        logger.warning("safe_write_file failed for %s: %s", p, exc)
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp).unlink(missing_ok=True)
        return False


def safe_read_file(path: Union[str, Path], max_size_bytes: int = 50_000_000) -> Optional[str]:
    """
    Read a text file safely with size guard.

    Returns ``None`` if the file is missing, unreadable, or too large.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        size = p.stat().st_size
        if size > max_size_bytes:
            logger.warning("safe_read_file refused: %s is %d bytes (max %d)", p, size, max_size_bytes)
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("safe_read_file failed for %s: %s", p, exc)
        return None


def safe_ensure_dir(path: Union[str, Path]) -> bool:
    """Create directory tree if missing. Returns ``True`` on success."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        logger.warning("safe_ensure_dir failed for %s: %s", path, exc)
        return False


def safe_delete_file(path: Union[str, Path]) -> bool:
    """
    Delete a single file. Returns ``True`` if removed or already gone.

    Does **not** traverse directories or glob — only exact files.
    """
    p = Path(path)
    if not p.exists():
        return True
    if not p.is_file():
        logger.warning("safe_delete_file refused: %s is not a regular file", p)
        return False
    try:
        p.unlink()
        return True
    except OSError as exc:
        logger.warning("safe_delete_file failed for %s: %s", p, exc)
        return False


def safe_list_files(
    directory: Union[str, Path],
    pattern: str = "*",
    recursive: bool = False,
) -> List[Path]:
    """
    List files in *directory* matching *pattern*.

    Args:
        directory: Root directory to search.
        pattern: Glob pattern (default ``"*"``).
        recursive: Whether to recurse into subdirectories.

    Returns:
        Sorted list of ``Path`` objects. Empty list on error.
    """
    d = Path(directory)
    if not d.exists() or not d.is_dir():
        return []
    try:
        if recursive:
            return sorted(d.rglob(pattern))
        return sorted(d.glob(pattern))
    except OSError as exc:
        logger.warning("safe_list_files failed for %s: %s", d, exc)
        return []


# ---------------------------------------------------------------------------
# String / input validation helpers
# ---------------------------------------------------------------------------

# ReDoS-safe — anchored, no nested quantifiers, no backrefs
_SYMBOL_RE: re.Pattern[str] = re.compile(r"^[A-Z0-9]{1,20}$", re.ASCII)
_SAFE_PATH_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-\./]+$", re.ASCII)


def validate_symbol(symbol: Any) -> Optional[str]:
    """
    Validate a trading symbol string.

    Returns upper-cased symbol if valid, ``None`` otherwise.
    """
    if not isinstance(symbol, str):
        return None
    sym = symbol.strip().upper()
    if _SYMBOL_RE.match(sym):
        return sym
    return None


def validate_safe_path(path: Any) -> Optional[Path]:
    """
    Validate a path string for safe characters.

    Returns a ``Path`` object if valid, ``None`` otherwise.
    """
    if not isinstance(path, str):
        return None
    p = path.strip()
    if not p or not _SAFE_PATH_RE.match(p):
        return None
    resolved = Path(p).resolve()
    # Prevent path traversal outside the project (heuristic)
    project_root = Path(__file__).resolve().parents[2]
    try:
        resolved.relative_to(project_root)
    except ValueError:
        logger.warning("validate_safe_path rejected traversal path: %s", resolved)
        return None
    return resolved


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp *value* to the inclusive range [*min_val*, *max_val*]."""
    v = safe_float(value)
    lo = safe_float(min_val)
    hi = safe_float(max_val)
    if lo > hi:
        lo, hi = hi, lo
    return max(lo, min(hi, v))


def round_decimals(value: float, decimals: int = 8) -> float:
    """Round *value* to *decimals* decimal places, with safe defaults."""
    d = safe_int(decimals, default=8)
    if d < 0:
        d = 0
    return round(safe_float(value), d)
