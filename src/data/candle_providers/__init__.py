"""Abstract candle providers for Hyperliquid venue history (Phase Data Provider)."""

from src.data.candle_providers.base import (
    CandlePage,
    CandleProvider,
    CandleProviderError,
    ProviderName,
)
from src.data.candle_providers.goldrush_hypercore import GoldrushHypercoreCandleProvider
from src.data.candle_providers.hyperliquid_public import HyperliquidPublicCandleProvider
from src.data.candle_providers.node_trades_fetcher import (
    FakeNodeTradesFetcher,
    NodeTradesFetcher,
    S3NodeTradesFetcher,
    archive_keys_for_window,
)
from src.data.candle_providers.node_trades_rebuild import (
    RebuildResult,
    RebuildWindow,
    extract_priority_windows,
    plan_object_keys,
    rebuild_from_support_package,
    rebuild_window,
)

__all__ = [
    "CandlePage",
    "CandleProvider",
    "CandleProviderError",
    "ProviderName",
    "GoldrushHypercoreCandleProvider",
    "HyperliquidPublicCandleProvider",
    "FakeNodeTradesFetcher",
    "NodeTradesFetcher",
    "S3NodeTradesFetcher",
    "archive_keys_for_window",
    "RebuildResult",
    "RebuildWindow",
    "extract_priority_windows",
    "plan_object_keys",
    "rebuild_from_support_package",
    "rebuild_window",
]
