"""
Use Case: GenerateSignal
Analisa dados de mercado usando a estratégia e persiste o sinal.
"""
from typing import Optional
from ...domain.entities import Signal
from ...domain.events import SignalGenerated
from ...domain.repositories import SignalRepository
from ...domain.services import MarketDataProvider
from ..interfaces import StrategyPort, EventPublisher, Logger
from ..dto import MarketDataDTO, SignalDTO
from .fetch_market_data import FetchMarketDataUseCase


class GenerateSignalUseCase:
    """
    Caso de uso: gerar sinal de trading.
    
    Input:  asset (str)
    Output: SignalDTO ou None
    Side Effects: persiste sinal, publica SignalGenerated event
    """
    
    def __init__(self,
                 strategy: StrategyPort,
                 signal_repo: SignalRepository,
                 publisher: EventPublisher,
                 logger: Logger,
                 fetch_use_case: FetchMarketDataUseCase = None):
        self.strategy = strategy
        self.signal_repo = signal_repo
        self.publisher = publisher
        self.logger = logger
        self.fetch_uc = fetch_use_case
    
    def execute(self, asset: str,
                market_data: MarketDataDTO = None) -> Optional[SignalDTO]:
        """
        Gera sinal para um asset.
        Se market_data não fornecido, busca automaticamente.
        """
        # Validar dados
        if not market_data or market_data.price <= 0:
            self.logger.warning(f"[GenerateSignal] Dados inválidos para {asset}")
            return None
        
        # Converter DTO para dict (interface da strategy)
        data_dict = {
            'price': market_data.price,
            'mark_price': market_data.mark_price,
            'oracle_price': market_data.oracle_price,
            'bid': market_data.bid,
            'ask': market_data.ask,
            'volume_24h': market_data.volume_24h,
            'oi_total': market_data.oi_usd,
            'oi_change_pct': market_data.oi_change_pct,
            'funding_rate': market_data.funding_rate,
            'funding_avg': market_data.funding_avg,
            'volume_ratio': market_data.volume_ratio,
            'timestamp': market_data.timestamp,
        }
        
        # Analisar
        result = self.strategy.analyze(data_dict, market_data.price)
        
        if not result or result.get('type') in ('HOLD', 'EXIT'):
            return None
        
        # Criar entidade de domínio
        signal = Signal(
            asset=asset,
            direction=result['type'].lower(),
            confidence=result.get('confidence', 1.0),
            entry_price=result.get('entry_price', market_data.price),
            stop_loss=result.get('stop_loss'),
            take_profit=result.get('take_profit'),
            reason=result.get('reason', ''),
            metadata=result.get('metadata', {})
        )
        
        # Persistir
        self.signal_repo.save(signal)
        
        # Publicar evento
        event = SignalGenerated(
            asset=signal.asset,
            direction=signal.direction,
            confidence=signal.confidence,
            entry_price=signal.entry_price,
            reason=signal.reason,
            metadata=signal.metadata
        )
        self.publisher.publish(event)
        
        self.logger.info(
            f"[GenerateSignal] {signal.direction.upper()} {asset} @ ${signal.entry_price:,.2f}"
        )
        
        return SignalDTO(
            asset=signal.asset,
            direction=signal.direction,
            confidence=signal.confidence,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            reason=signal.reason,
            timestamp=signal.timestamp
        )
