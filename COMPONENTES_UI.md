# 🎨 Componentes UI — Documentação de Produção

## Visão Geral

Sistema de **Web Components** nativos (Custom Elements + Shadow DOM) para o dashboard do bot Hyperliquid. Sem dependências externas — funciona em qualquer browser moderno.

**Stack:** Vanilla JS + CSS Custom Properties + Shadow DOM

---

## Arquitetura do Componente

```
┌─────────────────────────────────────┐
│         Custom Element              │
│  ┌─────────────────────────────┐  │
│  │      Shadow DOM               │  │
│  │  ┌─────────────────────┐    │  │
│  │  │  Styles isolados    │    │  │
│  │  │  (não vazam CSS)    │    │  │
│  │  └─────────────────────┘    │  │
│  │  ┌─────────────────────┐    │  │
│  │  │  Template HTML      │    │  │
│  │  │  (estrutura)        │    │  │
│  │  └─────────────────────┘    │  │
│  │  ┌─────────────────────┐    │  │
│  │  │  Lógica JS          │    │  │
│  │  │  (props, render)    │    │  │
│  │  └─────────────────────┘    │  │
│  └─────────────────────────────┘  │
└─────────────────────────────────────┘
```

### Princípios

1. **Encapsulamento** — Shadow DOM isolado, CSS não conflita
2. **Reusabilidade** — Props via atributos HTML, comportamento previsível
3. **Acessibilidade** — ARIA labels, roles, live regions
4. **Responsividade** — Media queries dentro do shadow DOM
5. **Graceful Degradation** — Funciona sem JS (mostra fallback)

---

## Componentes Disponíveis

### 1. `<status-indicator>`

Indicador visual do estado do bot.

#### Props (Attributes)

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `status` | string | `"loading"` | Estado: `running`, `stopped`, `warning`, `loading` |
| `message` | string | `""` | Mensagem contextual opcional |

#### Estados

```html
<!-- Em execução -->
<status-indicator status="running"></status-indicator>

<!-- Com mensagem -->
<status-indicator status="warning" message="API lenta"></status-indicator>

<!-- A carregar -->
<status-indicator status="loading"></status-indicator>
```

#### Acessibilidade

- `role="status"` — região de status dinâmica
- `aria-live="polite"` — anuncia mudanças sem interromper
- `aria-label` descreve o estado em texto

---

### 2. `<price-ticker>`

Card de preço em tempo real com variação.

#### Props

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `asset` | string | `"BTC"` | Símbolo do asset |
| `price` | number | `0` | Preço atual |
| `change` | number | `0` | Variação percentual (ex: `1.2` para +1.2%) |
| `loading` | boolean | `false` | Mostra shimmer de loading |

#### Exemplos

```html
<!-- Com dados -->
<price-ticker asset="BTC" price="77320.50" change="1.2"></price-ticker>

<!-- Loading -->
<price-ticker asset="ETH" loading="true"></price-ticker>

<!-- Negativo -->
<price-ticker asset="SOL" price="145.30" change="-2.5"></price-ticker>
```

#### Edge Cases

- `price="0"` ou inválido → mostra "—"
- `change` ausente → mostra "—"
- Mobile: card empilha verticalmente

---

### 3. `<signal-card>`

Card completo de sinal de trading.

#### Props

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `asset` | string | `"BTC"` | Asset |
| `direction` | string | `"HOLD"` | `LONG`, `SHORT`, ou `HOLD` |
| `confidence` | number | `0` | 0.0 a 1.0 |
| `entry` | number | `0` | Preço de entrada |
| `stop` | number | `0` | Stop loss |
| `target` | number | `0` | Take profit |
| `reason` | string | `""` | Razão do sinal |
| `timestamp` | string | `""` | ISO 8601 |
| `loading` | boolean | `false` | Loading state |

#### Exemplos

```html
<!-- Sinal LONG -->
<signal-card
  asset="BTC"
  direction="LONG"
  confidence="0.85"
  entry="77320"
  stop="75000"
  target="82000"
  reason="Volume spike + OI increase"
  timestamp="2026-04-26T09:30:00Z">
</signal-card>

<!-- Sem sinal -->
<signal-card direction="HOLD"></signal-card>

<!-- Loading -->
<signal-card loading="true"></signal-card>
```

#### Estados

| Direção | Cor | Ícone | Glow |
|---------|-----|-------|------|
| LONG | 🟢 `#10b981` | ▲ | Verde |
| SHORT | 🔴 `#ef4444` | ▼ | Vermelho |
| HOLD | ⚪ `#6b7280` | ⏸️ | Nenhum |

---

### 4. `<position-panel>`

Painel de posição aberta com PnL em tempo real.

#### Props

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `asset` | string | `"BTC"` | Asset |
| `direction` | string | `"FLAT"` | `LONG`, `SHORT`, `FLAT` |
| `entry` | number | `0` | Preço de entrada |
| `current` | number | `0` | Preço atual |
| `size` | number | `0` | Tamanho em USD |
| `leverage` | number | `1` | Alavancagem |
| `pnl` | number | `0` | PnL em USD |
| `pnl-pct` | number | `0` | PnL percentual |
| `loading` | boolean | `false` | Loading |

#### Exemplos

```html
<!-- Posição LONG -->
<position-panel
  asset="BTC"
  direction="LONG"
  entry="76500"
  current="77320"
  size="100"
  leverage="2"
  pnl="820"
  pnl-pct="1.07">
</position-panel>

<!-- Sem posição -->
<position-panel direction="FLAT"></position-panel>

<!-- Loading -->
<position-panel loading="true"></position-panel>
```

#### Acessibilidade

- `aria-live="polite"` no PnL — anuncia atualizações
- `role="region"` — landmark navegável

---

### 5. `<performance-chart>`

Gráfico de linha SVG de performance ao longo do tempo.

#### Props

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `data` | JSON | `[]` | Array de objetos `{date, value}` |
| `initial` | number | `10000` | Capital inicial |
| `loading` | boolean | `false` | Loading |

#### Exemplos

```html
<!-- Com dados -->
<performance-chart
  data='[{"date":"2026-04-20","value":10000},{"date":"2026-04-21","value":10200}]'
  initial="10000">
</performance-chart>

<!-- Vazio -->
<performance-chart initial="10000"></performance-chart>

<!-- Loading -->
<performance-chart loading="true"></performance-chart>
```

#### Edge Cases

- Dados inválidos → mensagem "Sem dados"
- Um só ponto → mostra ponto isolado
- Valor zero → tratado como válido

---

### 6. `<trade-history-table>`

Tabela de histórico de trades com scroll horizontal.

#### Props

| Prop | Tipo | Default | Descrição |
|------|------|---------|-----------|
| `trades` | JSON | `[]` | Array de trades |
| `loading` | boolean | `false` | Loading |

#### Estrutura do Trade

```json
{
  "asset": "BTC",
  "direction": "LONG",
  "entry": "75000",
  "exit": "78000",
  "pnl": "+1200",
  "pnl_pct": "+1.6",
  "exit_reason": "TP",
  "time": "2026-04-25 14:30"
}
```

#### Exemplos

```html
<trade-history-table
  trades='[{"asset":"BTC","direction":"LONG","entry":"75000","exit":"78000","pnl":"+1200","pnl_pct":"+1.6","exit_reason":"TP","time":"2026-04-25 14:30"}]'>
</trade-history-table>
```

#### Responsividade

- Desktop: 8 colunas visíveis
- Mobile (`<640px`): 6 colunas (esconde `reason` e `time`)
- Scroll horizontal com `tabindex="0"` para acessibilidade

---

## Estados de Loading

Todos os componentes suportam `loading="true"`:

```html
<!-- Shimmer effect -->
<price-ticker loading="true"></price-ticker>
<signal-card loading="true"></signal-card>
<position-panel loading="true"></position-panel>
```

**Design:**
- Background gradient animado (`loading-shimmer`)
- Sem conteúdo textual — apenas formas
- `aria-busy="true"` para screen readers

---

## Edge Cases Tratados

| Cenário | Comportamento |
|---------|--------------|
| Dados ausentes | Mostra "—" ou estado vazio |
| JSON inválido | Fallback para array vazio |
| Valores `NaN` | Tratados como "—" |
| Preço zero | Mostra "$0.00" (válido) |
| Mobile pequeno | Layout adapta, colunas escondem |
| Reduced motion | Animações desativadas via media query |
| Sem JavaScript | HTML fallback visível |

---

## Acessibilidade (A11y)

### WCAG 2.1 AA Compliance

| Critério | Implementação |
|----------|--------------|
| **1.3.1 Info and Relationships** | `role="region"`, `role="table"`, `role="status"` |
| **1.4.3 Contrast** | Todas as cores ≥ 4.5:1 |
| **1.4.10 Reflow** | Grid adapta a 320px |
| **2.1.1 Keyboard** | Focus visible em todos os elementos |
| **2.2.2 Pause, Stop, Hide** | `prefers-reduced-motion` respeitado |
| **4.1.2 Name, Role, Value** | `aria-label`, `aria-live`, `aria-busy` |

### Screen Reader Testing

```bash
# Testar com NVDA/VoiceOver:
1. Tab navegação entre cards
2. Leitura de status ao vivo (bot em execução)
3. Anúncio de novos sinais (aria-live)
4. Tabela navegável por células
```

---

## Responsividade

### Breakpoints

| Breakpoint | Layout |
|------------|--------|
| `> 1024px` | 3 colunas + wide cards |
| `768-1024px` | 2 colunas |
| `< 768px` | 1 coluna, header empilha |
| `< 480px` | Fonte reduzida, tabela scroll |

### Mobile-First CSS

```css
/* Base: Mobile */
.dashboard-grid {
  grid-template-columns: 1fr;
}

/* Tablet */
@media (min-width: 768px) {
  .dashboard-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

/* Desktop */
@media (min-width: 1024px) {
  .dashboard-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}
```

---

## Exemplo Completo: Dashboard

```html
<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hyperliquid Bot</title>
  <link rel="stylesheet" href="/static/css/components.css">
</head>
<body>
  <header class="app-header">
    <h1>🤖 Hyperliquid Bot</h1>
    <status-indicator id="bot-status" status="running"></status-indicator>
  </header>

  <div class="dashboard-grid">
    <!-- Preços -->
    <section class="card">
      <h2>📊 Preços</h2>
      <div class="ticker-grid">
        <price-ticker id="btc-price" asset="BTC"></price-ticker>
        <price-ticker id="eth-price" asset="ETH"></price-ticker>
      </div>
    </section>

    <!-- Sinal -->
    <section class="card">
      <h2>🎯 Sinal</h2>
      <signal-card id="current-signal"></signal-card>
    </section>

    <!-- Posição -->
    <section class="card">
      <h2>💼 Posição</h2>
      <position-panel id="open-position"></position-panel>
    </section>

    <!-- Performance -->
    <section class="card card-wide">
      <h2>📈 Performance</h2>
      <performance-chart id="perf-chart" initial="10000"></performance-chart>
    </section>

    <!-- Trades -->
    <section class="card card-wide">
      <h2>📋 Histórico</h2>
      <trade-history-table id="trade-table"></trade-history-table>
    </section>
  </div>

  <script type="module">
    import '/static/js/components.js';

    // Atualização em tempo real via polling
    async function updateDashboard() {
      const res = await fetch('/api/status');
      const data = await res.json();

      // Atualizar componentes via props
      document.getElementById('btc-price').setAttribute('price', data.prices.BTC);
      document.getElementById('btc-price').setAttribute('change', data.changes.BTC);

      document.getElementById('current-signal').setAttribute('direction', data.signal.direction);
      document.getElementById('current-signal').setAttribute('confidence', data.signal.confidence);

      document.getElementById('open-position').setAttribute('pnl', data.position.pnl);
      document.getElementById('open-position').setAttribute('current', data.position.current_price);
    }

    setInterval(updateDashboard, 5000);
  </script>
</body>
</html>
```

---

## Integração com Flask

```python
from flask import Flask, render_template, jsonify

app = Flask(__name__)

@app.route('/')
def dashboard():
    """Serve o dashboard com Web Components."""
    return render_template('dashboard_v3.html')

@app.route('/api/status')
def api_status():
    """Dados em tempo real para os componentes."""
    return jsonify({
        'prices': {'BTC': 77320.50, 'ETH': 4231.10},
        'changes': {'BTC': 1.2, 'ETH': -0.5},
        'signal': {
            'direction': 'LONG',
            'confidence': 0.85,
            'entry': 77320,
            'stop': 75000,
            'target': 82000
        },
        'position': {
            'direction': 'LONG',
            'pnl': 820,
            'current_price': 77320
        }
    })
```

---

## Performance

| Métrica | Valor |
|---------|-------|
| Tamanho JS | ~29 KB (minificado ~8 KB) |
| Tamanho CSS | ~4 KB |
| Tempo de parse | < 50ms |
| Shadow DOMs | 6 (um por componente) |
| Repaints | Mínimos ( Shadow DOM isolado ) |

---

## Roadmap

- [ ] **Tooltips** — Info em hover para labels técnicos
- [ ] **Toast Notifications** — Novos sinais (non-blocking)
- [ ] **Dark/Light Toggle** — Tema claro (accessibility)
- [ ] **Export PNG** — Gráfico para download
- [ ] **WebSocket** — Atualização real-time sem polling

---

*Documentação v1.0 — Hyperliquid Bot UI Components*
