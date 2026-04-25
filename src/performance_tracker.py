"""
Performance Tracker v2 - Gera relatórios diários/semanais da base de dados
Corre automaticamente ou sob demanda
"""
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from database import BotDatabase

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """Análise de performance com base nos dados da base de dados"""
    
    def __init__(self, db: Optional[BotDatabase] = None):
        self.db = db or BotDatabase()
    
    def generate_daily_report(self, asset: str = 'BTC', date: Optional[str] = None) -> Dict:
        """
        Gera relatório de performance para um dia específico
        Se date=None, usa o dia anterior (último dia completo)
        """
        if date is None:
            date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # Converter date para timestamps
        date_start = datetime.strptime(date, '%Y-%m-%d')
        date_end = date_start + timedelta(days=1)
        ts_start = int(date_start.timestamp() * 1000)
        ts_end = int(date_end.timestamp() * 1000)
        
        logger.info(f"Gerando relatório para {asset} @ {date}")
        
        # Buscar trades do dia
        conn = self.db._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM paper_trades
            WHERE symbol = ?
            AND exit_time IS NOT NULL
            AND (
                (entry_time >= ? AND entry_time < ?) OR
                (exit_time >= ? AND exit_time < ?)
            )
        ''', (asset, ts_start, ts_end, ts_start, ts_end))
        
        trades = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        if not trades:
            logger.info(f"Sem trades para {asset} em {date}")
            return {
                'date': date,
                'asset': asset,
                'total_trades': 0,
                'message': 'Sem trades neste dia'
            }
        
        # Separar wins/losses
        wins = [t for t in trades if (t.get('pnl_usd') or 0) > 0]
        losses = [t for t in trades if (t.get('pnl_usd') or 0) <= 0]
        
        total_pnl = sum(t.get('pnl_usd', 0) for t in trades)
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0
        
        avg_win = sum(t.get('pnl_usd', 0) for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.get('pnl_usd', 0) for t in losses) / len(losses)) if losses else 0
        
        profit_factor = sum(t.get('pnl_usd', 0) for t in wins) / abs(sum(t.get('pnl_usd', 0) for t in losses)) if losses and sum(t.get('pnl_usd', 0) for t in losses) != 0 else float('inf')
        
        # Separar long/short
        longs = [t for t in trades if t.get('side') == 'long']
        shorts = [t for t in trades if t.get('side') == 'short']
        
        # Calcular drawdown (simplificado)
        peak = 0
        max_dd = 0
        running_pnl = 0
        for t in sorted(trades, key=lambda x: x.get('exit_time', '')):
            running_pnl += t.get('pnl_usd', 0)
            if running_pnl > peak:
                peak = running_pnl
            dd = peak - running_pnl
            if dd > max_dd:
                max_dd = dd
        
        # Duração média de trades
        durations = []
        for t in trades:
            entry = t.get('entry_time')
            exit_t = t.get('exit_time')
            if entry and exit_t:
                try:
                    # Tentar parse de diferentes formatos
                    for fmt in ['%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                        try:
                            e = datetime.strptime(entry[:26], fmt)
                            x = datetime.strptime(exit_t[:26], fmt)
                            durations.append((x - e).total_seconds() / 60)
                            break
                        except:
                            continue
                except:
                    pass
        
        avg_duration = sum(durations) / len(durations) if durations else 0
        
        report = {
            'date': date,
            'asset': asset,
            'total_trades': len(trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': round(win_rate, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
            'total_pnl': round(total_pnl, 2),
            'max_drawdown': round(max_dd, 2),
            'avg_trade_duration': round(avg_duration, 1),
            'long_trades': len(longs),
            'short_trades': len(shorts),
            'long_pnl': round(sum(t.get('pnl_usd', 0) for t in longs), 2),
            'short_pnl': round(sum(t.get('pnl_usd', 0) for t in shorts), 2),
        }
        
        # Guardar na DB
        self.db.save_performance_log(report)
        
        return report
    
    def generate_summary(self, asset: str = 'BTC', days: int = 7) -> Dict:
        """Gera sumário dos últimos N dias"""
        logs = self.db.get_performance_log(asset=asset, days=days)
        
        if not logs:
            return {'message': f'Sem dados para {asset} nos últimos {days} dias'}
        
        total_trades = sum(l.get('total_trades', 0) for l in logs)
        total_wins = sum(l.get('winning_trades', 0) for l in logs)
        total_pnl = sum(l.get('total_pnl', 0) for l in logs)
        
        return {
            'asset': asset,
            'period_days': days,
            'days_with_data': len(logs),
            'total_trades': total_trades,
            'overall_win_rate': round((total_wins / total_trades * 100) if total_trades > 0 else 0, 2),
            'total_pnl': round(total_pnl, 2),
            'avg_daily_trades': round(total_trades / len(logs), 1),
            'daily_logs': logs
        }
    
    def print_report(self, report: Dict):
        """Imprime relatório formatado"""
        print("\n" + "="*70)
        print(f"📊 PERFORMANCE REPORT - {report.get('asset', 'BTC')} | {report.get('date', 'N/A')}")
        print("="*70)
        
        if report.get('total_trades', 0) == 0:
            print(report.get('message', 'Sem trades'))
            print("="*70)
            return
        
        print(f"Total Trades:        {report['total_trades']:>8}")
        print(f"Win Rate:            {report['win_rate']:>7.1f}%")
        print(f"Profit Factor:       {report['profit_factor']:>8.2f}")
        print(f"Total PnL:           ${report['total_pnl']:>10,.2f}")
        print(f"Max Drawdown:        ${report['max_drawdown']:>10,.2f}")
        print(f"Avg Trade Duration:  {report['avg_trade_duration']:>7.1f} min")
        print("-"*70)
        print(f"Longs:  {report['long_trades']:>3} trades | PnL: ${report['long_pnl']:>10,.2f}")
        print(f"Shorts: {report['short_trades']:>3} trades | PnL: ${report['short_pnl']:>10,.2f}")
        print("="*70)
        
        # Veredito
        pf = report['profit_factor']
        wr = report['win_rate']
        
        if pf > 1.5 and wr > 45:
            print("[✅ BOM] Edge positivo claro")
        elif pf > 1.2:
            print("[⚠️ OK] Edge marginal — precisa de mais dados")
        else:
            print("[❌ FRACO] Não lucrativo — rever estratégia")
        print("="*70)
    
    def generate_daily_if_needed(self, asset: str = 'BTC'):
        """Gera relatório do dia anterior se ainda não existir"""
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        # Verificar se já existe
        logs = self.db.get_performance_log(asset=asset, days=1)
        if logs and logs[0].get('date') == yesterday:
            logger.info(f"Relatório {yesterday} já existe. Skipping.")
            return None
        
        return self.generate_daily_report(asset, yesterday)
    
    def get_signal_analysis(self, asset: str = 'BTC', days: int = 7) -> Dict:
        """Analisa sinais: quantos gerados vs quantos executados"""
        ts_start = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        
        signals = self.db.get_signals(asset=asset, start_time=ts_start, limit=10000)
        
        if not signals:
            return {'message': 'Sem sinais no período'}
        
        total = len(signals)
        executed = len([s for s in signals if s.get('executed')])
        rejected = total - executed
        
        # Razões de rejeição
        reasons = {}
        for s in signals:
            if not s.get('executed'):
                reason = s.get('reason', 'unknown')
                # Simplificar reason
                if 'FILTER:' in reason:
                    key = reason.replace('FILTER:', '').strip().split('|')[0]
                elif 'VP_BLOCK' in reason:
                    key = 'volume_profile'
                else:
                    key = reason
                reasons[key] = reasons.get(key, 0) + 1
        
        return {
            'asset': asset,
            'period_days': days,
            'total_signals': total,
            'executed': executed,
            'rejected': rejected,
            'execution_rate': round((executed / total * 100), 1) if total > 0 else 0,
            'rejection_reasons': reasons,
            'long_signals': len([s for s in signals if s.get('signal_type') == 'LONG']),
            'short_signals': len([s for s in signals if s.get('signal_type') == 'SHORT']),
        }


def run_daily_report(asset: str = 'BTC', print_output: bool = True):
    """Entry point para relatório diário"""
    tracker = PerformanceTracker()
    report = tracker.generate_daily_if_needed(asset)
    
    if report and print_output:
        tracker.print_report(report)
    
    return report


def run_full_analysis(asset: str = 'BTC', days: int = 7):
    """Análise completa: performance + sinais"""
    tracker = PerformanceTracker()
    
    print("\n" + "🔥"*35)
    print("       FULL PERFORMANCE ANALYSIS")
    print("🔥"*35)
    
    # Performance
    perf = tracker.generate_summary(asset, days)
    print(f"\n📈 Performance ({days} dias):")
    print(json.dumps(perf, indent=2, default=str))
    
    # Sinais
    signals = tracker.get_signal_analysis(asset, days)
    print(f"\n📡 Signal Analysis ({days} dias):")
    print(json.dumps(signals, indent=2, default=str))
    
    # Último relatório diário
    latest = tracker.generate_daily_report(asset)
    if latest:
        tracker.print_report(latest)
    
    return {'performance': perf, 'signals': signals}


if __name__ == "__main__":
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    asset = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    
    if '--daily' in sys.argv:
        run_daily_report(asset)
    else:
        run_full_analysis(asset, days)
