"""
Data Aggregator v2 - Com retry logic, validação de respostas e cache
"""
import requests
import time
import json
from functools import wraps
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


def retry_on_failure(max_retries=3, backoff_seconds=2):
    """Decorator que retry com backoff exponencial"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    wait_time = backoff_seconds * (2 ** attempt)
                    logger.warning(
                        f"{func.__name__} falhou (tentativa {attempt+1}/{max_retries}): {e}. "
                        f"Aguardando {wait_time}s..."
                    )
                    time.sleep(wait_time)
            logger.error(f"{func.__name__} falhou após {max_retries} tentativas: {last_exception}")
            raise last_exception
        return wrapper
    return decorator


class DataAggregator:
    """Busca e agrega dados de OI, volume e funding de várias exchanges"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.sources = config.get('data_sources', {})
        self.last_oi = {}
        self.last_update = 0
        self._price_cache = {}  # Cache do último preço válido
        self._cache_timestamp = 0
        
        # Headers padrão para evitar bloqueios
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def validate_api(self, source_name: str) -> bool:
        """Testa se uma API está respondendo corretamente antes de usar"""
        try:
            if source_name == 'binance':
                resp = self.session.get(
                    "https://fapi.binance.com/fapi/v1/ping",
                    timeout=5
                )
                return resp.status_code == 200
            
            elif source_name == 'bybit':
                resp = self.session.get(
                    "https://api.bybit.com/v5/market/time",
                    timeout=5
                )
                data = resp.json()
                return data.get('retCode') == 0
            
            elif source_name == 'okx':
                resp = self.session.get(
                    "https://www.okx.com/api/v5/public/time",
                    timeout=5
                )
                data = resp.json()
                return data.get('code') == '0'
            
            elif source_name == 'hyperliquid':
                resp = self.session.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "meta"},
                    headers={"Content-Type": "application/json"},
                    timeout=5
                )
                # Hyperliquid meta retorna lista de tokens
                return resp.status_code == 200 and len(resp.text) > 10
            
            return False
            
        except Exception as e:
            logger.warning(f"Validação falhou para {source_name}: {e}")
            return False
    
    def test_all_apis(self) -> Dict[str, bool]:
        """Testa todas as APIs configuradas e retorna status"""
        results = {}
        logger.info("="*60)
        logger.info("TESTANDO APIs...")
        
        for name, cfg in self.sources.items():
            if not cfg.get('enabled', False):
                results[name] = False
                logger.info(f"  {name}: DISABLED (config)")
                continue
            
            is_ok = self.validate_api(name)
            results[name] = is_ok
            status = "✅ OK" if is_ok else "❌ FAIL"
            logger.info(f"  {name}: {status}")
        
        ok_count = sum(1 for v in results.values() if v)
        logger.info(f"  APIs funcionais: {ok_count}/{len(results)}")
        logger.info("="*60)
        
        return results
    
    def fetch_all_data(self, asset: str) -> Optional[Dict]:
        """Busca dados agregados de todas as exchanges"""
        results = {
            'oi_total': 0,
            'oi_change_pct': 0,
            'volume_total': 0,
            'funding_avg': 0,
            'exchanges_data': {},
            'timestamp': time.time()
        }
        
        valid_sources = 0
        funding_values = []
        prices = []
        
        for source_name, source_config in self.sources.items():
            if not source_config.get('enabled', False):
                continue
            
            try:
                if source_name == 'binance':
                    data = self._fetch_binance(asset)
                elif source_name == 'bybit':
                    data = self._fetch_bybit(asset)
                elif source_name == 'okx':
                    data = self._fetch_okx(asset)
                elif source_name == 'hyperliquid':
                    data = self._fetch_hyperliquid(asset)
                else:
                    continue
                
                if data:
                    results['exchanges_data'][source_name] = data
                    results['oi_total'] += data.get('oi_usd', 0)
                    results['volume_total'] += data.get('volume_24h', 0)
                    if data.get('funding_rate') is not None:
                        funding_values.append(data['funding_rate'])
                    if data.get('mark_price', 0) > 0:
                        prices.append(data['mark_price'])
                    valid_sources += 1
                    
            except Exception as e:
                logger.warning(f"Erro a buscar dados de {source_name}: {e}")
                continue
        
        if valid_sources == 0:
            logger.error("Nenhuma exchange respondeu!")
            return None
        
        # Funding médio
        if funding_values:
            results['funding_avg'] = sum(funding_values) / len(funding_values)
        
        # Variação de OI
        if self.last_oi:
            oi_old = sum(self.last_oi.values())
            if oi_old > 0:
                results['oi_change_pct'] = (results['oi_total'] - oi_old) / oi_old
        
        # Guardar cache
        self.last_oi = {
            k: v.get('oi_usd', 0) 
            for k, v in results['exchanges_data'].items()
        }
        
        # Guardar preço médio no cache
        if prices:
            self._price_cache[asset] = sum(prices) / len(prices)
            self._cache_timestamp = time.time()
        
        self.last_update = time.time()
        return results
    
    def get_cached_price(self, asset: str, max_age_seconds: int = 300) -> float:
        """Retorna preço do cache se for recente"""
        if asset in self._price_cache:
            age = time.time() - self._cache_timestamp
            if age < max_age_seconds:
                return self._price_cache[asset]
        return 0
    
    def _safe_json(self, response: requests.Response, source_name: str) -> Optional[Dict]:
        """Extrai JSON de forma segura, com logging detalhado"""
        if response.status_code != 200:
            logger.warning(
                f"{source_name} HTTP {response.status_code}: {response.text[:200]}"
            )
            return None
        
        if not response.text or response.text.strip() == '':
            logger.warning(f"{source_name} resposta vazia")
            return None
        
        # Verificar se é HTML (Cloudflare block, error page, etc.)
        text_start = response.text.strip()[:20].lower()
        if text_start.startswith('<!doctype') or text_start.startswith('<html'):
            logger.warning(f"{source_name} retornou HTML em vez de JSON! (Cloudflare block?)")
            return None
        
        try:
            return response.json()
        except json.JSONDecodeError as e:
            logger.warning(
                f"{source_name} JSON inválido: {e} | "
                f"Primeiros 100 chars: {response.text[:100]}"
            )
            return None
    
    @retry_on_failure(max_retries=2, backoff_seconds=1)
    def _fetch_binance(self, asset: str) -> Optional[Dict]:
        """Busca dados da Binance Futures"""
        base_url = self.sources['binance']['base_url']
        symbol = f"{asset}USDT"
        
        # Open Interest
        resp = self.session.get(
            f"{base_url}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=10
        )
        oi_data = self._safe_json(resp, 'binance')
        if not oi_data:
            return None
        
        # Mark Price
        resp = self.session.get(
            f"{base_url}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10
        )
        price_data = self._safe_json(resp, 'binance')
        mark_price = float(price_data.get('markPrice', 0)) if price_data else 0
        
        oi_contracts = float(oi_data.get('openInterest', 0))
        oi_usd = oi_contracts * mark_price
        
        # Funding
        resp = self.session.get(
            f"{base_url}/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10
        )
        funding_data = self._safe_json(resp, 'binance')
        funding_rate = None
        if funding_data and isinstance(funding_data, list) and len(funding_data) > 0:
            funding_rate = float(funding_data[0].get('fundingRate', 0))
        
        # Volume
        resp = self.session.get(
            f"{base_url}/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            timeout=10
        )
        ticker_data = self._safe_json(resp, 'binance')
        volume = float(ticker_data.get('volume', 0)) * mark_price if ticker_data else 0
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': volume,
            'mark_price': mark_price
        }
    
    @retry_on_failure(max_retries=2, backoff_seconds=1)
    def _fetch_bybit(self, asset: str) -> Optional[Dict]:
        """Busca dados da Bybit"""
        base_url = self.sources['bybit']['base_url']
        symbol = f"{asset}USDT"
        
        resp = self.session.get(
            f"{base_url}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=10
        )
        data = self._safe_json(resp, 'bybit')
        
        if not data or data.get('retCode') != 0:
            return None
        
        result = data['result']['list'][0]
        last_price = float(result.get('lastPrice', 0))
        oi_contracts = float(result.get('openInterest', 0))
        oi_usd = oi_contracts * last_price
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': float(result.get('fundingRate', 0)),
            'volume_24h': float(result.get('volume24h', 0)),
            'mark_price': last_price
        }
    
    @retry_on_failure(max_retries=2, backoff_seconds=1)
    def _fetch_okx(self, asset: str) -> Optional[Dict]:
        """Busca dados da OKX"""
        base_url = self.sources['okx']['base_url']
        symbol = f"{asset}-USDT-SWAP"
        
        # OI
        resp = self.session.get(
            f"{base_url}/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        oi_data = self._safe_json(resp, 'okx')
        
        if not oi_data or oi_data.get('code') != '0':
            return None
        
        oi_contracts = float(oi_data['data'][0].get('oi', 0))
        
        # Mark Price
        resp = self.session.get(
            f"{base_url}/api/v5/public/mark-price",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        price_data = self._safe_json(resp, 'okx')
        mark_price = float(price_data['data'][0].get('markPx', 0)) if price_data else 0
        
        oi_usd = oi_contracts * mark_price
        
        # Funding
        resp = self.session.get(
            f"{base_url}/api/v5/public/funding-rate",
            params={"instId": symbol},
            timeout=10
        )
        funding_data = self._safe_json(resp, 'okx')
        funding_rate = None
        if funding_data and funding_data.get('data'):
            funding_rate = float(funding_data['data'][0].get('fundingRate', 0))
        
        # Volume
        resp = self.session.get(
            f"{base_url}/api/v5/market/tickers",
            params={"instType": "SWAP", "instId": symbol},
            timeout=10
        )
        ticker_data = self._safe_json(resp, 'okx')
        volume = float(ticker_data['data'][0].get('volCcy24h', 0)) if ticker_data else 0
        
        return {
            'oi_usd': oi_usd,
            'funding_rate': funding_rate,
            'volume_24h': volume,
            'mark_price': mark_price
        }
    
    def _get_sanity_range(self, asset: str) -> tuple:
        """Retorna (min, max) aceitáveis para validação de preço"""
        ranges = {
            'BTC': (10000.0, 200000.0),
            'ETH': (500.0, 20000.0),
            'SOL': (10.0, 1000.0),
        }
        return ranges.get(asset, (0.01, 100000.0))
    
    def _is_price_sane(self, asset: str, price: float) -> bool:
        """Valida se o preço está num range realista"""
        min_val, max_val = self._get_sanity_range(asset)
        sane = min_val <= price <= max_val
        if not sane and price > 0:
            logger.error(
                f"🚨 PREÇO INSANO detectado! {asset}=${price:,.2f} "
                f"(esperado: ${min_val:,.0f}-${max_val:,.0f})"
            )
        return sane

    @retry_on_failure(max_retries=3, backoff_seconds=2)
    def _fetch_hyperliquid(self, asset: str) -> Optional[Dict]:
        """Busca preço da Hyperliquid — MÉTODO ROBUSTO com fallback multi-API"""
        base_url = self.sources['hyperliquid']['base_url']
        
        # ─── MÉTODO 1: allMids ───
        try:
            resp = self.session.post(
                f"{base_url}/info",
                json={"type": "allMids"},
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, dict) and asset in data:
                        raw = data[asset]
                        mark_price = float(raw) if raw else 0
                        logger.info(
                            f"📡 [allMids] {asset} raw='{raw}' → ${mark_price:,.2f}"
                        )
                        
                        if mark_price > 0 and self._is_price_sane(asset, mark_price):
                            self._price_cache[asset] = mark_price
                            self._cache_timestamp = time.time()
                            return {
                                'oi_usd': 0,
                                'funding_rate': None,
                                'volume_24h': 0,
                                'mark_price': mark_price
                            }
                        elif mark_price > 0:
                            logger.warning(f"⚠️ allMids deu preço insano, tentando método 2...")
                    else:
                        logger.warning(f"⚠️ allMids: {asset} não encontrado em dict com {len(data)} keys")
                except Exception as e:
                    logger.warning(f"⚠️ allMids parse falhou: {e}")
        except Exception as e:
            logger.warning(f"⚠️ allMids request falhou: {e}")
        
        # ─── MÉTODO 2: metaAndAssetCtxs (match por índice) ───
        try:
            resp = self.session.post(
                f"{base_url}/info",
                json={"type": "metaAndAssetCtxs"},
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if isinstance(data, list) and len(data) >= 2:
                        meta = data[0]
                        ctxs = data[1]
                        
                        # Match por índice na universe
                        universe = meta.get('universe', [])
                        for idx, u in enumerate(universe):
                            if isinstance(u, dict) and u.get('name') == asset:
                                if idx < len(ctxs):
                                    ctx = ctxs[idx]
                                    # Tentar midPx, markPx, oraclePx nesta ordem
                                    for px_key in ['midPx', 'markPx', 'oraclePx']:
                                        raw = ctx.get(px_key)
                                        if raw:
                                            try:
                                                mark_price = float(raw)
                                                logger.info(
                                                    f"📡 [metaAndAssetCtxs] {asset} "
                                                    f"{px_key}='{raw}' → ${mark_price:,.2f}"
                                                )
                                                if self._is_price_sane(asset, mark_price):
                                                    self._price_cache[asset] = mark_price
                                                    self._cache_timestamp = time.time()
                                                    return {
                                                        'oi_usd': 0,
                                                        'funding_rate': None,
                                                        'volume_24h': 0,
                                                        'mark_price': mark_price
                                                    }
                                            except:
                                                continue
                                break
                        logger.warning(f"⚠️ metaAndAssetCtxs: {asset} não encontrado/match")
                except Exception as e:
                    logger.warning(f"⚠️ metaAndAssetCtxs parse falhou: {e}")
        except Exception as e:
            logger.warning(f"⚠️ metaAndAssetCtxs request falhou: {e}")
        
        # ─── FALLBACK: cache ───
        return self._fallback_price(asset)

    def _fallback_price(self, asset: str) -> Dict:
        """Retorna preço do cache quando a API falha"""
        cached = self.get_cached_price(asset, max_age_seconds=120)  # 2 min max
        if cached > 0:
            # Validar cache também
            if self._is_price_sane(asset, cached):
                logger.warning(
                    f"⚠️ APIs falharam — usando CACHE para {asset}: ${cached:,.2f} "
                    f"(idade: {time.time() - self._cache_timestamp:.0f}s)"
                )
                return {
                    'oi_usd': 0,
                    'funding_rate': None,
                    'volume_24h': 0,
                    'mark_price': cached
                }
            else:
                logger.error(f"🚨 Cache também tem preço insano! Ignorando.")
        logger.error(f"❌ Sem preço válido para {asset}! Dashboard mostrará 0")
        return {
            'oi_usd': 0,
            'funding_rate': None,
            'volume_24h': 0,
            'mark_price': 0
        }


def test_apis_command():
    """Comando CLI para testar APIs"""
    import sys
    from pathlib import Path
    
    # Adicionar src ao path
    sys.path.insert(0, str(Path(__file__).parent))
    from utils import load_config
    
    logging.basicConfig(level=logging.INFO)
    
    config = load_config()
    agg = DataAggregator(config)
    results = agg.test_all_apis()
    
    print("\n" + "="*60)
    print("RESULTADO DOS TESTES:")
    for name, ok in results.items():
        status = "✅ FUNCIONAL" if ok else "❌ INDISPONÍVEL"
        print(f"  {name}: {status}")
    
    ok_count = sum(1 for v in results.values() if v)
    print(f"\nTotal: {ok_count}/{len(results)} APIs OK")
    
    if ok_count == 0:
        print("\n⚠️  NENHUMA API FUNCIONAL! Verifica a internet/conexão.")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(test_apis_command())
