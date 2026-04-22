"""
Dashboard Rich - Interface visual no terminal
Abre numa nova janela/janela separada do terminal
"""
import time
import logging
import threading
from datetime import datetime
from typing import Dict, Optional
from pathlib import Path

# Rich imports
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.progress import Progress, BarColumn, TextColumn
    from rich.text import Text
    from rich.align import Align
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Rich não instalado. Instala com: pip install rich")
    print("A correr com logging normal...")

from utils import load_config
from data_aggregator import DataAggregator
from strategy import MomentumStrategy

logger = logging.getLogger(__name__)


class BotDashboard:
    """Dashboard visual para o bot de trading"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.console = Console()
        self.aggregator = DataAggregator(config)
        self.strategy = MomentumStrategy(config)
        
        self.assets = config['assets']
        self.polling_interval = config['polling']['oi_interval']
        
        # Dados atuais por asset
        self.current_data = {}
        self.last_update = "N/A"
        self.status = "[green]RUNNING[/green]"
        self.total_signals = 0
        
        self.running = False
    
    def _make_layout(self) -> Layout:
        """Cria layout do dashboard"""
        layout = Layout()
        
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3)
        )
        
        layout["main"].split_row(
            Layout(name="assets"),
            Layout(name="signals", size=40)
        )
        
        return layout
    
    def _make_header(self) -> Panel:
        """Header com info do bot"""
        title = f"[bold cyan]Hyperliquid Momentum Bot v{self.config['bot']['version']}[/bold cyan]"
        subtitle = f"Paper Trading: {self.config['bot']['paper_trading']} | Assets: {', '.join(self.assets)}"
        return Panel(
            Align.center(f"{title}\n{subtitle}"),
            border_style="cyan"
        )
    
    def _make_assets_table(self) -> Table:
        """Tabela com dados dos assets"""
        table = Table(
            title="[bold]Assets em Monitorização[/bold]",
            header_style="bold magenta",
            border_style="blue"
        )
        
        table.add_column("Asset", style="cyan", no_wrap=True)
        table.add_column("Preço", justify="right", style="green")
        table.add_column("OI Global", justify="right", style="yellow")
        table.add_column("OI Δ", justify="right")
        table.add_column("Volume", justify="right", style="blue")
        table.add_column("Funding", justify="right", style="magenta")
        table.add_column("Sinal", justify="center")
        
        for asset in self.assets:
            data = self.current_data.get(asset, {})
            
            price = data.get('price', 0)
            oi = data.get('oi_total', 0)
            oi_change = data.get('oi_change_pct', 0)
            volume_ratio = data.get('volume_ratio', 0)
            funding = data.get('funding_avg', 0)
            signal = data.get('signal', '')
            
            # Cores condicionais
            oi_color = "green" if oi_change > 0 else "red" if oi_change < 0 else "white"
            vol_color = "green" if volume_ratio > 1.5 else "white"
            signal_style = "[bold green]LONG[/bold green]" if signal == 'LONG' else "[dim]-[/dim]"
            
            table.add_row(
                asset,
                f"${price:,.2f}" if price else "N/A",
                f"${oi:,.0f}" if oi else "N/A",
                f"[{oi_color}]{oi_change*100:+.2f}%[/{oi_color}]" if oi else "N/A",
                f"[{vol_color}]{volume_ratio:.1f}x[/{vol_color}]" if volume_ratio else "N/A",
                f"{funding*100:.4f}%" if funding else "N/A",
                signal_style
            )
        
        return table
    
    def _make_signals_panel(self) -> Panel:
        """Painel com últimos sinais"""
        content = "[bold]Últimos Sinais[/bold]\n\n"
        
        # Simulação - em produção isto viria do strategy
        content += "[dim]Aguardando dados...[/dim]\n"
        content += f"\nTotal sinais hoje: [yellow]{self.total_signals}[/yellow]"
        
        return Panel(content, border_style="green")
    
    def _make_footer(self) -> Panel:
        """Footer com status"""
        now = datetime.now().strftime("%H:%M:%S")
        content = f"Última atualização: {self.last_update} | Hora: {now} | Status: {self.status}"
        return Panel(Align.center(content), border_style="dim")
    
    def update_data(self):
        """Busca novos dados das APIs"""
        for asset in self.assets:
            try:
                data = self.aggregator.fetch_all_data(asset)
                if data:
                    # Calcular métricas extras
                    hl_data = data['exchanges_data'].get('hyperliquid', {})
                    price = hl_data.get('mark_price', 0)
                    
                    # Adicionar ao current_data
                    self.current_data[asset] = {
                        'price': price,
                        'oi_total': data.get('oi_total', 0),
                        'oi_change_pct': data.get('oi_change_pct', 0),
                        'volume_ratio': 0,  # Calculado no strategy
                        'funding_avg': data.get('funding_avg', 0),
                        'signal': ''
                    }
                    
                    # Verificar sinal
                    if price > 0:
                        signal = self.strategy.analyze(data, price)
                        if signal:
                            self.current_data[asset]['signal'] = signal
                            self.total_signals += 1
                
            except Exception as e:
                logger.warning(f"Erro a buscar {asset}: {e}")
        
        self.last_update = datetime.now().strftime("%H:%M:%S")
    
    def run(self):
        """Inicia o dashboard em tempo real"""
        if not RICH_AVAILABLE:
            print("Rich não está disponível. Instala com: pip install rich")
            return
        
        self.running = True
        
        layout = self._make_layout()
        
        try:
            with Live(layout, console=self.console, refresh_per_second=2) as live:
                while self.running:
                    # Atualizar dados
                    self.update_data()
                    
                    # Atualizar layout
                    layout["header"].update(self._make_header())
                    layout["assets"].update(self._make_assets_table())
                    layout["signals"].update(self._make_signals_panel())
                    layout["footer"].update(self._make_footer())
                    
                    # Aguardar
                    time.sleep(self.polling_interval)
                    
        except KeyboardInterrupt:
            self.running = False
            self.console.print("\n[bold red]Dashboard interrompido[/bold red]")


def main():
    """Entry point do dashboard"""
    if not RICH_AVAILABLE:
        print("="*60)
        print("INSTALAR RICH:")
        print("  pip install rich")
        print("="*60)
        return
    
    config = load_config()
    
    print("A iniciar dashboard...")
    print("Pressiona Ctrl+C para parar")
    
    dashboard = BotDashboard(config)
    dashboard.run()


if __name__ == "__main__":
    main()
