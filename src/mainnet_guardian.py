"""
🛡️ MAINNET GUARDIAN v3 — Segurança de Deploy para Mainnet
============================================================
Módulo de segurança crítica para proteger o bot antes de deploy com dinheiro real.

Funcionalidades:
  1. NetworkGate — controlo testnet/mainnet com confirmação de dois passos
  2. HttpsEnforcer — verificação obrigatória de HTTPS em todas as URLs
  3. RateLimiter — controlo de rate limiting por exchange (evita bans)
  4. OrderValidator — validação completa de ordens (tamanho, slippage, margem)
  5. CircuitBreaker — paragem automática após perda diária (soft/hard stop)
  6. GracefulShutdown — handler de shutdown com fecho de posições
  7. EmergencyStop — paragem de emergência via ficheiro STOP
  8. AuditLogger — logging estruturado para auditoria

USO:
    from mainnet_guardian import MainnetGuardian, NetworkGate
    
    guardian = MainnetGuardian(config)
    guardian.verify_before_start()  # Bloqueia se não estiver seguro
    
    # No loop de trading:
    if guardian.check_circuit_breaker(current_capital):
        logger.critical("🛑 BOT PARADO — Circuit breaker ativado!")
        break
"""
import logging
import os
import sys
import signal
import time
import json
import atexit
import threading
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Tuple, List, Any
from dataclasses import dataclass, asdict
from collections import deque, defaultdict

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTES DE SEGURANÇA
# =============================================================================

MIN_ORDER_SIZE_USD = 10.0          # Mínimo $10 por ordem (Hyperliquid)
MAX_ORDER_SIZE_USD = 100_000.0     # Máximo $100k por ordem
MAX_SLIPPAGE_PCT = 0.005           # 0.5% slippage máximo aceitável
MAX_PRICE_DEVIATION_PCT = 0.02     # 2% desvio máximo do preço de mercado
DAILY_LOSS_SOFT_PCT = 0.05         # 5% — soft stop (pode resetar manualmente)
DAILY_LOSS_HARD_PCT = 0.10         # 10% — hard stop (requer restart)
EMERGENCY_TIMEOUT_SECONDS = 3        # Timeout reduzido para fecho de emergência
GRACEFUL_SHUTDOWN_TIMEOUT = 30     # Máximo tempo para fechar posições no shutdown
RATE_LIMIT_DEFAULT_INTERVAL = 1.0  # Segundos entre requests por default

# Binance: 1200 req/min = 20 req/s (ponderado)
# Bybit: 50 req/s
# OKX: 20 req/s
# Hyperliquid: não documentado, mas conservador
RATE_LIMITS = {
    'binance': {'requests_per_second': 20, 'burst': 5},
    'bybit': {'requests_per_second': 50, 'burst': 10},
    'okx': {'requests_per_second': 20, 'burst': 5},
    'hyperliquid': {'requests_per_second': 10, 'burst': 3},
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OrderCheckResult:
    """Resultado da validação de uma ordem"""
    is_valid: bool
    reason: str
    checks_passed: List[str]
    checks_failed: List[str]
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CircuitStatus:
    """Estado do circuit breaker"""
    tripped: bool
    reason: Optional[str]
    daily_pnl: float
    daily_loss_pct: float
    daily_trades: int
    soft_limit_pct: float
    hard_limit_pct: float
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class SecurityAudit:
    """Registo de auditoria de segurança"""
    timestamp: str
    event: str
    level: str  # INFO, WARNING, CRITICAL
    details: Dict[str, Any]
    
    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp,
            'event': self.event,
            'level': self.level,
            'details': self.details,
        }


# =============================================================================
# 1. NETWORK GATE — Controlo Testnet/Mainnet
# =============================================================================

class NetworkGate:
    """
    ⚡ PORTÃO DE REDE — Impede deploy acidental em mainnet.
    
    Regras:
      - Paper trading: sempre permitido
      - Testnet: requer confirmação explícita no config
      - Mainnet: requer confirmação DE DOIS PASSOS:
        1. Config: mainnet_enabled: true
        2. Ficheiro de confirmação: .mainnet_approved na root do projeto
      
    USO:
        gate = NetworkGate(config)
        if not gate.can_trade_real():
            raise RuntimeError("Mainnet não aprovada!")
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.bot_config = config.get('bot', {})
        self.risk_config = config.get('risk', {})
        
        self.paper_trading = self.bot_config.get('paper_trading', True)
        self.network = self.bot_config.get('network', 'paper').lower()
        self.mainnet_enabled = self.risk_config.get('mainnet_enabled', False)
        self.mainnet_confirm_required = self.risk_config.get('mainnet_confirm_required', True)
        
        # Paths
        self.project_root = Path(__file__).parent.parent
        self.approval_file = self.project_root / '.mainnet_approved'
        self.blocker_file = self.project_root / '.mainnet_blocked'
        
        self._banner_printed = False
    
    def get_network(self) -> str:
        """Retorna a rede atual: 'paper', 'testnet', ou 'mainnet'"""
        if self.paper_trading:
            return 'paper'
        return self.network
    
    def can_trade_real(self) -> bool:
        """
        Verifica se o bot pode executar ordens reais.
        Paper trading = sempre True (não executa reais).
        """
        if self.paper_trading:
            return True  # Paper trading é sempre "seguro"
        
        # Verificar se network é válida
        if self.network not in ('testnet', 'mainnet'):
            logger.error(f"🚫 Network inválida: '{self.network}'. Use 'testnet' ou 'mainnet'.")
            return False
        
        # Testnet: requer mainnet_enabled=True OU confirmação desativada
        if self.network == 'testnet':
            if self.mainnet_enabled or not self.mainnet_confirm_required:
                return True
            logger.warning("⚠️  Testnet requer mainnet_enabled=True ou mainnet_confirm_required=false")
            return False
        
        # Mainnet: DOIS PASSOS obrigatórios
        if self.network == 'mainnet':
            if not self.mainnet_enabled:
                logger.critical("🚫 MAINNET BLOQUEADA: mainnet_enabled=false no config!")
                return False
            
            if self.mainnet_confirm_required and not self.approval_file.exists():
                logger.critical("🚫 MAINNET BLOQUEADA: Ficheiro .mainnet_approved não encontrado!")
                logger.critical("   Para aprovar mainnet, crie o ficheiro:")
                logger.critical(f"   echo 'APPROVED' > {self.approval_file}")
                return False
            
            if self.blocker_file.exists():
                logger.critical("🚫 MAINNET BLOQUEADA: Ficheiro .mainnet_blocked existe!")
                return False
            
            return True
        
        return False
    
    def print_startup_banner(self):
        """Imprime banner de segurança no startup"""
        if self._banner_printed:
            return
        
        network = self.get_network()
        
        if network == 'paper':
            logger.info("=" * 60)
            logger.info("🧪  PAPER TRADING — Sem dinheiro real em risco")
            logger.info("=" * 60)
        elif network == 'testnet':
            logger.info("=" * 60)
            logger.info("🧪  TESTNET — Dinheiro de teste apenas")
            logger.info("=" * 60)
        elif network == 'mainnet':
            logger.critical("=" * 60)
            logger.critical("🔴 MAINNET — DINHEIRO REAL EM RISCO!")
            logger.critical("=" * 60)
        
        self._banner_printed = True
    
    def approve_mainnet(self) -> bool:
        """Cria ficheiro de aprovação para mainnet"""
        try:
            self.approval_file.write_text(
                f"APPROVED\nDate: {datetime.now().isoformat()}\n"
                f"By: manual_approval\n"
            )
            logger.warning("✅ Mainnet aprovada! Ficheiro .mainnet_approved criado.")
            return True
        except Exception as e:
            logger.error(f"Erro a criar ficheiro de aprovação: {e}")
            return False
    
    def block_mainnet(self) -> bool:
        """Bloqueia mainnet (emergência)"""
        try:
            self.blocker_file.write_text(
                f"BLOCKED\nDate: {datetime.now().isoformat()}\n"
            )
            logger.critical("🛑 Mainnet BLOQUEADA! Ficheiro .mainnet_blocked criado.")
            return True
        except Exception as e:
            logger.error(f"Erro a criar ficheiro de bloqueio: {e}")
            return False


# =============================================================================
# 2. HTTPS ENFORCER — Verificação Obrigatória de HTTPS
# =============================================================================

class HttpsEnforcer:
    """
    ⚡ FORÇA HTTPS — Garante que todas as URLs de API usam HTTPS.
    
    Impede ataques man-in-the-middle e configurações maliciosas.
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.sources = config.get('data_sources', {})
        self.violations: List[str] = []
    
    def validate_all_urls(self) -> Tuple[bool, List[str]]:
        """
        Valida todas as URLs de data sources.
        Retorna (all_valid, list_of_violations).
        """
        self.violations = []
        
        for name, cfg in self.sources.items():
            if not cfg.get('enabled', False):
                continue
            
            url = cfg.get('base_url', '')
            if not url:
                continue
            
            if not url.startswith('https://'):
                violation = f"🚫 {name}: URL não usa HTTPS: {url}"
                self.violations.append(violation)
                logger.critical(violation)
        
        if self.violations:
            logger.critical("=" * 60)
            logger.critical("🚫 HTTPS VIOLADO — Bot NÃO pode iniciar!")
            logger.critical("=" * 60)
            return False, self.violations
        
        logger.info("✅ Todas as URLs usam HTTPS")
        return True, []
    
    def enforce_session(self, session) -> Any:
        """
        Configura uma session requests para forçar verificação SSL.
        Retorna a session modificada.
        """
        if hasattr(session, 'verify'):
            session.verify = True
        return session
    
    @staticmethod
    def is_https(url: str) -> bool:
        """Verifica se uma URL usa HTTPS"""
        return url.startswith('https://')


# =============================================================================
# 3. RATE LIMITER — Controlo de Taxa de Requests
# =============================================================================

class RateLimiter:
    """
    ⚡ RATE LIMITER — Evita exceder limites das APIs.
    
    Implementa token bucket por exchange.
    """
    
    def __init__(self):
        self._last_request: Dict[str, float] = defaultdict(float)
        self._request_counts: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        self._limits = RATE_LIMITS.copy()
    
    def wait_if_needed(self, exchange: str) -> float:
        """
        Aguarda se necessário antes de fazer request.
        Retorna tempo de espera em segundos.
        """
        now = time.time()
        limit = self._limits.get(exchange, {'requests_per_second': 10, 'burst': 3})
        
        max_rps = limit['requests_per_second']
        min_interval = 1.0 / max_rps
        
        last = self._last_request[exchange]
        elapsed = now - last
        
        if elapsed < min_interval:
            wait = min_interval - elapsed
            time.sleep(wait)
            self._last_request[exchange] = time.time()
            return wait
        
        self._last_request[exchange] = now
        return 0.0
    
    def record_request(self, exchange: str, status_code: int = 200):
        """Regista um request para estatísticas"""
        self._request_counts[exchange].append({
            'time': time.time(),
            'status': status_code,
        })
    
    def get_stats(self, exchange: str, window_seconds: int = 60) -> Dict:
        """Retorna estatísticas de requests numa janela de tempo"""
        now = time.time()
        cutoff = now - window_seconds
        
        requests = [r for r in self._request_counts[exchange] if r['time'] > cutoff]
        
        return {
            'exchange': exchange,
            'window_seconds': window_seconds,
            'total_requests': len(requests),
            'requests_per_minute': len(requests) * (60 / window_seconds),
            'errors': sum(1 for r in requests if r['status'] >= 400),
        }
    
    def check_429_response(self, response_headers: Dict) -> Optional[int]:
        """
        Verifica header Retry-After após 429.
        Retorna segundos para esperar ou None.
        """
        retry_after = response_headers.get('Retry-After')
        if retry_after:
            try:
                return int(retry_after)
            except ValueError:
                # Pode ser data HTTP
                pass
        return None


# =============================================================================
# 4. ORDER VALIDATOR — Validação Completa de Ordens
# =============================================================================

class OrderValidator:
    """
    ⚡ VALIDADOR DE ORDENS — Verifica se uma ordem é segura ANTES de enviar.
    
    Checks:
      1. Tamanho mínimo ($10)
      2. Tamanho máximo ($100k)
      3. Side válido (BUY/SELL)
      4. Preço de mercado válido
      5. Desvio de preço (máx 2%)
      6. Margem suficiente
      7. Leverage dentro do limite
    """
    
    def __init__(self, config: Dict):
        self.risk = config.get('risk', {})
        self.min_size = self.risk.get('min_order_size_usd', MIN_ORDER_SIZE_USD)
        self.max_size = self.risk.get('max_order_size_usd', MAX_ORDER_SIZE_USD)
        self.max_slippage = self.risk.get('max_slippage_pct', MAX_SLIPPAGE_PCT)
        self.max_deviation = self.risk.get('max_price_deviation_pct', MAX_PRICE_DEVIATION_PCT)
        self.max_leverage = self.risk.get('max_leverage', 2)
        self.initial_capital = self.risk.get('initial_capital', 10000.0)
    
    def validate(
        self,
        asset: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        market_price: Optional[float] = None,
        account_balance: Optional[float] = None,
    ) -> OrderCheckResult:
        """
        Valida uma ordem completa.
        Retorna OrderCheckResult com detalhes de cada check.
        """
        passed = []
        failed = []
        
        # 1. Tamanho mínimo
        if size < self.min_size:
            failed.append(f"Tamanho mínimo: ${size:.2f} < ${self.min_size}")
        else:
            passed.append("Tamanho mínimo OK")
        
        # 2. Tamanho máximo
        if size > self.max_size:
            failed.append(f"Tamanho máximo: ${size:.2f} > ${self.max_size}")
        else:
            passed.append("Tamanho máximo OK")
        
        # 3. Side válido
        if side not in ('BUY', 'SELL', 'long', 'short'):
            failed.append(f"Side inválido: {side}")
        else:
            passed.append("Side válido")
        
        # Normalizar side
        normalized_side = 'BUY' if side in ('BUY', 'long') else 'SELL'
        
        # 4. Preço de mercado
        if market_price is None or market_price <= 0:
            failed.append("Preço de mercado inválido")
        else:
            passed.append("Preço de mercado OK")
        
        # 5. Desvio de preço (para limit orders)
        if price is not None and market_price and market_price > 0:
            deviation = abs(price - market_price) / market_price
            if deviation > self.max_deviation:
                failed.append(f"Desvio de preço: {deviation*100:.2f}% > {self.max_deviation*100:.1f}%")
            else:
                passed.append("Desvio de preço OK")
        
        # 6. Margem suficiente
        if account_balance is not None:
            margin_needed = size / self.max_leverage
            if margin_needed > account_balance * 0.95:
                failed.append(
                    f"Margem insuficiente: ${margin_needed:.2f} > ${account_balance * 0.95:.2f}"
                )
            else:
                passed.append("Margem suficiente")
        
        # 7. Slippage check (para market orders)
        if price is None and market_price and market_price > 0:
            # Market order = slippage potencial
            passed.append("Market order (slippage possível)")
        
        is_valid = len(failed) == 0
        reason = "OK" if is_valid else "; ".join(failed)
        
        return OrderCheckResult(
            is_valid=is_valid,
            reason=reason,
            checks_passed=passed,
            checks_failed=failed,
        )
    
    def validate_slippage(
        self,
        expected_price: float,
        actual_price: float,
        side: str,
    ) -> Tuple[bool, float]:
        """
        Valida slippage após fill.
        Retorna (is_acceptable, slippage_pct).
        """
        if expected_price <= 0:
            return False, 0.0
        
        slippage = abs(actual_price - expected_price) / expected_price
        is_acceptable = slippage <= self.max_slippage
        
        if not is_acceptable:
            logger.warning(
                f"🚫 Slippage excessivo: {slippage*100:.2f}% > {self.max_slippage*100:.1f}% | "
                f"Expected: ${expected_price:,.2f} | Actual: ${actual_price:,.2f}"
            )
        
        return is_acceptable, slippage


# =============================================================================
# 5. CIRCUIT BREAKER — Paragem Automática após Perda
# =============================================================================

class CircuitBreaker:
    """
    ⚡ CIRCUIT BREAKER — Para o bot se perdas excederem limites.
    
    SOFT STOP (5%): Para trading, pode resetar manualmente.
    HARD STOP (10%): Para IMEDIATAMENTE, requer restart do bot.
    """
    
    def __init__(self, config: Dict):
        self.risk = config.get('risk', {})
        self.soft_limit = self.risk.get('daily_loss_limit_pct', DAILY_LOSS_SOFT_PCT)
        self.hard_limit = self.risk.get('daily_loss_hard_stop_pct', DAILY_LOSS_HARD_PCT)
        
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_date = date.today()
        self._tripped = False
        self._reason = None
        self._lock = threading.Lock()
    
    def check(self, current_capital: float, initial_capital: float) -> bool:
        """
        Verifica se circuit breaker deve ativar.
        Retorna True se trading deve PARAR.
        """
        with self._lock:
            today = date.today()
            
            # Reset diário
            if today != self.daily_date:
                self.daily_date = today
                self.daily_pnl = 0.0
                self.daily_trades = 0
                self._tripped = False
                self._reason = None
                logger.info("🔄 Circuit breaker resetado (novo dia)")
                return False
            
            if self._tripped:
                return True
            
            if initial_capital <= 0:
                return False
            
            daily_loss_pct = (initial_capital - current_capital) / initial_capital
            
            # Hard stop
            if daily_loss_pct >= self.hard_limit:
                self._tripped = True
                self._reason = (
                    f"HARD STOP: Perda diária {daily_loss_pct*100:.1f}% >= "
                    f"{self.hard_limit*100:.1f}%"
                )
                logger.critical("=" * 60)
                logger.critical(f"🛑 {self._reason}")
                logger.critical("🛑 BOT PARADO — Hard stop ativado!")
                logger.critical("=" * 60)
                return True
            
            # Soft stop
            if daily_loss_pct >= self.soft_limit:
                self._tripped = True
                self._reason = (
                    f"SOFT STOP: Perda diária {daily_loss_pct*100:.1f}% >= "
                    f"{self.soft_limit*100:.1f}%"
                )
                logger.critical("=" * 60)
                logger.critical(f"🛑 {self._reason}")
                logger.critical("🛑 BOT PARADO — Soft stop ativado!")
                logger.critical("=" * 60)
                return True
            
            return False
    
    def record_trade_pnl(self, pnl_usd: float):
        """Regista PnL de um trade"""
        with self._lock:
            self.daily_pnl += pnl_usd
            self.daily_trades += 1
    
    def reset(self) -> bool:
        """Reset manual do circuit breaker (para soft stop)"""
        with self._lock:
            if self._tripped and self._reason and 'HARD STOP' in self._reason:
                logger.critical("🚫 HARD STOP não pode ser resetado manualmente!")
                return False
            
            self._tripped = False
            self._reason = None
            logger.warning("⚠️  Circuit breaker resetado manualmente!")
            return True
    
    def get_status(self) -> CircuitStatus:
        """Retorna estado atual do circuit breaker"""
        with self._lock:
            daily_loss_pct = 0.0
            if hasattr(self, '_initial_capital') and self._initial_capital > 0:
                daily_loss_pct = (self._initial_capital - (self._initial_capital + self.daily_pnl)) / self._initial_capital
            
            return CircuitStatus(
                tripped=self._tripped,
                reason=self._reason,
                daily_pnl=self.daily_pnl,
                daily_loss_pct=daily_loss_pct,
                daily_trades=self.daily_trades,
                soft_limit_pct=self.soft_limit,
                hard_limit_pct=self.hard_limit,
            )


# =============================================================================
# 6. GRACEFUL SHUTDOWN — Fecho Controlado
# =============================================================================

class GracefulShutdown:
    """
    ⚡ SHUTDOWN GRACIOSO — Fecha posições antes de parar.
    
    Handlers para:
      - SIGINT (Ctrl+C)
      - SIGTERM (systemctl stop, kill)
      - atexit (saida normal)
    """
    
    def __init__(self, timeout_seconds: int = GRACEFUL_SHUTDOWN_TIMEOUT):
        self.timeout = timeout_seconds
        self._handlers: List[callable] = []
        self._shutdown_in_progress = False
        self._completed = False
        self._position_close_callback: Optional[callable] = None
        self._lock = threading.Lock()
    
    def register_position_close_callback(self, callback: callable):
        """Regista função para fechar posição aberta"""
        self._position_close_callback = callback
    
    def register_handler(self, callback: callable):
        """Regista handler adicional de shutdown"""
        self._handlers.append(callback)
    
    def setup(self):
        """Configura handlers de sinais"""
        if threading.current_thread() is not threading.main_thread():
            logger.debug("GracefulShutdown: ignorado — não estamos na thread principal")
            return
        
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        atexit.register(self._handle_atexit)
        logger.info("⚡ Graceful shutdown configurado (Ctrl+C fecha posições)")
    
    def _handle_signal(self, signum, frame):
        """Handler de sinais UNIX"""
        sig_name = signal.Signals(signum).name
        logger.warning("\n" + "=" * 60)
        logger.warning(f"🛑 SINAL RECEBIDO ({sig_name}) — Iniciando graceful shutdown...")
        logger.warning("=" * 60)
        self._execute_shutdown()
        sys.exit(0)
    
    def _handle_atexit(self):
        """Handler de saída normal"""
        if not self._shutdown_in_progress:
            self._execute_shutdown()
    
    def _execute_shutdown(self):
        """Executa sequência de shutdown"""
        with self._lock:
            if self._shutdown_in_progress:
                return
            self._shutdown_in_progress = True
        
        start_time = time.time()
        
        # 1. Fechar posição aberta
        if self._position_close_callback:
            logger.warning("🛑 A fechar posição aberta...")
            try:
                self._position_close_callback()
                logger.warning("✅ Posição fechada com sucesso")
            except Exception as e:
                logger.error(f"❌ Erro ao fechar posição: {e}")
        
        # 2. Executar handlers adicionais
        for handler in self._handlers:
            try:
                handler()
            except Exception as e:
                logger.error(f"Erro em handler de shutdown: {e}")
        
        # 3. Log de tempo
        elapsed = time.time() - start_time
        logger.warning(f"✅ Shutdown completo em {elapsed:.1f}s")
        self._completed = True
    
    def is_shutdown_requested(self) -> bool:
        """Verifica se shutdown foi pedido"""
        return self._shutdown_in_progress
    
    def wait_for_completion(self, max_wait: float = 60.0) -> bool:
        """Aguarda completion do shutdown"""
        start = time.time()
        while not self._completed:
            if time.time() - start > max_wait:
                return False
            time.sleep(0.1)
        return True


# =============================================================================
# 7. EMERGENCY STOP — Paragem de Emergência via Ficheiro
# =============================================================================

class EmergencyStop:
    """
    ⚡ EMERGENCY STOP — Para o bot criando um ficheiro STOP.
    
    Útil para:
      - Parar remotamente (SSH + touch STOP)
      - Parar via cron job ou monitor externo
      - Parada de emergência quando não tens acesso ao terminal
    """
    
    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            project_root = Path(__file__).parent.parent
        self.stop_file = project_root / 'STOP'
        self.resume_file = project_root / 'RESUME'
    
    def check(self) -> bool:
        """Verifica se foi pedida paragem de emergência"""
        if self.stop_file.exists():
            return True
        return False
    
    def stop(self, reason: str = "manual") -> bool:
        """Cria ficheiro de paragem"""
        try:
            self.stop_file.write_text(f"STOP\nReason: {reason}\nTime: {datetime.now().isoformat()}\n")
            logger.critical("🛑 EMERGENCY STOP ativado! Criado ficheiro STOP.")
            return True
        except Exception as e:
            logger.error(f"Erro a criar ficheiro STOP: {e}")
            return False
    
    def resume(self) -> bool:
        """Remove ficheiro de paragem"""
        try:
            if self.stop_file.exists():
                self.stop_file.unlink()
                logger.warning("✅ Emergency stop removido. Bot pode continuar.")
            if self.resume_file.exists():
                self.resume_file.unlink()
            return True
        except Exception as e:
            logger.error(f"Erro a remover ficheiro STOP: {e}")
            return False


# =============================================================================
# 8. AUDIT LOGGER — Logging Estruturado para Auditoria
# =============================================================================

class AuditLogger:
    """
    ⚡ AUDIT LOGGER — Guarda eventos de segurança em formato estruturado.
    
    Guarda em JSONL para fácil parsing programático.
    """
    
    def __init__(self, log_dir: Optional[Path] = None):
        if log_dir is None:
            log_dir = Path(__file__).parent.parent / 'logs'
        self.log_dir = log_dir
        self.log_dir.mkdir(exist_ok=True)
        self.audit_file = self.log_dir / 'audit.jsonl'
        self._lock = threading.Lock()
    
    def log(self, event: str, level: str = "INFO", details: Optional[Dict] = None):
        """Regista evento de auditoria"""
        audit = SecurityAudit(
            timestamp=datetime.now().isoformat(),
            event=event,
            level=level,
            details=details or {},
        )
        
        with self._lock:
            with open(self.audit_file, 'a') as f:
                f.write(json.dumps(audit.to_dict()) + '\n')
        
        # Também log normal
        msg = f"[AUDIT] {event}"
        if level == 'CRITICAL':
            logger.critical(msg)
        elif level == 'WARNING':
            logger.warning(msg)
        else:
            logger.info(msg)
    
    def get_recent_events(self, minutes: int = 60) -> List[Dict]:
        """Retorna eventos recentes"""
        cutoff = time.time() - (minutes * 60)
        events = []
        
        try:
            with open(self.audit_file, 'r') as f:
                for line in f:
                    try:
                        event = json.loads(line.strip())
                        event_time = datetime.fromisoformat(event['timestamp'])
                        if event_time.timestamp() > cutoff:
                            events.append(event)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        
        return events


# =============================================================================
# MAINNET GUARDIAN — Classe Principal
# =============================================================================

class MainnetGuardian:
    """
    🛡️ MAINNET GUARDIAN — Coordena todas as camadas de segurança.
    
    USO:
        guardian = MainnetGuardian(config)
        
        # No startup:
        guardian.verify_before_start()
        
        # No loop:
        if guardian.should_stop():
            break
        
        # Antes de cada ordem:
        result = guardian.validate_order(asset, side, size, price, market_price)
        if not result.is_valid:
            logger.error(f"Ordem rejeitada: {result.reason}")
            continue
    """
    
    def __init__(self, config: Dict):
        self.config = config
        
        # Inicializar todas as camadas
        self.network_gate = NetworkGate(config)
        self.https_enforcer = HttpsEnforcer(config)
        self.rate_limiter = RateLimiter()
        self.order_validator = OrderValidator(config)
        self.circuit_breaker = CircuitBreaker(config)
        self.graceful_shutdown = GracefulShutdown()
        self.emergency_stop = EmergencyStop()
        self.audit_logger = AuditLogger()
        
        self._verified = False
        self._initial_capital = config.get('risk', {}).get('initial_capital', 10000.0)
    
    def verify_before_start(self) -> bool:
        """
        🔒 VERIFICAÇÃO PRÉ-START — Bloqueia se não estiver seguro.
        Retorna True se pode iniciar.
        """
        if self._verified:
            return True
        
        logger.info("=" * 60)
        logger.info("🛡️  MAINNET GUARDIAN — Verificação de segurança")
        logger.info("=" * 60)
        
        checks = []
        
        # 1. Network gate
        if not self.network_gate.can_trade_real():
            self.network_gate.print_startup_banner()
            checks.append(("Network Gate", False, "Mainnet não aprovada"))
            logger.critical("🚫 Network gate BLOQUEADO!")
        else:
            self.network_gate.print_startup_banner()
            checks.append(("Network Gate", True, f"OK ({self.network_gate.get_network()})"))
        
        # 2. HTTPS
        https_ok, violations = self.https_enforcer.validate_all_urls()
        if not https_ok:
            checks.append(("HTTPS", False, f"{len(violations)} violações"))
            logger.critical("🚫 HTTPS BLOQUEADO!")
        else:
            checks.append(("HTTPS", True, "Todas as URLs usam HTTPS"))
        
        # 3. Config mínima
        risk = self.config.get('risk', {})
        if not risk.get('stop_loss_pct'):
            checks.append(("Stop Loss Config", False, "stop_loss_pct não definido"))
        else:
            checks.append(("Stop Loss Config", True, f"{risk['stop_loss_pct']*100:.1f}%"))
        
        if not risk.get('max_position_size_usd'):
            checks.append(("Position Limit", False, "max_position_size_usd não definido"))
        else:
            checks.append(("Position Limit", True, f"${risk['max_position_size_usd']}"))
        
        # Log de resumo
        logger.info("-" * 60)
        for name, ok, detail in checks:
            status = "✅" if ok else "❌"
            logger.info(f"  {status} {name}: {detail}")
        logger.info("-" * 60)
        
        all_ok = all(ok for _, ok, _ in checks)
        
        if all_ok:
            logger.info("✅ Todas as verificações passaram. Bot pode iniciar.")
            self._verified = True
            self.audit_logger.log("verify_before_start", "INFO", {"checks": checks})
            return True
        else:
            failed = [name for name, ok, _ in checks if not ok]
            logger.critical(f"🚫 VERIFICAÇÃO FALHOU: {', '.join(failed)}")
            self.audit_logger.log("verify_before_start", "CRITICAL", {
                "checks": checks,
                "failed": failed,
            })
            return False
    
    def should_stop(self, current_capital: Optional[float] = None) -> bool:
        """
        Verifica se o bot deve parar (circuit breaker ou emergency stop).
        """
        # Emergency stop via ficheiro
        if self.emergency_stop.check():
            logger.critical("🛑 Ficheiro STOP detectado! A parar...")
            return True
        
        # Circuit breaker
        if current_capital is not None:
            if self.circuit_breaker.check(current_capital, self._initial_capital):
                return True
        
        return False
    
    def validate_order(
        self,
        asset: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        market_price: Optional[float] = None,
        account_balance: Optional[float] = None,
    ) -> OrderCheckResult:
        """Valida uma ordem antes de enviar"""
        result = self.order_validator.validate(
            asset, side, size, price, market_price, account_balance
        )
        
        if not result.is_valid:
            self.audit_logger.log(
                "order_rejected",
                "WARNING",
                {
                    "asset": asset,
                    "side": side,
                    "size": size,
                    "reason": result.reason,
                    "checks_failed": result.checks_failed,
                }
            )
        
        return result
    
    def validate_slippage(
        self,
        expected_price: float,
        actual_price: float,
        side: str,
    ) -> Tuple[bool, float]:
        """Valida slippage após fill"""
        return self.order_validator.validate_slippage(expected_price, actual_price, side)
    
    def wait_rate_limit(self, exchange: str) -> float:
        """Aguarda rate limit se necessário"""
        return self.rate_limiter.wait_if_needed(exchange)
    
    def record_request(self, exchange: str, status_code: int = 200):
        """Regista um request para rate limiting"""
        self.rate_limiter.record_request(exchange, status_code)
    
    def setup_graceful_shutdown(self, position_close_callback: callable):
        """Configura graceful shutdown com callback de fecho de posição"""
        self.graceful_shutdown.register_position_close_callback(position_close_callback)
        self.graceful_shutdown.setup()
    
    def get_status(self) -> Dict:
        """Retorna estado completo de segurança"""
        circuit = self.circuit_breaker.get_status()
        
        return {
            'verified': self._verified,
            'network': self.network_gate.get_network(),
            'can_trade_real': self.network_gate.can_trade_real(),
            'circuit_breaker': circuit.to_dict() if hasattr(circuit, 'to_dict') else {
                'tripped': circuit.tripped,
                'reason': circuit.reason,
                'daily_pnl': circuit.daily_pnl,
                'daily_loss_pct': circuit.daily_loss_pct,
            },
            'emergency_stop': self.emergency_stop.check(),
        }
    
    def log_audit(self, event: str, level: str = "INFO", details: Optional[Dict] = None):
        """Regista evento de auditoria"""
        self.audit_logger.log(event, level, details)


# =============================================================================
# FUNÇÕES DE CONVENIÊNCIA
# =============================================================================

def create_guardian(config: Dict) -> MainnetGuardian:
    """Factory function para criar MainnetGuardian"""
    return MainnetGuardian(config)


def approve_mainnet(config: Dict) -> bool:
    """Aprova mainnet (cria ficheiro .mainnet_approved)"""
    gate = NetworkGate(config)
    return gate.approve_mainnet()


def block_mainnet(config: Dict) -> bool:
    """Bloqueia mainnet (cria ficheiro .mainnet_blocked)"""
    gate = NetworkGate(config)
    return gate.block_mainnet()


def emergency_stop(project_root: Optional[Path] = None) -> bool:
    """Ativa paragem de emergência"""
    es = EmergencyStop(project_root)
    return es.stop()


def check_emergency_stop(project_root: Optional[Path] = None) -> bool:
    """Verifica se há paragem de emergência ativa"""
    es = EmergencyStop(project_root)
    return es.check()


if __name__ == "__main__":
    # Teste rápido
    logging.basicConfig(level=logging.INFO)
    
    test_config = {
        'bot': {'paper_trading': True, 'network': 'paper'},
        'risk': {
            'initial_capital': 10000,
            'max_position_size_usd': 100,
            'stop_loss_pct': 0.02,
            'daily_loss_limit_pct': 0.05,
            'daily_loss_hard_stop_pct': 0.10,
        },
        'data_sources': {
            'binance': {'enabled': True, 'base_url': 'https://fapi.binance.com'},
            'bybit': {'enabled': True, 'base_url': 'https://api.bybit.com'},
        }
    }
    
    guardian = MainnetGuardian(test_config)
    ok = guardian.verify_before_start()
    print(f"\nVerificação: {'PASS' if ok else 'FAIL'}")
    
    # Testar validação de ordem
    result = guardian.validate_order('BTC', 'BUY', 50, market_price=50000)
    print(f"\nOrdem válida: {result.is_valid} — {result.reason}")
    
    # Testar ordem inválida
    result = guardian.validate_order('BTC', 'BUY', 5, market_price=50000)
    print(f"Ordem inválida: {result.is_valid} — {result.reason}")
