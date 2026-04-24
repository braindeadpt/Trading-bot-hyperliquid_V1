"""
Cliente Hyperliquid - execução de ordens
V1: Paper trading (simulação) — NÃO executa ordens reais
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Cliente para interagir com a Hyperliquid
    V1: Paper trading (simulação) — NÃO executa ordens reais
    """
    
    # ⚡ CONSTANTES DE VALIDAÇÃO
    MIN_ORDER_SIZE_USD = 10.0       # Mínimo $10 por ordem
    MAX_ORDER_SIZE_USD = 100000.0   # Máximo $100k por ordem
    MAX_SLIPPAGE_PCT = 0.005        # 0.5% slippage máximo
    MAX_PRICE_DEVIATION_PCT = 0.02  # 2% desvio máximo do preço de mercado
    
    def __init__(self, config: Dict, paper_trading: bool = True):
        self.config = config
        self.paper_trading = paper_trading
        self.base_url = config.get('data_sources', {}).get('hyperliquid', {}).get('base_url', 'https://api.hyperliquid.xyz')
        
        logger.info(f"HyperliquidClient iniciado — Paper Trading: {paper_trading}")
    
    def _validate_order(self, asset: str, side: str, size: float, price: float = None, market_price: float = None) -> tuple:
        """
        ⚡ VALIDAÇÃO DE ORDENS — Verifica se a ordem é segura antes de enviar.
        Retorna (is_valid: bool, reason: str)
        """
        # 1. Tamanho mínimo
        if size < self.MIN_ORDER_SIZE_USD:
            return False, f"Tamanho mínimo é ${self.MIN_ORDER_SIZE_USD} (recebido ${size:.2f})"
        
        # 2. Tamanho máximo
        if size > self.MAX_ORDER_SIZE_USD:
            return False, f"Tamanho máximo é ${self.MAX_ORDER_SIZE_USD} (recebido ${size:.2f})"
        
        # 3. Side válido
        if side not in ('BUY', 'SELL'):
            return False, f"Side inválido: {side} (deve ser BUY ou SELL)"
        
        # 4. Preço de mercado conhecido
        if market_price is None or market_price <= 0:
            return False, "Preço de mercado inválido — não posso validar ordem"
        
        # 5. Desvio de preço (se ordem limite)
        if price is not None:
            deviation = abs(price - market_price) / market_price
            if deviation > self.MAX_PRICE_DEVIATION_PCT:
                return False, f"Desvio de preço excessivo: {deviation*100:.2f}% > {self.MAX_PRICE_DEVIATION_PCT*100:.1f}%"
        
        # 6. Margem suficiente (paper trading)
        if self.paper_trading:
            initial_capital = self.config.get('risk', {}).get('initial_capital', 10000.0)
            max_leverage = self.config.get('risk', {}).get('max_leverage', 2)
            margin_used = size / max_leverage
            if margin_used > initial_capital * 0.95:  # Deixa 5% de buffer
                return False, f"Margem insuficiente: ${margin_used:.2f} > ${initial_capital * 0.95:.2f}"
        
        return True, "OK"
    
    def place_order(self, asset: str, side: str, size: float, price: float = None, market_price: float = None) -> Dict:
        """
        Coloca uma ordem (simulada em paper trading)
        
        Args:
            asset: BTC, ETH, etc.
            side: 'BUY' ou 'SELL'
            size: Tamanho em USD
            price: Preço limite (None = market order)
            market_price: Preço de mercado actual (para validação)
        """
        # ⚡ VALIDAÇÃO PRÉ-ORDEM
        is_valid, reason = self._validate_order(asset, side, size, price, market_price)
        if not is_valid:
            logger.error(f"🚫 ORDEM REJEITADA: {reason}")
            return {
                'status': 'REJECTED',
                'reason': reason,
                'asset': asset,
                'side': side,
                'size': size,
            }
        
        if self.paper_trading:
            price_str = f"${price:,.2f}" if price else "MARKET"
            logger.info(
                f"🧪 PAPER TRADE | {side} {asset} | ${size:.2f} | "
                f"Preço: {price_str}"
            )
            return {
                'status': 'PAPER_FILLED',
                'asset': asset,
                'side': side,
                'size': size,
                'price': price or market_price or 0,
                'order_id': f'paper_{hash(f'{asset}{side}{size}')}'
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
            initial_capital = self.config.get('risk', {}).get('initial_capital', 10000.0)
            return {
                'USDC': initial_capital,
                'status': 'paper'
            }
        
        # TODO: Buscar saldo real
        return {}
    
    def place_stop_loss_order(self, asset: str, side: str, size: float, stop_price: float, market_price: float = None) -> Dict:
        """
        ⚡ STOP-LOSS NA EXCHANGE — Coloca stop-loss order na exchange.
        Em paper trading: simula.
        Em real trading: envia stop-loss order à Hyperliquid.
        """
        # Validar
        is_valid, reason = self._validate_order(asset, side, size, stop_price, market_price)
        if not is_valid:
            logger.error(f"🚫 STOP-LOSS REJEITADO: {reason}")
            return {'status': 'REJECTED', 'reason': reason}
        
        if self.paper_trading:
            logger.info(f"🧪 PAPER STOP-LOSS | {side} {asset} | Stop: ${stop_price:,.2f}")
            return {
                'status': 'PAPER_STOP_SET',
                'asset': asset,
                'side': side,
                'size': size,
                'stop_price': stop_price,
                'order_id': f'paper_sl_{hash(f'{asset}{side}{stop_price}')}'
            }
        
        # TODO: Enviar stop-loss order real à Hyperliquid
        raise NotImplementedError("Stop-loss real ainda não implementado")
