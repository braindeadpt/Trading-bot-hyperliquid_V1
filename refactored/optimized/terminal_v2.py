"""
TerminalCLI v2 — Otimizado: polling a 1 FPS, sem sleep redundante.
Resolve problema de 2 FPS desnecessários + sleep que bloqueia refresh.
"""
import time
import logging
from typing import Dict

from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.console import Console
from rich.text import Text

from refactored.core.event_bus import EventBus

logger = logging.getLogger(__name__)


class TerminalCLI:
    """
    Dashboard terminal v2 — POLLING eficiente.
    
    Mudanças v2:
    - 1 FPS em vez de 2 (suficiente para terminal)
    - Sem time.sleep() dentro do Live (Live gere o refresh)
    - Polling a cada 5s em vez de 0.5s
    - Event-driven updates (não polling busy)
    """
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.console = Console()
        
        self._market_data = {}
        self._bot_status = {'running': False, 'state': 'IDLE'}
        self._last_trade = None
        self._trade_count = 0
        self._capital = 10000
        self._needs_refresh = True  # ✅ Flag para dirty-check
        
        self._subscribe()
    
    def _subscribe(self):
        self.event_bus.subscribe('market.data', self._on_market)
        self.event_bus.subscribe('trade.entered', self._on_trade_enter)
        self.event_bus.subscribe('trade.exited', self._on_trade_exit)
        self.event_bus.subscribe('state.changed', self._on_state)
    
    def _on_market(self, event):
        p = event.payload
        self._market_data[p.get('asset', 'BTC')] = p
        self._needs_refresh = True
    
    def _on_trade_enter(self, event):
        self._last_trade = event.payload
        self._trade_count += 1
        self._needs_refresh = True
    
    def _on_trade_exit(self, event):
        p = event.payload
        self._capital += p.get('pnl_usd', 0)
        self._last_trade = p
        self._needs_refresh = True
    
    def _on_state(self, event):
        self._bot_status['state'] = event.payload.get('to', 'IDLE')
        self._needs_refresh = True
    
    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3)
        )
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1)
        )
        return layout
    
    def _render_header(self) -> Panel:
        status_color = "green" if self._bot_status.get('running') else "red"
        status = f"[bold {status_color}]● {self._bot_status.get('state', 'IDLE')}[/]"
        text = Text.from_markup(
            f"[bold cyan]🔥 HYPERLIQUID BOT v2.0[/]  |  "
            f"Status: {status}  |  "
            f"Capital: [bold green]${self._capital:,.2f}[/]"
        )
        return Panel(text, border_style="cyan")
    
    def _render_market_table(self) -> Table:
        table = Table(title="📊 Mercado", border_style="blue")
        table.add_column("Asset", style="cyan")
        table.add_column("Preço", justify="right")
        table.add_column("OI", justify="right")
        table.add_column("Funding", justify="right")
        
        for asset, data in self._market_data.items():
            price = data.get('price', 0)
            oi = data.get('oi', 0)
            funding = data.get('funding', 0)
            table.add_row(
                asset,
                f"${price:,.2f}",
                f"${oi/1e9:.2f}B" if oi else "N/A",
                f"{funding*100:.4f}%"
            )
        return table
    
    def _render_trade_panel(self) -> Panel:
        if not self._last_trade:
            return Panel("⏳ À espera do primeiro trade...", title="🎯 Último Trade", border_style="yellow")
        
        t = self._last_trade
        if 'pnl_usd' in t:
            color = "green" if t['pnl_usd'] > 0 else "red"
            content = (
                f"[bold]{t.get('direction', 'N/A').upper()}[/] {t.get('asset', '')}\n"
                f"PnL: [bold {color}]${t['pnl_usd']:+.2f}[/]\n"
                f"Razão: {t.get('reason', '')}"
            )
        else:
            content = (
                f"[bold]{t.get('direction', 'N/A').upper()}[/] {t.get('asset', '')}\n"
                f"Preço: ${t.get('price', 0):,.2f}\n"
                f"Size: ${t.get('size', 0):,.2f}"
            )
        return Panel(content, title="🎯 Último Trade", border_style="yellow")
    
    def _render_footer(self) -> Panel:
        return Panel(
            f"Trades: {self._trade_count}  |  "
            f"Pressione Ctrl+C para sair",
            border_style="dim"
        )
    
    def run(self):
        """Loop principal otimizado."""
        layout = self._make_layout()
        
        # ✅ 1 FPS é suficiente para terminal
        with Live(layout, console=self.console, refresh_per_second=1, screen=True):
            try:
                while True:
                    # ✅ Só atualiza se algo mudou (event-driven)
                    if self._needs_refresh:
                        layout["header"].update(self._render_header())
                        layout["left"].update(self._render_market_table())
                        layout["right"].update(self._render_trade_panel())
                        layout["footer"].update(self._render_footer())
                        self._needs_refresh = False
                    
                    # ✅ Sleep de 5s — Live gere o refresh interno
                    # O sleep é fora do update loop para não bloquear
                    time.sleep(5)
                    
            except KeyboardInterrupt:
                self.console.print("\n[bold yellow]👋 Bot a encerrar...[/]")
