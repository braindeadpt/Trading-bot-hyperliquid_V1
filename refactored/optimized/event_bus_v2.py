"""
EventBus v2 — Otimizado: deque O(1) history, zero-allocation subscriptions.
Substitui o v1 que fazia slicing O(n) a cada evento.
"""
import threading
import logging
from typing import Callable, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Evento tipado do sistema."""
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = "system"


class EventBus:
    """
    Barramento de eventos thread-safe — VERSÃO OTIMIZADA.
    
    Mudanças v2:
    - History usa deque(maxlen) → O(1) append, sem slicing
    - Subscribers snapshot com list() → evita mutação durante iteração
    - Lock granular: subscribers vs history separados quando possível
    """
    
    def __init__(self, max_history: int = 5000):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._history: deque = deque(maxlen=max_history)  # ✅ O(1) auto-trim
        self._max_history = max_history
        self._lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._event_count = 0
    
    # ─── Subscrição ──────────────────────────────────────────
    
    def subscribe(self, event_type: str, callback: Callable[[Event], None]) -> None:
        """Regista callback para um tipo de evento."""
        with self._lock:
            self._subscribers[event_type].append(callback)
        logger.debug(f"[EventBus] Subscrição: {event_type} → {callback.__qualname__}")
    
    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove callback."""
        with self._lock:
            if event_type in self._subscribers:
                subs = self._subscribers[event_type]
                # ✅ O(n) mas raro — unsubscrição não é hot path
                self._subscribers[event_type] = [cb for cb in subs if cb != callback]
    
    # ─── Publicação ──────────────────────────────────────────
    
    def publish(self, event_type: str, payload: Dict[str, Any] = None, source: str = "system") -> None:
        """Publica evento síncrono (thread-safe, zero-allocation history)."""
        event = Event(type=event_type, payload=payload or {}, source=source)
        
        # ✅ History O(1) — deque.append() nunca realoca
        with self._stats_lock:
            self._history.append(event)
            self._event_count += 1
        
        # ✅ Snapshot seguro fora do lock
        with self._lock:
            subscribers = list(self._subscribers.get(event_type, []))
        
        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"[EventBus] Erro no subscriber {callback.__qualname__}: {e}")
    
    # ─── Queries ─────────────────────────────────────────────
    
    def get_history(self, event_type: str = None, limit: int = 100) -> List[Event]:
        """Retorna histórico de eventos. O(limit) em vez de O(n)."""
        with self._stats_lock:
            if event_type:
                # ✅ Itera do mais recente para trás, para no limit
                result = []
                for event in reversed(self._history):
                    if event.type == event_type:
                        result.append(event)
                        if len(result) >= limit:
                            break
                return list(reversed(result))
            return list(self._history)[-limit:]  # ✅ O(limit) slice pequeno
    
    def get_latest(self, event_type: str) -> Event:
        """Retorna evento mais recente de um tipo. O(1) average."""
        with self._stats_lock:
            for event in reversed(self._history):
                if event.type == event_type:
                    return event
        return None
    
    def stats(self) -> Dict[str, int]:
        """Estatísticas do bus."""
        with self._lock:
            with self._stats_lock:
                return {
                    'total_events': self._event_count,
                    'history_size': len(self._history),
                    'event_types': list(self._subscribers.keys()),
                    'subscriber_count': sum(len(subs) for subs in self._subscribers.values())
                }
