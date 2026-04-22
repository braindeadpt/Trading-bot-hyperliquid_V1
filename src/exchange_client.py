"""
Cliente Hyperliquid - execução de ordens
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Cliente para interagir com a Hyperliquid
    V1: Paper trading (simulação) — NÃO executa ordens reais
    """
    
    def __init__(self, config: Dict, paper_trading: bool = True):
        self.config = config
        self.paper_trading = paper_trading
        self.base_url = config['data_sources']['hyperliquid']['base_url']
        
        logger.info(f"HyperliquidClient iniciado — Paper Trading: {paper_trading}")
    
    def place_order(self, asset: str, side: str, size: float, price: float = None) -> Dict:
        """
        Coloca uma ordem (simulada em paper trading)
        
        Args:
            asset: BTC, ETH, etc.
            side: 'BUY' ou 'SELL'
            size: Tamanho em USD
            price: Preço limite (None = market order)
        """
        if self.paper_trading:
            logger.info(
                f"🧪 PAPER TRADE | {side} {asset} | ${size:.2f} | "
                f"Preço: ${price:,.2f if price else 'MARKET'}"
            )
            return {
                'status': 'PAPER_FILLED',
                'asset': asset,
                'side': side,
                'size': size,
                'price': price or 0,
                'order_id': f'paper_{hash(f"{asset}{side}{size}")}'
            }
        
        # TODO: Implementar execução real com wallet + assinatura
        logger.error("Execução real ainda não implementada!")
        raise NotImplementedError("Execução real requer wallet e assinatura criptográfica")
    
    def close_position(self, asset: str) -> Dict:
        """Fecha posição aberta"""
        if self.paper_trading:
            logger.info(f"🧪 PAPER CLOSE | {asset}")
            return {'status': 'PAPER_CLOSED', 'asset': asset}
        
        raise NotImplementedError()
    
    def get_balance(self) -> Dict:
        """Retorna saldo da conta (simulado)"""
        if self.paper_trading:
            return {
                'USDC': 10000.0,  # Saldo fictício para paper trading
                'status': 'paper'
            }
        
        # TODO: Buscar saldo real
        return {}
