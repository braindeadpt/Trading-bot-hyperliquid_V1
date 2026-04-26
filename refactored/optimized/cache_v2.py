"""
DataCache v2 — Otimizado: RLock + OrderedDict LRU eviction O(1).
Resolve race conditions reais e substitui eviction O(n log n).
"""
import time
import threading
import logging
from typing import Any, Optional, Dict
from dataclasses import dataclass
from collections import OrderedDict

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    value: Any
    timestamp: float
    ttl: int
    
    @property
    def is_expired(self) -> bool:
        return time.time() - self.timestamp > self.ttl


class DataCache:
    """
    Cache em memória v2 — THREAD-SAFE com RLock + OrderedDict LRU.
    
    Mudanças v2:
    - threading.RLock() → 100% thread-safe
    - OrderedDict → LRU move_to_end() O(1)
    - Eviction → popitem(last=False) O(1)
    - Stats → sem lock extra (devolve snapshot)
    """
    
    def __init__(self, ttl_seconds: int = 10, max_size: int = 1000):
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._default_ttl = ttl_seconds
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._lock = threading.RLock()
    
    def get(self, key: str) -> Optional[Any]:
        """Busca valor do cache — LRU move-to-front."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            
            if entry.is_expired:
                del self._store[key]
                self._misses += 1
                return None
            
            # ✅ LRU: move para o fim (mais recentemente usado)
            self._store.move_to_end(key)
            self._hits += 1
            return entry.value
    
    def set(self, key: str, value: Any, ttl: int = None) -> None:
        """Guarda valor — eviction O(1) se cheio."""
        with self._lock:
            # ✅ Eviction O(1): remove o mais antigo
            while len(self._store) >= self._max_size:
                self._store.popitem(last=False)
            
            self._store[key] = CacheEntry(
                value=value,
                timestamp=time.time(),
                ttl=ttl or self._default_ttl
            )
            # Move para o fim (acabou de ser usado)
            self._store.move_to_end(key)
    
    def delete(self, key: str) -> None:
        """Remove chave."""
        with self._lock:
            self._store.pop(key, None)
    
    def clear(self) -> None:
        """Limpa todo o cache."""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
    
    def get_or_compute(self, key: str, compute_fn, ttl: int = None) -> Any:
        """Busca cache ou computa e guarda."""
        cached = self.get(key)
        if cached is not None:
            return cached
        
        value = compute_fn()
        self.set(key, value, ttl)
        return value
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Estatísticas — snapshot consistente."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                'size': len(self._store),
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate_pct': round(hit_rate, 1),
                'max_size': self._max_size,
                'default_ttl': self._default_ttl
            }
    
    def __repr__(self):
        s = self.stats
        return f"DataCache(size={s['size']}, hit_rate={s['hit_rate_pct']}%)"
