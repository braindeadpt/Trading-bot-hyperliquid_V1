"""Hyperliquid live order client — EIP-712 signing via official SDK.

Provides a thin async wrapper around the ``hyperliquid-python-sdk``
(``hyperliquid.exchange.Exchange`` for signed orders,
``hyperliquid.info.Info`` for read-only queries).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.security.vault import Vault, VaultKeyError, VaultNotInitializedError, get_vault
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

VAULT_KEY = "hyperliquid_private_key"
ENV_KEY = "HYPERLIQUID_PRIVATE_KEY"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"

_KEY_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

# Cache of asset metadata: symbol → {sz_decimals, px_decimals}
_meta_cache: Dict[str, Dict[str, int]] = {}


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


# ═══════════════════════════════════════════════════════════════
#  Asset metadata helpers
# ═══════════════════════════════════════════════════════════════

def build_meta_cache(info: Any) -> Dict[str, Dict[str, int]]:
    """Build a {symbol → {sz_decimals, px_decimals}} map from Info.meta().

    Call once after creating the Info client; results are cached in the
    module-level ``_meta_cache`` dict.
    """
    raw = info.meta()
    if not isinstance(raw, (list, tuple)):
        return {}
    cache: Dict[str, Dict[str, int]] = {}
    for entry in raw:
        name = entry.get("name", "")
        if name:
            cache[name] = {
                "sz_decimals": int(entry.get("szDecimals", 0)),
                "px_decimals": int(entry.get("pxDecimals", 0)),
            }
    _meta_cache.update(cache)
    return cache


def get_symbol_info(symbol: str) -> Optional[Dict[str, int]]:
    """Return cached metadata for *symbol*, or None if not found."""
    return _meta_cache.get(symbol)


def normalize_size(symbol: str, size: float) -> float:
    """Round *size* to the correct number of decimals for *symbol*."""
    info = get_symbol_info(symbol)
    if info is None:
        return size
    decimals = info["sz_decimals"]
    factor = 10 ** decimals
    return math.floor(size * factor) / factor


def normalize_price(symbol: str, price: float) -> float:
    """Round *price* to the correct number of decimals (tick size) for *symbol*."""
    info = get_symbol_info(symbol)
    if info is None:
        return price
    decimals = info["px_decimals"]
    factor = 10 ** decimals
    return round(price * factor) / factor


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

    def __init__(self, private_key: str, *, use_testnet: bool = True) -> None:
        self._private_key = normalize_private_key(private_key)
        self._use_testnet = use_testnet
        self._exchange: Any = None
        self._info: Any = None
        self._wallet_address: Optional[str] = None

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
        self._exchange = Exchange(wallet, base_url=base_url)
        self._info = Info(base_url=base_url, skip_ws=True)
        self._wallet_address = wallet.address
        # Warm the meta cache for symbol normalisation
        build_meta_cache(self._info)
        logger.info(
            "HyperliquidLiveClient ready mode=%s wallet=%s",
            "testnet" if self._use_testnet else "mainnet",
            self._wallet_address,
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
    ) -> Dict[str, Any]:
        """Submit entry order (limit Alo, capped aggressive limit, or SDK market).

        Returns the raw HL response dict.  Raises on invalid params.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        is_buy = side == "long"
        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid order size for {symbol}: {sz}")

        if order_type == "limit_maker":
            px = normalize_price(symbol, safe_float(limit_price))
            if px <= 0:
                raise ValueError(f"Invalid limit price for {symbol}: {px}")
            tif = "Alo" if post_only else "Gtc"
            hl_order_type = {"limit": {"tif": tif}}
            result = await asyncio.to_thread(
                self._exchange.order,
                symbol,
                is_buy,
                sz,
                px,
                hl_order_type,
            )
        elif order_type == "limit_slippage_cap":
            ref = safe_float(limit_price)
            if ref <= 0:
                raise ValueError(
                    f"limit_slippage_cap requires reference price for {symbol}"
                )
            result = await self.place_aggressive_limit(
                symbol,
                side,
                sz,
                reference_price=ref,
                max_slippage_pct=max_slippage_pct,
                reduce_only=False,
            )
        else:
            result = await asyncio.to_thread(
                self._exchange.market_open,
                symbol,
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

        hl_order_type = {"limit": {"tif": tif if tif in ("Ioc", "Gtc", "Alo") else "Ioc"}}
        result = await asyncio.to_thread(
            self._exchange.order,
            symbol,
            is_buy,
            sz,
            px,
            hl_order_type,
            bool(reduce_only),
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
            result = await asyncio.to_thread(
                self._exchange.market_close,
                symbol,
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

        result = await asyncio.to_thread(
            self._exchange.cancel,
            symbol,
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
        """Return the list of open orders for the connected wallet.

        Requires the client to be initialised.
        """
        if self._info is None or self._wallet_address is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        result = await asyncio.to_thread(
            self._info.open_orders,
            self._wallet_address,
        )
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

        result = await asyncio.to_thread(
            self._exchange.order,
            symbol,
            is_buy,
            sz,
            px,
            order_type,
            True,  # reduce_only
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
        """Return the exchange metadata (asset names, szDecimals, pxDecimals, …).

        Can be called before initialising the Exchange (only Info needed).
        """
        if self._info is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        result = await asyncio.to_thread(self._info.meta)
        return result if isinstance(result, list) else []
