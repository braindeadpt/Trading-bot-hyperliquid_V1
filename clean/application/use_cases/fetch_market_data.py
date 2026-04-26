"""
Use Case: FetchMarketData
Busca dados de mercado através do provider e publica evento de domínio.
"""
from typing import Optional
from ...domain.entities import MarketSnapshot
from ...domain.events import MarketDataUpdated
from ...domain.services import MarketDataProvider
from ..interfaces import EventPublisher, Logger
from ..dto import MarketDataDTO


class FetchMarketDataUseCase:
    """
    Caso de uso: buscar dados de mercado.
    
    Input:  asset (str)
    Output: MarketDataDTO ou None
    Side Effects: publica MarketDataUpdated event
    """
    
    def __init__(self,
                 provider: MarketDataProvider,
                 publisher: EventPublisher,
                 logger: Logger):
        self.provider = provider
        self.publisher = publisher
        self.logger = logger
    
    def execute(self, asset: str) -> Optional[MarketDataDTO]:
        """Busca dados de mercado para um asset."""
        snapshot = self.provider.get_snapshot(asset)
        
        if not snapshot:
            self.logger.warning(f"[FetchMarketData] Sem dados para {asset}")
            return None
        
        # Publicar evento de domínio
        event = MarketDataUpdated(
            asset=snapshot.asset,
            price=snapshot.price,
            oi=snapshot.oi_usd,
            funding=snapshot.funding_rate,
            volume=snapshot.volume_24h,
            source=snapshot.source
        )
        self.publisher.publish(event)
        
        # Retornar DTO
        return MarketDataDTO(
            asset=snapshot.asset,
            price=snapshot.price,
            mark_price=snapshot.mark_price,
            oracle_price=snapshot.oracle_price,
            bid=snapshot.bid,
            ask=snapshot.ask,
            volume_24h=snapshot.volume_24h,
            oi_usd=snapshot.oi_usd,
            oi_change_pct=snapshot.oi_change_pct,
            funding_rate=snapshot.funding_rate,
            funding_avg=snapshot.funding_avg,
            volume_ratio=snapshot.volume_ratio,
            timestamp=snapshot.timestamp,
            source=snapshot.source
        )
