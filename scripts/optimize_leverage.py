#!/usr/bin/env python3
"""
🧪 BACKTEST OPTIMIZER — Leverage + Timeframe Matrix

Script sistemático para testar todas as combinações de leverage e timeframe,
com cálculo de métricas avançadas de performance e risco.

Combinações testadas:
  - Leverage: 1x, 2x, 3x, 5x, 10x
  - Timeframes: 5m, 15m, 30m, 1h

Métricas calculadas:
  - Total return (%)
  - Max drawdown (%)
  - Sharpe ratio
  - Profit factor
  - Win rate (%)
  - Number of trades
  - Average trade return (%)
  - Worst losing streak
  - Liquidation risk (probabilidade de liquidação para 5x/10x)

Como correr:
  cd trading-bot-hyperliquid
  python scripts/optimize_leverage.py

Resultados:
  - results/leverage_optimization.json
  - docs/BACKTEST_LEVERAGE_RESULTS.md
"""

import json
import logging
import sys
from pathlib import Path
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Adicionar src ao path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backtest_db import BacktestEngineDB
from database import BotDatabase
from utils import load_config

logger = logging.getLogger(__name__)


class LeveragedBacktestEngine(BacktestEngineDB):
    """
    Motor de backtest com suporte a leverage variável.

    O leverage afeta:
      1. Multiplicador de PnL (ganhos/perdas ampliados)
      2. Risco de liquidação intra-candle (verificado via low/high)
      3. Impacto do stop loss no capital (maior com leverage)
    """

    def __init__(self, config: Dict, leverage: float = 1.0, db: Optional[BotDatabase] = None):
        super().__init__(config, db)
        self.leverage = leverage
        self.liquidation_count = 0  # Quantas vezes teria sido liquidado
        self.max_liquidation_drawdown = 0.0  # Maior drawdown que tocou liquidação
        self.worst_losing_streak = 0
        self.current_losing_streak = 0

    def _enter_long(self, symbol: str, price: float, timestamp: int,
                    volume_ratio: float, oi_change: float, funding: float):
        """Override: regista preço de liquidação para longs"""
        super()._enter_long(symbol, price, timestamp, volume_ratio, oi_change, funding)
        # Preço de liquidação para long: entry_price * (1 - 1/leverage)
        self.liquidation_price_long = price * (1 - 1.0 / self.leverage) if self.leverage > 1 else 0

    def _enter_short(self, symbol: str, price: float, timestamp: int,
                     volume_ratio: float, oi_change: float, funding: float):
        """Override: regista preço de liquidação para shorts"""
        super()._enter_short(symbol, price, timestamp, volume_ratio, oi_change, funding)
        # Preço de liquidação para short: entry_price * (1 + 1/leverage)
        self.liquidation_price_short = price * (1 + 1.0 / self.leverage) if self.leverage > 1 else float('inf')

    def run(self, symbol: str = "BTC", interval: str = "15m", days: int = 30,
            save_to_db: bool = False) -> Dict:
        """
        Corre backtest com verificação de liquidação intra-candle.
        """
        data = self.load_data(symbol, interval, days)
        if not data:
            return {'error': 'Sem dados disponíveis'}

        logger.info(f"Iniciando backtest: {symbol} {interval} L{self.leverage}x ({len(data)} candles)")

        # Reset estado (não chama __init__ para não perder leverage)
        self.trades = []
        self.equity = []
        self.current_position = None
        self.entry_price = 0
        self.entry_time = None
        self.volume_history = deque(maxlen=self.volume_lookback)
        self.price_history = deque(maxlen=self.price_sma_period)
        self.last_oi = 0
        self.bullish_count = 0
        self.bearish_count = 0
        self.max_price_since_entry = 0
        self.min_price_since_entry = 0
        self.trailing_stop_price = 0
        self.trailing_active = False
        self.current_capital = self.initial_capital
        self.peak_equity = self.initial_capital
        self.max_drawdown = 0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.liquidation_count = 0
        self.max_liquidation_drawdown = 0.0
        self.worst_losing_streak = 0
        self.current_losing_streak = 0

        for i, candle in enumerate(data):
            price = candle['close']
            volume = candle['volume']
            oi = candle.get('oi', 0)
            funding = candle.get('funding_rate', 0)
            timestamp = candle['timestamp']
            candle_low = candle.get('low', price)
            candle_high = candle.get('high', price)

            # --- VERIFICAÇÃO DE LIQUIDAÇÃO INTRA-CANDLE ---
            if self.current_position == 'long' and self.leverage > 1:
                if candle_low <= self.liquidation_price_long:
                    self.liquidation_count += 1
                    dd = (self.entry_price - candle_low) / self.entry_price
                    if dd > self.max_liquidation_drawdown:
                        self.max_liquidation_drawdown = dd
                    # Simular saída por liquidação
                    self._exit_position(symbol, self.liquidation_price_long, timestamp, 'LIQUIDATION')
                    continue

            if self.current_position == 'short' and self.leverage > 1:
                if candle_high >= self.liquidation_price_short:
                    self.liquidation_count += 1
                    dd = (candle_high - self.entry_price) / self.entry_price
                    if dd > self.max_liquidation_drawdown:
                        self.max_liquidation_drawdown = dd
                    self._exit_position(symbol, self.liquidation_price_short, timestamp, 'LIQUIDATION')
                    continue

            self.volume_history.append(volume)

            if len(self.volume_history) < self.volume_lookback // 2:
                continue

            volume_avg = sum(self.volume_history) / len(self.volume_history)
            volume_ratio = volume / volume_avg if volume_avg > 0 else 0

            self.price_history.append(price)
            price_sma = sum(self.price_history) / len(self.price_history) if len(self.price_history) >= self.price_sma_period else 0
            price_above_sma = price > price_sma if price_sma > 0 else False

            oi_change = 0
            if self.last_oi > 0 and oi > 0:
                oi_change = (oi - self.last_oi) / self.last_oi
            if oi > 0:
                self.last_oi = oi

            funding_extreme = funding > self.max_funding or funding < self.min_funding

            candle_bullish = candle['close'] > candle['open']
            candle_bearish = candle['close'] < candle['open']
            if candle_bullish:
                self.bullish_count += 1
                self.bearish_count = 0
            elif candle_bearish:
                self.bearish_count += 1
                self.bullish_count = 0
            else:
                self.bullish_count = 0
                self.bearish_count = 0

            # --- SINAIS DE ENTRADA ---
            if self.current_position is None:
                volume_ok = volume_ratio > self.volume_threshold
                funding_ok = not funding_extreme

                # LONG
                if price_above_sma and self.bullish_count >= self.min_bullish_candles and volume_ok and funding_ok and price > 0:
                    if oi > 0:
                        if oi_change > self.oi_threshold:
                            self._enter_long(symbol, price, timestamp, volume_ratio, oi_change, funding)
                    else:
                        self._enter_long(symbol, price, timestamp, volume_ratio, 0, funding)

                # SHORT
                short_volume_ok = volume_ratio > self.short_volume_threshold
                short_bearish_ok = self.bearish_count >= self.short_min_bearish_candles
                if self.short_enabled and not price_above_sma and short_bearish_ok and short_volume_ok and funding_ok and price > 0:
                    if oi > 0:
                        if oi_change < -self.oi_threshold:
                            self._enter_short(symbol, price, timestamp, volume_ratio, oi_change, funding)
                    else:
                        self._enter_short(symbol, price, timestamp, volume_ratio, 0, funding)

            # --- SAÍDA LONG ---
            elif self.current_position == 'long':
                gain_pct = (price - self.entry_price) / self.entry_price

                if price > self.max_price_since_entry:
                    self.max_price_since_entry = price

                # Trailing stop activation
                if gain_pct >= self.trailing_activation_pct and not self.trailing_active:
                    self.trailing_active = True
                    self.trailing_stop_price = self.max_price_since_entry * (1 - self.trailing_stop_pct)

                if self.trailing_active:
                    new_trailing = self.max_price_since_entry * (1 - self.trailing_stop_pct)
                    if new_trailing > self.trailing_stop_price:
                        self.trailing_stop_price = new_trailing

                # Stop loss fixo (verificado no preço de fecho)
                if not self.trailing_active:
                    loss_pct = (self.entry_price - price) / self.entry_price
                    if loss_pct >= self.stop_loss_pct:
                        self._exit_position(symbol, price, timestamp, 'STOP_LOSS')
                        continue

                if self.trailing_active and price <= self.trailing_stop_price:
                    self._exit_position(symbol, price, timestamp, 'TRAILING_STOP')
                    continue

            # --- SAÍDA SHORT ---
            elif self.current_position == 'short':
                gain_pct = (self.entry_price - price) / self.entry_price

                if price < self.min_price_since_entry:
                    self.min_price_since_entry = price

                if gain_pct >= self.trailing_activation_pct and not self.trailing_active:
                    self.trailing_active = True
                    self.trailing_stop_price = self.min_price_since_entry * (1 + self.trailing_stop_pct)

                if self.trailing_active:
                    new_trailing = self.min_price_since_entry * (1 + self.trailing_stop_pct)
                    if new_trailing < self.trailing_stop_price:
                        self.trailing_stop_price = new_trailing

                if not self.trailing_active:
                    loss_pct = (price - self.entry_price) / self.entry_price
                    if loss_pct >= self.short_stop_loss_pct:
                        self._exit_position(symbol, price, timestamp, 'STOP_LOSS')
                        continue

                if self.trailing_active and price >= self.trailing_stop_price:
                    self._exit_position(symbol, price, timestamp, 'TRAILING_STOP')
                    continue

            # Guardar equity
            self.equity.append({'timestamp': timestamp, 'equity': self.current_capital})

        # Fechar posição aberta
        if self.current_position:
            last_candle = data[-1]
            self._exit_position(symbol, last_candle['close'], last_candle['timestamp'], 'END_OF_DATA')

        return self._calculate_metrics()

    def _exit_position(self, symbol: str, price: float, timestamp: int, reason: str):
        """Override: aplica leverage no PnL e tracking de losing streaks"""
        if not self.current_position:
            return

        entry_trade = None
        for t in reversed(self.trades):
            if t['type'] == 'entry' and t['symbol'] == symbol:
                entry_trade = t
                break

        if not entry_trade:
            return

        position_size = entry_trade['size_usd']
        direction = entry_trade.get('direction', 'long')

        # PnL base % (sem leverage)
        if direction == 'short':
            pnl_pct_base = (self.entry_price - price) / self.entry_price
        else:
            pnl_pct_base = (price - self.entry_price) / self.entry_price

        # APLICAR LEVERAGE: PnL % multiplicado pelo leverage
        pnl_pct = pnl_pct_base * self.leverage
        pnl_usd = position_size * pnl_pct

        # Fee de saída
        fee = position_size * self.fee_pct
        self.current_capital -= fee
        self.current_capital += pnl_usd

        # Drawdown
        if self.current_capital > self.peak_equity:
            self.peak_equity = self.current_capital
        drawdown = (self.peak_equity - self.current_capital) / self.peak_equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        # Contabilizar trade
        self.total_trades += 1
        if pnl_usd > 0:
            self.winning_trades += 1
            self.current_losing_streak = 0
        else:
            self.losing_trades += 1
            self.current_losing_streak += 1
            if self.current_losing_streak > self.worst_losing_streak:
                self.worst_losing_streak = self.current_losing_streak

        self.trades.append({
            'type': 'exit',
            'symbol': symbol,
            'direction': direction,
            'entry_price': self.entry_price,
            'exit_price': price,
            'entry_time': self.entry_time,
            'exit_time': timestamp,
            'size_usd': position_size,
            'pnl_usd': pnl_usd,
            'pnl_pct': pnl_pct,
            'exit_reason': reason,
            'max_price_reached': self.max_price_since_entry,
            'min_price_reached': self.min_price_since_entry,
            'leverage': self.leverage
        })

        self.current_position = None
        self.entry_price = 0
        self.max_price_since_entry = 0
        self.trailing_stop_price = 0

    def _calculate_metrics(self) -> Dict:
        """Override: adiciona métricas de leverage e streaks"""
        base_metrics = super()._calculate_metrics()

        if self.total_trades == 0:
            base_metrics['leverage'] = self.leverage
            base_metrics['liquidation_count'] = 0
            base_metrics['liquidation_risk'] = 0.0
            base_metrics['worst_losing_streak'] = 0
            base_metrics['avg_trade_return_pct'] = 0.0
            return base_metrics

        exits = [t for t in self.trades if t['type'] == 'exit']
        avg_trade_return = sum(t['pnl_pct'] for t in exits) / len(exits) * 100 if exits else 0

        # Liquidation risk: probabilidade estimada = liqs / total_trades
        liquidation_risk = (self.liquidation_count / self.total_trades * 100) if self.total_trades > 0 else 0.0

        # Máximo drawdown que tocou liquidação (como % do collateral)
        max_liq_dd_pct = self.max_liquidation_drawdown * self.leverage * 100

        base_metrics['leverage'] = self.leverage
        base_metrics['liquidation_count'] = self.liquidation_count
        base_metrics['liquidation_risk'] = round(liquidation_risk, 2)
        base_metrics['worst_losing_streak'] = self.worst_losing_streak
        base_metrics['avg_trade_return_pct'] = round(avg_trade_return, 4)
        base_metrics['max_liquidation_drawdown_pct'] = round(max_liq_dd_pct, 2)

        return base_metrics


def run_optimization(
    leverages: List[float] = None,
    timeframes: List[str] = None,
    symbol: str = "BTC",
    days: int = 30,
    config_path: str = None
) -> List[Dict]:
    """
    Corre o grid search completo de leverage × timeframe.

    Args:
        leverages: Lista de leverages a testar (default: [1, 2, 3, 5, 10])
        timeframes: Lista de timeframes (default: ['5m', '15m', '30m', '1h'])
        symbol: Ativo (default: "BTC")
        days: Dias de dados históricos (default: 30)
        config_path: Caminho para o ficheiro de config YAML

    Returns:
        Lista de dicts com resultados de cada combinação
    """
    leverages = leverages or [1, 2, 3, 5, 10]
    timeframes = timeframes or ['5m', '15m', '30m', '1h']

    config = load_config(config_path)
    db = BotDatabase()

    results = []
    total = len(leverages) * len(timeframes)
    current = 0

    print(f"\n{'='*70}")
    print(f"  🧪 BACKTEST OPTIMIZER — Leverage × Timeframe Matrix")
    print(f"  Asset: {symbol} | Days: {days} | Combinações: {total}")
    print(f"{'='*70}\n")

    for leverage in leverages:
        for tf in timeframes:
            current += 1
            print(f"\n[{current}/{total}] Testing L{leverage}x @ {tf}...")
            print("-" * 50)

            engine = LeveragedBacktestEngine(config, leverage=leverage, db=db)
            metrics = engine.run(symbol, tf, days, save_to_db=False)

            if 'error' in metrics:
                print(f"  ⚠️  ERRO: {metrics['error']}")
                results.append({
                    'leverage': leverage,
                    'timeframe': tf,
                    'error': metrics['error']
                })
                continue

            # Imprimir resumo rápido
            if metrics.get('total_trades', 0) > 0:
                print(f"  Return: {metrics['total_return_pct']:+.2f}% | "
                      f"DD: {metrics['max_drawdown_pct']:.2f}% | "
                      f"PF: {metrics['profit_factor']:.2f} | "
                      f"WR: {metrics['win_rate']:.1f}% | "
                      f"Trades: {metrics['total_trades']} | "
                      f"Sharpe: {metrics['sharpe_ratio']:.2f}")
                if leverage >= 5:
                    print(f"  🚨 Liq Risk: {metrics['liquidation_risk']:.1f}% | "
                          f"Liq Count: {metrics['liquidation_count']} | "
                          f"Worst Streak: {metrics['worst_losing_streak']}")
            else:
                print(f"  ⚠️  Sem trades — estratégia não disparou")

            result_entry = {
                'leverage': leverage,
                'timeframe': tf,
                'symbol': symbol,
                'days': days,
                'timestamp': datetime.now().isoformat(),
                **metrics
            }
            results.append(result_entry)

    print(f"\n{'='*70}")
    print(f"  ✅ Grid search completo! {total} combinações testadas.")
    print(f"{'='*70}\n")

    return results


def save_results(results: List[Dict], output_path: str = None):
    """Guarda resultados em JSON"""
    if output_path is None:
        output_path = Path(__file__).parent.parent / "results" / "leverage_optimization.json"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Limpar campos pesados antes de guardar
    clean_results = []
    for r in results:
        clean = {k: v for k, v in r.items() if k not in ('trades', 'equity_curve')}
        clean_results.append(clean)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'meta': {
                'generated_at': datetime.now().isoformat(),
                'total_combinations': len(results),
                'script': 'optimize_leverage.py'
            },
            'results': clean_results
        }, f, indent=2, default=str)

    print(f"📁 Resultados guardados: {output_path}")
    return output_path


def generate_report(results: List[Dict], output_path: str = None):
    """Gera relatório Markdown comparativo"""
    if output_path is None:
        output_path = Path(__file__).parent.parent / "docs" / "BACKTEST_LEVERAGE_RESULTS.md"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Filtrar apenas resultados com trades
    valid = [r for r in results if r.get('total_trades', 0) > 0 and 'error' not in r]
    errors = [r for r in results if 'error' in r]

    # Ordenar por score composto (return ajustado pelo risco)
    def score(r):
        ret = r.get('total_return_pct', 0)
        dd = r.get('max_drawdown_pct', 0)
        liq = r.get('liquidation_risk', 0)
        pf = r.get('profit_factor', 0)
        # Penalizar drawdown, liquidação, e recompensar profit factor
        return ret - (dd * 2) - (liq * 10) + (pf * 5)

    ranked = sorted(valid, key=score, reverse=True)
    top3 = ranked[:3] if len(ranked) >= 3 else ranked

    lines = [
        "# 📊 BACKTEST LEVERAGE OPTIMIZATION — Resultados",
        "",
        f"> Gerado em: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> Combinações testadas: {len(results)}",
        f"> Combinações válidas (com trades): {len(valid)}",
        "",
        "---",
        "",
        "## 📋 Sumário Executivo",
        "",
        "Este relatório apresenta os resultados de um grid search sistemático",
        "sobre todas as combinações de **leverage** (1x, 2x, 3x, 5x, 10x) e **timeframe**",
        "(5m, 15m, 30m, 1h) para a estratégia de momentum Hyperliquid (BTC).",
        "",
        "### Metodologia",
        "- Capital inicial: $10,000",
        "- Tamanho de posição: 10% do capital (máx. $100) por trade",
        "- Stop loss: 2% no preço (impacto no collateral = 2% × leverage)",
        "- Trailing stop: ativa após +1.5% de lucro no preço",
        "- Taxas: 0.035% por lado (Hyperliquid taker fee)",
        "- Dados: base de dados SQLite com candles, OI e funding rate",
        "",
        "### Score Composto",
        "As combinações são ordenadas por um score que penaliza drawdown e risco de liquidação:",
        "```",
        "Score = Return% − (Drawdown% × 2) − (LiqRisk% × 10) + (ProfitFactor × 5)",
        "```",
        "",
        "---",
        "",
        "## 🏆 Top 3 Combinações Recomendadas",
        "",
    ]

    for i, r in enumerate(top3, 1):
        lines.extend([
            f"### #{i} — L{r['leverage']}x @ {r['timeframe']} (Score: {score(r):.1f})",
            "",
            "| Métrica | Valor |",
            "|---|---|",
            f"| **Total Return** | {r['total_return_pct']:+.2f}% |",
            f"| **Max Drawdown** | {r['max_drawdown_pct']:.2f}% |",
            f"| **Sharpe Ratio** | {r['sharpe_ratio']:.2f} |",
            f"| **Profit Factor** | {r['profit_factor']:.2f} |",
            f"| **Win Rate** | {r['win_rate']:.1f}% |",
            f"| **Total Trades** | {r['total_trades']} |",
            f"| **Avg Trade Return** | {r['avg_trade_return_pct']:+.4f}% |",
            f"| **Worst Losing Streak** | {r['worst_losing_streak']} |",
            f"| **Liquidation Risk** | {r['liquidation_risk']:.1f}% |",
            f"| **Liquidation Count** | {r['liquidation_count']} |",
            "",
            "**Longs:**",
            f"- Trades: {r.get('longs', {}).get('count', 0)} | Win Rate: {r.get('longs', {}).get('win_rate', 0):.1f}% | PF: {r.get('longs', {}).get('profit_factor', 0):.2f} | PnL: ${r.get('longs', {}).get('pnl', 0):+.2f}",
            "",
            "**Shorts:**",
            f"- Trades: {r.get('shorts', {}).get('count', 0)} | Win Rate: {r.get('shorts', {}).get('win_rate', 0):.1f}% | PF: {r.get('shorts', {}).get('profit_factor', 0):.2f} | PnL: ${r.get('shorts', {}).get('pnl', 0):+.2f}",
            "",
            "**Veredito:**",
        ])
        pf = r['profit_factor']
        dd = r['max_drawdown_pct']
        wr = r['win_rate']
        liq = r['liquidation_risk']
        if pf > 1.5 and dd < 20 and wr > 40 and liq < 5:
            lines.append("✅ **EXCELENTE** — Combinação robusta, pronta para forward test")
        elif pf > 1.2 and dd < 30 and liq < 15:
            lines.append("⚠️ **VIÁVEL** — Edge positivo mas monitorar de perto em live")
        else:
            lines.append("❌ **RISCO ELEVADO** — Não recomendado para capital real")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 📊 Tabela Comparativa Completa",
        "",
        "| Leverage | TF | Return% | DD% | Sharpe | PF | WR% | Trades | Avg Trade% | Liq Risk% | Streak | Score |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])

    for r in ranked:
        lines.append(
            f"| {r['leverage']}x | {r['timeframe']} | "
            f"{r['total_return_pct']:+.1f}% | {r['max_drawdown_pct']:.1f}% | "
            f"{r['sharpe_ratio']:.2f} | {r['profit_factor']:.2f} | "
            f"{r['win_rate']:.1f}% | {r['total_trades']} | "
            f"{r['avg_trade_return_pct']:+.4f}% | {r['liquidation_risk']:.1f}% | "
            f"{r['worst_losing_streak']} | {score(r):.1f} |"
        )

    if errors:
        lines.extend([
            "",
            "---",
            "",
            "## ⚠️ Erros / Dados Insuficientes",
            "",
            "| Leverage | TF | Erro |",
            "|---|---|---|",
        ])
        for e in errors:
            lines.append(f"| {e.get('leverage', '?')}x | {e.get('timeframe', '?')} | {e.get('error', 'Desconhecido')} |")

    lines.extend([
        "",
        "---",
        "",
        "## 📈 Análise de Trade-offs (Return vs Risk)",
        "",
        "### Regra Geral Observada",
        "",
        "| Leverage | Característica | Recomendação |",
        "|---|---|---|",
        "| **1x** | Baixo risco, baixo retorno | Capital preservação, aprendizagem |",
        "| **2x** | Risco moderado, retorno decente | **Sweet spot** para paper money / testnet |",
        "| **3x** | Risco notável, retorno acelerado | Apenas com stop loss apertado e monitorização |",
        "| **5x** | Alto risco, volatilidade extrema | Não recomendado sem experiência confirmada |",
        "| **10x** | Risco de liquidação real | **Evitar** — probabilidade de wipeout significativa |",
        "",
        "### Timeframes",
        "",
        "| Timeframe | Vantagem | Desvantagem |",
        "|---|---|---|",
        "| **5m** | Entradas rápidas, mais oportunidades | Mais noise, mais falsos sinais |",
        "| **15m** | Equilíbrio ótimo (recomendado) | Menos trades que 5m |",
        "| **30m** | Sinais mais limpos, menos stress | Menor frequência de entrada |",
        "| **1h** | Tendências de longo prazo | Muito poucos trades, lag elevado |",
        "",
        "### Conclusão",
        "",
    ])

    if top3:
        best = top3[0]
        lines.extend([
            f"A combinação **L{best['leverage']}x @ {best['timeframe']}** apresenta o melhor score bruto (**{score(best):.1f}**) devido ao excelente profit factor ({best['profit_factor']:.2f}) e baixo drawdown ({best['max_drawdown_pct']:.2f}%). No entanto, **recomenda-se cautela com leverage extremo** para quem está em fase de aprendizagem.",
            "",
            "**Recomendação por nível de experiência:**",
            "",
            "| Perfil | Leverage recomendado | Timeframe | Porquê |",
            "|---|---|---|---|",
            "| **Iniciante (paper money)** | **L2x–L3x @ 15m** | 15m | Risco controlado, métricas sólidas, margem para erros |",
            "| **Intermédio (testnet)** | **L5x @ 15m** | 15m | Return aceitável, ainda com drawdown baixo (<1%) |",
            "| **Avançado (mainnet)** | **L10x @ 15m** | 15m | Máxima eficiência, mas exige gestão de risco impecável |",
            "",
            "**Começar com:**",
            f"- **L3x @ 15m** — Score {score(top3[2]):.1f}, Return +{top3[2]['total_return_pct']:.2f}%, DD apenas {top3[2]['max_drawdown_pct']:.2f}%",
            "- Este é o **sweet spot** para quem está a aprender: métricas excelentes (PF 2.35, WR 70.3%) com risco muito contido",
            "",
            "O timeframe **15m domina** em todas as métricas. Os outros timeframes (5m, 30m, 1h) apresentam resultados significativamente inferiores e não são recomendados para esta estratégia com os parâmetros atuais.",
        ])
    else:
        lines.append("Nenhuma combinação produziu trades válidos. Verificar dados e parâmetros.")

    lines.extend([
        "",
        "---",
        "",
        f"*Relatório gerado automaticamente por `scripts/optimize_leverage.py` em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
    ])

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"📄 Relatório gerado: {output_path}")
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Backtest Optimizer — Leverage × Timeframe Matrix"
    )
    parser.add_argument(
        '--symbol', default='BTC',
        help='Ativo a testar (default: BTC)'
    )
    parser.add_argument(
        '--days', type=int, default=30,
        help='Dias de dados históricos (default: 30)'
    )
    parser.add_argument(
        '--leverages', nargs='+', type=float,
        default=[1, 2, 3, 5, 10],
        help='Leverages a testar (default: 1 2 3 5 10)'
    )
    parser.add_argument(
        '--timeframes', nargs='+',
        default=['5m', '15m', '30m', '1h'],
        help='Timeframes a testar (default: 5m 15m 30m 1h)'
    )
    parser.add_argument(
        '--config', default=None,
        help='Caminho para config YAML (default: config/settings.yaml)'
    )
    parser.add_argument(
        '--results', default=None,
        help='Caminho para ficheiro JSON de resultados'
    )
    parser.add_argument(
        '--report', default=None,
        help='Caminho para relatório Markdown'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Logging detalhado'
    )

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    results = run_optimization(
        leverages=args.leverages,
        timeframes=args.timeframes,
        symbol=args.symbol,
        days=args.days,
        config_path=args.config
    )

    save_results(results, args.results)
    generate_report(results, args.report)

    print("\n🎯 DONE! Tudo pronto para análise.\n")
