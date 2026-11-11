"""Hyperliquid live order client — EIP-712 signing via official SDK.

Provides a thin async wrapper around the ``hyperliquid-python-sdk``
(``hyperliquid.exchange.Exchange`` for signed orders,
``hyperliquid.info.Info`` for read-only queries).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from src.security.vault import Vault, VaultKeyError, VaultNotInitializedError, get_vault
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

VAULT_KEY = "hyperliquid_private_key"
ENV_KEY = "HYPERLIQUID_PRIVATE_KEY"
ENV_ACCOUNT_KEY = "HYPERLIQUID_ACCOUNT_ADDRESS"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"
PERP_MAX_DECIMALS: int = 6
SPOT_MAX_DECIMALS: int = 8
HL_MAX_SIG_FIGS: int = 5
MIN_ORDER_NOTIONAL_USD: float = 10.0
DEFAULT_HTTP_TIMEOUT_S: float = 10.0

_KEY_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDR_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Cache of asset metadata: symbol -> {sz_decimals, px_decimals, ...}
_meta_cache: Dict[str, Dict[str, Any]] = {}


def normalize_private_key(raw: str) -> str:
    """Return a 0x-prefixed 32-byte hex private key."""
    key = raw.strip()
    if not key:
        raise ValueError("Empty private key")
    if not _KEY_PATTERN.match(key):
        raise ValueError("Invalid Hyperliquid private key format (expected 64 hex chars)")
    if not key.startswith("0x"):
        key = f"0x{key}"
    return key


def resolve_private_key(vault: Optional[Vault] = None) -> Optional[str]:
    """Load signing key from env or encrypted vault."""
    env_val = os.environ.get(ENV_KEY, "").strip()
    if env_val:
        try:
            return normalize_private_key(env_val)
        except ValueError as exc:
            logger.error("Invalid %s: %s", ENV_KEY, exc)
            return None

    try:
        v = vault if vault is not None else get_vault()
        return normalize_private_key(v.retrieve(VAULT_KEY, fallback_env=True))
    except (VaultKeyError, VaultNotInitializedError):
        return None
    except ValueError as exc:
        logger.error("Invalid vault key '%s': %s", VAULT_KEY, exc)
        return None
    except Exception as exc:
        logger.error("Failed to load Hyperliquid private key: %s", exc)
        return None


def resolve_account_address() -> Optional[str]:
    """Optional master-account address for agent-wallet (API wallet) signing.

    When ``HYPERLIQUID_ACCOUNT_ADDRESS`` is set, the signing key is treated as
    an HL API/agent wallet: orders are signed by the agent but executed on
    this account. Returns None when unset or malformed.
    """
    val = os.environ.get(ENV_ACCOUNT_KEY, "").strip()
    if not val:
        return None
    if not _ADDR_PATTERN.match(val):
        logger.error("Invalid %s (expected 0x + 40 hex chars)", ENV_ACCOUNT_KEY)
        return None
    return val


# ═══════════════════════════════════════════════════════════════
#  Asset metadata helpers
# ═══════════════════════════════════════════════════════════════

def _round_sig_figs(value: Decimal, sig_figs: int = HL_MAX_SIG_FIGS) -> Decimal:
    """Round positive *value* to at most *sig_figs* significant digits."""
    if value == 0:
        return Decimal("0")
    exponent = value.adjusted() - sig_figs + 1
    quant = Decimal(10) ** exponent
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def make_cloid(
    symbol: str,
    side: str,
    timestamp_ms: Optional[int] = None,
    salt: str = "",
) -> str:
    """Return a deterministic 16-byte Hyperliquid hex ``cloid`` (``0x`` + 32 hex)."""
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    payload = f"hlbot:{symbol}:{side}:{ts}:{salt}".encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=16).hexdigest()
    return f"0x{digest}"


def build_meta_cache(info: Any) -> Dict[str, Dict[str, Any]]:
    """Build a ``{symbol -> {sz_decimals, px_decimals, ...}}`` map from ``Info.meta()``.

    Hyperliquid's ``meta()`` universe objects expose ``szDecimals`` and
    ``maxLeverage`` — there is no ``pxDecimals`` key in the live API. Per
    Hyperliquid's wire specification, perpetual prices allow up to
    ``6 - szDecimals`` decimal places and up to 5 significant figures
    (integer prices are always allowed regardless of significant figures).
    If a test fixture explicitly supplies ``pxDecimals``, it is honoured
    for backward compatibility.
    """
    raw = info.meta()
    # SDK 0.23 returns {"universe": [...], "marginTables": [...]}; older
    # versions returned the universe list directly.
    if isinstance(raw, dict):
        raw = raw.get("universe", [])
    if not isinstance(raw, (list, tuple)):
        return {}
    cache: Dict[str, Dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if name:
            sz_dec = max(0, int(entry.get("szDecimals", 0)))
            explicit_px = "pxDecimals" in entry and entry.get("pxDecimals") is not None
            px_dec = (
                max(0, int(entry["pxDecimals"]))
                if explicit_px
                else max(0, PERP_MAX_DECIMALS - sz_dec)
            )
            cache[name] = {
                "sz_decimals": sz_dec,
                "px_decimals": px_dec,
                "is_spot": False,
                "enforce_sig_figs": not explicit_px,
                "max_leverage": int(entry.get("maxLeverage", 10) or 10),
                "wire_coin": str(name),
            }
    _meta_cache.update(cache)
    return cache


def build_spot_meta_cache(info: Any) -> Dict[str, Dict[str, Any]]:
    """Populate ``_meta_cache`` with Hyperliquid spot market metadata when available.

    Spot markets allow up to ``8 - szDecimals`` decimal places and 5 sig figs,
    and map human pair names (``HYPE/USDC``) to wire coin ids (``@107``).
    """
    spot_fn = getattr(info, "spot_meta", None)
    if not callable(spot_fn):
        return {}
    try:
        spot_raw = spot_fn()
    except Exception as exc:  # noqa: BLE001
        logger.debug("spot_meta unavailable during cache build: %s", exc)
        return {}
    if not isinstance(spot_raw, dict):
        return {}
    tokens = spot_raw.get("tokens")
    universe = spot_raw.get("universe")
    if not isinstance(tokens, list) or not isinstance(universe, list):
        return {}

    token_by_index: Dict[int, Dict[str, Any]] = {}
    for tok in tokens:
        if isinstance(tok, dict) and "index" in tok:
            token_by_index[int(tok["index"])] = tok

    cache: Dict[str, Dict[str, Any]] = {}
    for spot_entry in universe:
        if not isinstance(spot_entry, dict):
            continue
        wire_name = str(spot_entry.get("name", ""))
        tok_pair = spot_entry.get("tokens")
        if not wire_name or not isinstance(tok_pair, (list, tuple)) or len(tok_pair) != 2:
            continue
        base_info = token_by_index.get(int(tok_pair[0]))
        quote_info = token_by_index.get(int(tok_pair[1]))
        if not base_info or not quote_info:
            continue
        sz_dec = max(0, int(base_info.get("szDecimals", 0)))
        px_dec = max(0, SPOT_MAX_DECIMALS - sz_dec)
        pair_name = f"{base_info.get('name', '')}/{quote_info.get('name', '')}"
        meta_obj: Dict[str, Any] = {
            "sz_decimals": sz_dec,
            "px_decimals": px_dec,
            "is_spot": True,
            "enforce_sig_figs": True,
            "max_leverage": 1,
            "wire_coin": wire_name,
            "pair_name": pair_name,
        }
        cache[wire_name] = meta_obj
        if pair_name and pair_name not in _meta_cache:
            cache[pair_name] = meta_obj
    _meta_cache.update(cache)
    return cache


def get_symbol_info(symbol: str) -> Optional[Dict[str, Any]]:
    """Return cached metadata for *symbol*, or None if not found."""
    return _meta_cache.get(symbol)


def resolve_wire_coin(symbol: str) -> str:
    """Resolve human spot pair (e.g. ``HYPE/USDC``) or perp name to SDK coin string."""
    info = get_symbol_info(symbol)
    if info and info.get("wire_coin"):
        return str(info["wire_coin"])
    return symbol


def normalize_size(symbol: str, size: float) -> float:
    """Floor *size* to ``szDecimals`` for *symbol* using exact Decimal arithmetic."""
    if not math.isfinite(size) or size <= 0:
        return 0.0
    info = get_symbol_info(symbol)
    if info is None:
        return size
    decimals = max(0, int(info["sz_decimals"]))
    quant = Decimal(10) ** (-decimals)
    floored = Decimal(str(size)).quantize(quant, rounding=ROUND_DOWN)
    return float(floored)


def normalize_price(symbol: str, price: float) -> float:
    """Round *price* to Hyperliquid's wire tick & 5-significant-figure rules."""
    if not math.isfinite(price) or price <= 0:
        return 0.0
    info = get_symbol_info(symbol)
    if info is None:
        return price
    decimals = max(0, int(info["px_decimals"]))
    d = Decimal(str(price))
    if info.get("enforce_sig_figs", False):
        # Hyperliquid allows any integer price regardless of >5 sig figs (e.g. BTC >= $100k).
        if d >= Decimal("100000"):
            return float(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        d = _round_sig_figs(d, HL_MAX_SIG_FIGS)
    quant = Decimal(10) ** (-decimals)
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


# ═══════════════════════════════════════════════════════════════
#  Live client
# ═══════════════════════════════════════════════════════════════

class HyperliquidLiveClient:
    """Async wrapper around hyperliquid-python-sdk Exchange + Info.

    Usage::

        client = HyperliquidLiveClient(private_key, use_testnet=True)
        await client.open()
        resp = await client.place_entry("BTC", "long", 0.01)
        await client.close()
    """

    def __init__(
        self,
        private_key: str,
        *,
        use_testnet: bool = True,
        account_address: Optional[str] = None,
    ) -> None:
        self._private_key = normalize_private_key(private_key)
        self._use_testnet = use_testnet
        # Agent-wallet mode: sign with *private_key*, trade *account_address*.
        self._account_address = (
            account_address or resolve_account_address()
        )
        self._exchange: Any = None
        self._info: Any = None
        self._wallet_address: Optional[str] = None
        self._action_lock: asyncio.Lock = asyncio.Lock()
        self._http_timeout: float = DEFAULT_HTTP_TIMEOUT_S

    # ── Properties ──────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._exchange is not None and self._info is not None

    @property
    def wallet_address(self) -> Optional[str]:
        return self._wallet_address

    @property
    def exchange(self) -> Any:
        """The underlying ``hyperliquid.exchange.Exchange`` instance."""
        return self._exchange

    @property
    def info(self) -> Any:
        """The underlying ``hyperliquid.info.Info`` instance."""
        return self._info

    # ── Lifecycle ───────────────────────────────────────────────

    def initialize(self) -> None:
        """Create SDK Exchange + Info (blocking — call via asyncio.to_thread)."""
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        wallet = Account.from_key(self._private_key)
        base_url = TESTNET_URL if self._use_testnet else MAINNET_URL
        try:
            self._exchange = Exchange(
                wallet,
                base_url=base_url,
                account_address=self._account_address,
                timeout=self._http_timeout,
            )
        except TypeError:
            self._exchange = Exchange(
                wallet, base_url=base_url, account_address=self._account_address
            )
        exchange_info = getattr(self._exchange, "info", None)
        if exchange_info is not None:
            self._info = exchange_info
        else:
            try:
                self._info = Info(
                    base_url=base_url, skip_ws=True, timeout=self._http_timeout
                )
            except TypeError:
                self._info = Info(base_url=base_url, skip_ws=True)
        # Queries (positions/orders/fills) must target the traded account —
        # the master address when signing via an agent wallet.
        self._wallet_address = self._account_address or wallet.address
        # Warm the meta cache for symbol normalisation (perp + spot)
        build_meta_cache(self._info)
        build_spot_meta_cache(self._info)
        logger.info(
            "HyperliquidLiveClient ready mode=%s wallet=%s signer=%s",
            "testnet" if self._use_testnet else "mainnet",
            self._wallet_address,
            wallet.address if self._account_address else "self",
        )

    async def open(self) -> None:
        """Initialise the client (thread-safe)."""
        await asyncio.to_thread(self.initialize)

    async def close(self) -> None:
        """Tear down the client."""
        self._exchange = None
        self._info = None
        self._wallet_address = None
        _meta_cache.clear()
        logger.info("HyperliquidLiveClient closed")

    def _build_cloid_obj(self, cloid_hex: Optional[str]) -> Any:
        """Convert a ``0x`` + 32-hex string into ``hyperliquid.utils.types.Cloid``."""
        if not cloid_hex:
            return None
        try:
            from hyperliquid.utils.types import Cloid

            return Cloid.from_str(cloid_hex)
        except Exception:  # noqa: BLE001
            return None

    async def _call_exchange_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        px: float,
        hl_order_type: Dict[str, Any],
        *,
        reduce_only: bool = False,
        cloid: Optional[str] = None,
    ) -> Any:
        """Invoke ``self._exchange.order`` under the nonce/session lock."""
        cloid_obj = self._build_cloid_obj(cloid)
        async with self._action_lock:
            if cloid_obj is not None:
                try:
                    return await asyncio.to_thread(
                        self._exchange.order,
                        coin,
                        is_buy,
                        sz,
                        px,
                        hl_order_type,
                        bool(reduce_only),
                        cloid_obj,
                    )
                except TypeError:
                    # Fallback for minimal test doubles with 5- or 6-arg order()
                    pass
            if reduce_only:
                return await asyncio.to_thread(
                    self._exchange.order,
                    coin,
                    is_buy,
                    sz,
                    px,
                    hl_order_type,
                    True,
                )
            return await asyncio.to_thread(
                self._exchange.order,
                coin,
                is_buy,
                sz,
                px,
                hl_order_type,
            )

    # ── Order operations ────────────────────────────────────────

    async def place_entry(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        post_only: bool = False,
        max_slippage_pct: float = 5.0,
        cloid: Optional[str] = None,
        enforce_min_notional: bool = True,
    ) -> Dict[str, Any]:
        """Submit entry order (limit Alo, capped aggressive limit, or SDK market).

        Returns the raw HL response dict.  Raises on invalid params.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        is_buy = side in ("long", "buy", "B")
        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid order size for {symbol}: {sz}")

        coin = resolve_wire_coin(symbol)

        if order_type == "limit_maker":
            px = normalize_price(symbol, safe_float(limit_price))
            if px <= 0:
                raise ValueError(f"Invalid limit price for {symbol}: {px}")
            if enforce_min_notional and (sz * px) < MIN_ORDER_NOTIONAL_USD:
                raise ValueError(
                    f"Order notional ${sz * px:.2f} below Hyperliquid ${MIN_ORDER_NOTIONAL_USD:.0f} minimum for {symbol}"
                )
            tif = "Alo" if post_only else "Gtc"
            hl_order_type = {"limit": {"tif": tif}}
            result = await self._call_exchange_order(
                coin,
                is_buy,
                sz,
                px,
                hl_order_type,
                reduce_only=False,
                cloid=cloid,
            )
        elif order_type == "limit_slippage_cap":
            ref = safe_float(limit_price)
            if ref <= 0:
                raise ValueError(
                    f"limit_slippage_cap requires reference price for {symbol}"
                )
            if enforce_min_notional and (sz * ref) < MIN_ORDER_NOTIONAL_USD:
                raise ValueError(
                    f"Order notional ${sz * ref:.2f} below Hyperliquid ${MIN_ORDER_NOTIONAL_USD:.0f} minimum for {symbol}"
                )
            result = await self.place_aggressive_limit(
                symbol,
                side,
                sz,
                reference_price=ref,
                max_slippage_pct=max_slippage_pct,
                reduce_only=False,
                cloid=cloid,
            )
        else:
            if (
                enforce_min_notional
                and limit_price is not None
                and safe_float(limit_price) > 0
                and (sz * safe_float(limit_price)) < MIN_ORDER_NOTIONAL_USD
            ):
                raise ValueError(
                    f"Order notional ${sz * safe_float(limit_price):.2f} below Hyperliquid ${MIN_ORDER_NOTIONAL_USD:.0f} minimum for {symbol}"
                )
            async with self._action_lock:
                result = await asyncio.to_thread(
                    self._exchange.market_open,
                    coin,
                    is_buy,
                    sz,
                )

        logger.info("HL entry response %s %s: %s", symbol, side, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def place_aggressive_limit(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        reference_price: float,
        max_slippage_pct: float = 5.0,
        reduce_only: bool = False,
        tif: str = "Ioc",
        cloid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place an aggressive IoC limit that simulates a market with a hard slip cap.

        Hyperliquid has no true market orders; CCXT/`market_open` already
        synthesises them. This path makes the band explicit: buy at
        ``ref * (1 + cap%)``, sell at ``ref * (1 - cap%)``, IoC so unfilled
        size is cancelled rather than resting as a maker.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        is_buy = side in ("long", "buy", "B")
        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid aggressive size for {symbol}: {sz}")
        ref = safe_float(reference_price)
        if ref <= 0:
            raise ValueError(f"Invalid reference price for {symbol}: {ref}")
        cap = max(0.0, safe_float(max_slippage_pct)) / 100.0
        raw_px = ref * (1.0 + cap) if is_buy else ref * (1.0 - cap)
        px = normalize_price(symbol, raw_px)
        if px <= 0:
            raise ValueError(f"Invalid aggressive price for {symbol}: {px}")

        coin = resolve_wire_coin(symbol)
        hl_order_type = {"limit": {"tif": tif if tif in ("Ioc", "Gtc", "Alo") else "Ioc"}}
        result = await self._call_exchange_order(
            coin,
            is_buy,
            sz,
            px,
            hl_order_type,
            reduce_only=bool(reduce_only),
            cloid=cloid,
        )
        logger.info(
            "HL aggressive limit %s side=%s size=%.6f ref=%.4f px=%.4f cap=%.3f%% tif=%s: %s",
            symbol,
            side,
            sz,
            ref,
            px,
            max_slippage_pct,
            tif,
            result,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def close_position(
        self,
        symbol: str,
        size: float,
        *,
        reference_price: Optional[float] = None,
        market_mode: str = "sdk_market",
        max_slippage_pct: float = 5.0,
        position_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close an open perp position (reduce-only).

        *market_mode*:
          - ``sdk_market`` — SDK ``market_close`` (default, legacy)
          - ``limit_slippage_cap`` — aggressive IoC limit with hard slip band
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid close size for {symbol}: {sz}")

        if str(market_mode) == "limit_slippage_cap":
            ref = safe_float(reference_price)
            if ref <= 0:
                raise ValueError(
                    f"limit_slippage_cap close requires reference_price for {symbol}"
                )
            if position_side not in ("long", "short"):
                raise ValueError(
                    f"limit_slippage_cap close requires position_side for {symbol}"
                )
            # Closing long → sell; closing short → buy.
            close_side = "short" if position_side == "long" else "long"
            result = await self.place_aggressive_limit(
                symbol,
                close_side,
                sz,
                reference_price=ref,
                max_slippage_pct=max_slippage_pct,
                reduce_only=True,
            )
        else:
            coin = resolve_wire_coin(symbol)
            async with self._action_lock:
                result = await asyncio.to_thread(
                    self._exchange.market_close,
                    coin,
                    sz,
                )
        logger.info("HL close response %s: %s", symbol, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def cancel_order(
        self,
        symbol: str,
        order_id: int,
    ) -> Dict[str, Any]:
        """Cancel an open order by its HL order id.

        Args:
            symbol: Asset name (e.g. ``"BTC"``).
            order_id: Hyperliquid internal order id (integer).

        Returns the raw HL response dict.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        if order_id <= 0:
            raise ValueError(f"Invalid order_id for {symbol}: {order_id}")

        coin = resolve_wire_coin(symbol)
        async with self._action_lock:
            result = await asyncio.to_thread(
                self._exchange.cancel,
                coin,
                order_id,
            )
        logger.info("HL cancel %s oid=%s: %s", symbol, order_id, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def get_order_status(
        self,
        order_id: int | str,
    ) -> Dict[str, Any]:
        """Query Hyperliquid for the current status of a single order."""
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        oid = int(order_id)
        query = getattr(self._info, "query_order_by_oid", None)
        if callable(query):
            result = await asyncio.to_thread(query, self._wallet_address, oid)
            return result if isinstance(result, dict) else {"status": str(result)}

        # Fallback for SDK variants without query_order_by_oid.
        open_orders = await self.get_open_orders()
        for order in open_orders:
            if int(order.get("oid", -1)) == oid:
                return {"status": "open", "order": order}
        return {"status": "unknown"}

    async def get_order_fills(
        self,
        order_id: int | str,
        *,
        lookback_ms: int = 86_400_000,
    ) -> List[Dict[str, Any]]:
        """Return user fills associated with *order_id*."""
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        oid = str(order_id)
        fills_fn = getattr(self._info, "user_fills", None)
        if not callable(fills_fn):
            return []

        try:
            raw = await asyncio.to_thread(fills_fn, self._wallet_address)
        except TypeError:
            raw = await asyncio.to_thread(fills_fn)

        if not isinstance(raw, list):
            return []

        cutoff = int(time.time() * 1000) - int(lookback_ms)
        matched: List[Dict[str, Any]] = []
        for fill in raw:
            if not isinstance(fill, dict):
                continue
            fill_oid = fill.get("oid", fill.get("orderId"))
            if fill_oid is None or str(fill_oid) != oid:
                continue
            ts = int(fill.get("time", fill.get("timestamp", 0)) or 0)
            if ts and ts < cutoff:
                continue
            matched.append(fill)
        return matched

    async def get_user_fills(
        self,
        *,
        lookback_ms: int = 86_400_000,
    ) -> List[Dict[str, Any]]:
        """Return recent user fills for the connected wallet."""
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        fills_fn = getattr(self._info, "user_fills", None)
        if not callable(fills_fn):
            return []

        try:
            raw = await asyncio.to_thread(fills_fn, self._wallet_address)
        except TypeError:
            raw = await asyncio.to_thread(fills_fn)

        if not isinstance(raw, list):
            return []

        cutoff = int(time.time() * 1000) - int(lookback_ms)
        out: List[Dict[str, Any]] = []
        for fill in raw:
            if not isinstance(fill, dict):
                continue
            ts = int(fill.get("time", fill.get("timestamp", 0)) or 0)
            if ts and ts < cutoff:
                continue
            out.append(fill)
        return out

    # ── Read-only queries ───────────────────────────────────────

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return open orders for the traded account, trigger metadata included.

        Uses ``frontend_open_orders`` — the plain ``open_orders`` endpoint
        returns trigger orders WITHOUT ``isTrigger``/``tpsl``/``triggerPx``,
        which would make them indistinguishable from resting limits (breaks
        protection sync, reconciliation and orphan-trigger purge). Falls back
        to the basic endpoint when the frontend variant is unavailable.

        Requires the client to be initialised.
        """
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        fn = getattr(self._info, "frontend_open_orders", None) or self._info.open_orders
        result = await asyncio.to_thread(fn, self._wallet_address)
        return result if isinstance(result, list) else []

    async def get_user_state(self) -> Dict[str, Any]:
        """Return the full user state (positions, account value, …).

        Requires the client to be initialised.
        """
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        result = await asyncio.to_thread(
            self._info.user_state,
            self._wallet_address,
        )
        return result if isinstance(result, dict) else {}

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Return open perp positions parsed from ``user_state``."""
        from src.exchanges.hl_positions import parse_exchange_positions

        state = await self.get_user_state()
        positions = parse_exchange_positions(state)
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in positions.values()
        ]

    async def place_trigger_order(
        self,
        symbol: str,
        position_side: str,
        size: float,
        *,
        trigger_price: float,
        tpsl: str,
    ) -> Dict[str, Any]:
        """Place a reduce-only native SL/TP trigger order."""
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        if tpsl not in ("sl", "tp"):
            raise ValueError(f"Invalid tpsl={tpsl!r} — expected 'sl' or 'tp'")

        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid trigger size for {symbol}: {sz}")

        px = normalize_price(symbol, safe_float(trigger_price))
        if px <= 0:
            raise ValueError(f"Invalid trigger price for {symbol}: {px}")

        # Closing a long = sell (is_buy=False); closing a short = buy.
        is_buy = position_side == "short"
        order_type = {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": tpsl}}
        coin = resolve_wire_coin(symbol)

        result = await self._call_exchange_order(
            coin,
            is_buy,
            sz,
            px,
            order_type,
            reduce_only=True,
        )
        logger.info(
            "HL trigger %s %s %s size=%.6f trigger=%.4f: %s",
            tpsl.upper(),
            symbol,
            position_side,
            sz,
            px,
            result,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all open orders, optionally scoped to *symbol*."""
        orders = await self.get_open_orders()
        cancelled = 0
        for order in orders:
            if not isinstance(order, dict):
                continue
            sym = str(order.get("coin", order.get("symbol", ""))).upper()
            if symbol and sym != symbol.upper():
                continue
            oid = order.get("oid", order.get("orderId"))
            if oid is None:
                continue
            try:
                await self.cancel_order(sym, int(oid))
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("cancel_all_orders: failed %s oid=%s: %s", sym, oid, exc)
        return cancelled

    async def flatten_all_positions(self) -> List[Dict[str, Any]]:
        """Market-close every open perp position."""
        from src.exchanges.hl_positions import parse_exchange_positions

        state = await self.get_user_state()
        positions = parse_exchange_positions(state)
        results: List[Dict[str, Any]] = []
        for sym, pos in positions.items():
            try:
                resp = await self.close_position(sym, pos.size)
                results.append({"symbol": sym, "response": resp, "ok": True})
            except Exception as exc:  # noqa: BLE001
                logger.error("flatten_all_positions failed for %s: %s", sym, exc)
                results.append({"symbol": sym, "error": str(exc), "ok": False})
        return results

    async def confirm_flat(self) -> bool:
        """True when user_state shows zero open perp positions."""
        from src.exchanges.hl_positions import parse_exchange_positions

        state = await self.get_user_state()
        return len(parse_exchange_positions(state)) == 0

    async def get_exchange_meta(self) -> List[Dict[str, Any]]:
        """Return the exchange metadata universe list.

        Handles both SDK 0.23+ ``{"universe": [...]}`` dicts and legacy list returns.
        """
        if self._info is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        result = await asyncio.to_thread(self._info.meta)
        if isinstance(result, dict):
            universe = result.get("universe", [])
            return list(universe) if isinstance(universe, list) else []
        return result if isinstance(result, list) else []

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        *,
        is_cross: bool = True,
    ) -> Dict[str, Any]:
        """Configure per-asset leverage and margin mode on Hyperliquid."""
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")
        fn = getattr(self._exchange, "update_leverage", None)
        if not callable(fn):
            return {"status": "unsupported"}
        info = get_symbol_info(symbol)
        max_lev = int(info.get("max_leverage", 50)) if info else 50
        eff_lev = max(1, min(int(leverage), max_lev))
        coin = resolve_wire_coin(symbol)
        async with self._action_lock:
            result = await asyncio.to_thread(fn, eff_lev, coin, bool(is_cross))
        logger.info("HL update_leverage %s lev=%dx cross=%s: %s", symbol, eff_lev, is_cross, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def schedule_cancel(self, cancel_time_ms: Optional[int]) -> Dict[str, Any]:
        """Arm or clear Hyperliquid's dead-man's switch (``scheduleCancel``).

        *cancel_time_ms* must be at least 5 seconds in the future (15s+
        recommended) or ``None`` to disarm.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")
        fn = getattr(self._exchange, "schedule_cancel", None)
        if not callable(fn):
            return {"status": "unsupported"}
        async with self._action_lock:
            result = await asyncio.to_thread(fn, cancel_time_ms)
        return result if isinstance(result, dict) else {"raw": result}

    async def set_account_abstraction(self, abstraction: str = "u") -> Dict[str, Any]:
        """Set Hyperliquid L1 account abstraction (``'u'`` Unified, ``'p'`` PM, ``'i'`` Isolated).

        Signed via ``sign_l1_action``, so API/Agent wallets can invoke this
        without exposing the master withdrawal key on the host.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")
        if abstraction not in ("u", "p", "i"):
            raise ValueError(f"Invalid HL abstraction mode: {abstraction!r}")
        fn = getattr(self._exchange, "agent_set_abstraction", None)
        if not callable(fn):
            return {"status": "unsupported"}
        async with self._action_lock:
            result = await asyncio.to_thread(fn, abstraction)
        logger.info("HL agent_set_abstraction(%s): %s", abstraction, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def transfer_usd_class(self, amount_usd: float, *, to_perp: bool) -> Dict[str, Any]:
        """Transfer USDC between Spot and Perp ledgers (requires Master Wallet key)."""
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")
        if self._account_address:
            raise PermissionError(
                "usd_class_transfer requires master-wallet EIP-712 signature; "
                "enable Unified Account ('u') when trading via HYPERLIQUID_ACCOUNT_ADDRESS agent wallet."
            )
        amt = round(safe_float(amount_usd), 2)
        if amt <= 0:
            raise ValueError(f"Invalid usd_class_transfer amount: {amount_usd}")
        fn = getattr(self._exchange, "usd_class_transfer", None)
        if not callable(fn):
            return {"status": "unsupported"}
        async with self._action_lock:
            result = await asyncio.to_thread(fn, amt, bool(to_perp))
        logger.info("HL usd_class_transfer amount=%.2f to_perp=%s: %s", amt, to_perp, result)
        return result if isinstance(result, dict) else {"raw": result}
