"""Hyperliquid live order client — EIP-712 signing via official SDK."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

from src.security.vault import Vault, VaultKeyError, VaultNotInitializedError, get_vault
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

VAULT_KEY = "hyperliquid_private_key"
ENV_KEY = "HYPERLIQUID_PRIVATE_KEY"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"

_KEY_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


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


class HyperliquidLiveClient:
    """Thin async wrapper around hyperliquid-python-sdk Exchange."""

    def __init__(self, private_key: str, *, use_testnet: bool = True) -> None:
        self._private_key = normalize_private_key(private_key)
        self._use_testnet = use_testnet
        self._exchange: Any = None
        self._wallet_address: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self._exchange is not None

    @property
    def wallet_address(self) -> Optional[str]:
        return self._wallet_address

    def initialize(self) -> None:
        """Create SDK Exchange (blocking — call via asyncio.to_thread)."""
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        wallet = Account.from_key(self._private_key)
        base_url = TESTNET_URL if self._use_testnet else MAINNET_URL
        self._exchange = Exchange(wallet, base_url=base_url)
        self._wallet_address = wallet.address
        logger.info(
            "HyperliquidLiveClient ready mode=%s wallet=%s",
            "testnet" if self._use_testnet else "mainnet",
            self._wallet_address,
        )

    async def open(self) -> None:
        await asyncio.to_thread(self.initialize)

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
        """Submit entry order (limit Alo or market)."""
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        is_buy = side == "long"
        sz = safe_float(size)
        if sz <= 0:
            raise ValueError(f"Invalid order size for {symbol}: {sz}")

        if order_type == "limit_maker":
            px = safe_float(limit_price)
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
        """Market-close an open perp position."""
        if self._exchange is None:
            raise RuntimeError("HyperliquidLiveClient not initialized")

        sz = safe_float(size)
        if sz <= 0:
            raise ValueError(f"Invalid close size for {symbol}: {sz}")

        result = await asyncio.to_thread(
            self._exchange.market_close,
            symbol,
            sz,
        )
        logger.info("HL close response %s: %s", symbol, result)
        return result if isinstance(result, dict) else {"raw": result}
