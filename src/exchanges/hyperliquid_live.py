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
    ) -> Dict[str, Any]:
        """Submit entry order (limit Alo or market).

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
        else:
            result = await asyncio.to_thread(
                self._exchange.market_open,
                symbol,
                is_buy,
                sz,
            )

        logger.info("HL entry response %s %s: %s", symbol, side, result)
        return result if isinstance(result, dict) else {"raw": result}

    async def close_position(
        self,
        symbol: str,
        size: float,
    ) -> Dict[str, Any]:
        """Market-close an open perp position (reduce-only by design).

        Returns the raw HL response dict.
        """
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        sz = normalize_size(symbol, safe_float(size))
        if sz <= 0:
            raise ValueError(f"Invalid close size for {symbol}: {sz}")

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

    async def get_exchange_meta(self) -> List[Dict[str, Any]]:
        """Return the exchange metadata (asset names, szDecimals, pxDecimals, …).

        Can be called before initialising the Exchange (only Info needed).
        """
        if self._info is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        result = await asyncio.to_thread(self._info.meta)
        return result if isinstance(result, list) else []
