"""
StateMachine — Gerenciamento de estados do bot.
IDLE → SCANNING → ANALYZING → ENTERING → POSITION → EXITING → IDLE
"""
import logging
from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class BotState(Enum):
    """Estados possíveis do bot."""
    IDLE = auto()          # À espera, sem posição
    SCANNING = auto()      # A buscar dados de mercado
    ANALYZING = auto()     # A analisar sinais
    ENTERING = auto()      # A executar entrada
    POSITION = auto()      # Em posição, a monitorizar
    EXITING = auto()       # A executar saída
    ERROR = auto()         # Estado de erro, requer intervenção
    SHUTDOWN = auto()      # A encerrar


@dataclass
class StateTransition:
    """Transição de estado com metadados."""
    from_state: BotState
    to_state: BotState
    reason: str = ""
    timestamp: float = 0.0


class StateMachine:
    """
    Máquina de estados finita para o ciclo de vida do bot.
    
    Garante que o bot só pode transitar entre estados válidos.
    Exemplo: não pode ir de IDLE direto para POSITION (tem de passar por ENTERING).
    """
    
    # Transições válidas: de → para[]
    VALID_TRANSITIONS = {
        BotState.IDLE: [BotState.SCANNING, BotState.SHUTDOWN],
        BotState.SCANNING: [BotState.ANALYZING, BotState.IDLE, BotState.ERROR],
        BotState.ANALYZING: [BotState.ENTERING, BotState.IDLE, BotState.ERROR],
        BotState.ENTERING: [BotState.POSITION, BotState.IDLE, BotState.ERROR],
        BotState.POSITION: [BotState.EXITING, BotState.ERROR],
        BotState.EXITING: [BotState.IDLE, BotState.ERROR],
        BotState.ERROR: [BotState.IDLE, BotState.SHUTDOWN],
        BotState.SHUTDOWN: [],  # Terminal
    }
    
    def __init__(self, event_bus=None):
        self._state = BotState.IDLE
        self._previous_state = None
        self._transition_history = []
        self._event_bus = event_bus
        self._on_enter_callbacks = {}
        self._on_exit_callbacks = {}
    
    @property
    def state(self) -> BotState:
        return self._state
    
    @property
    def previous_state(self) -> Optional[BotState]:
        return self._previous_state
    
    @property
    def is_idle(self) -> bool:
        return self._state == BotState.IDLE
    
    @property
    def is_in_position(self) -> bool:
        return self._state == BotState.POSITION
    
    @property
    def is_error(self) -> bool:
        return self._state == BotState.ERROR
    
    def can_transition(self, new_state: BotState) -> bool:
        """Verifica se transição é válida."""
        return new_state in self.VALID_TRANSITIONS.get(self._state, [])
    
    def transition(self, new_state: BotState, reason: str = "") -> bool:
        """Tenta transitar para novo estado. Retorna True se sucesso."""
        if not self.can_transition(new_state):
            logger.warning(
                f"[StateMachine] Transição INVÁLIDA: {self._state.name} → {new_state.name}"
            )
            return False
        
        old_state = self._state
        
        # Callback de saída
        if old_state in self._on_exit_callbacks:
            self._on_exit_callbacks[old_state](old_state, new_state, reason)
        
        # Executar transição
        self._previous_state = old_state
        self._state = new_state
        
        # Registar
        import time
        transition = StateTransition(
            from_state=old_state,
            to_state=new_state,
            reason=reason,
            timestamp=time.time()
        )
        self._transition_history.append(transition)
        
        # Callback de entrada
        if new_state in self._on_enter_callbacks:
            self._on_enter_callbacks[new_state](old_state, new_state, reason)
        
        # Publicar evento
        if self._event_bus:
            self._event_bus.publish('state.changed', {
                'from': old_state.name,
                'to': new_state.name,
                'reason': reason
            })
        
        logger.info(f"[StateMachine] {old_state.name} → {new_state.name} | {reason}")
        return True
    
    def on_enter(self, state: BotState, callback: Callable):
        """Regista callback para quando se entra num estado."""
        self._on_enter_callbacks[state] = callback
    
    def on_exit(self, state: BotState, callback: Callable):
        """Regista callback para quando se sai de um estado."""
        self._on_exit_callbacks[state] = callback
    
    def get_history(self, limit: int = 50):
        """Retorna histórico de transições."""
        return self._transition_history[-limit:]
    
    def __repr__(self):
        return f"StateMachine({self._state.name})"
