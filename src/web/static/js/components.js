/**
 * Web Components — Dashboard Hyperliquid Bot v3
 * Componentes reutilizáveis, acessíveis, production-ready
 */

// ─── Utilitários ─────────────────────────────────────────

/**
 * Formata número como moeda USD
 * @param {number} value
 * @returns {string}
 */
function formatUSD(value) {
  if (value === undefined || value === null || isNaN(value)) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(value);
}

/**
 * Formata percentagem
 * @param {number} value
 * @returns {string}
 */
function formatPct(value) {
  if (value === undefined || value === null || isNaN(value)) return '—';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

/**
 * Formata timestamp relativo
 * @param {string} isoDate
 * @returns {string}
 */
function timeAgo(isoDate) {
  if (!isoDate) return '—';
  const date = new Date(isoDate);
  const now = new Date();
  const diff = Math.floor((now - date) / 1000);
  
  if (diff < 60) return 'agora';
  if (diff < 3600) return `${Math.floor(diff / 60)}m atrás`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h atrás`;
  return `${Math.floor(diff / 86400)}d atrás`;
}

/**
 * Cria loading shimmer element
 * @param {string} width
 * @param {string} height
 * @returns {HTMLElement}
 */
function createShimmer(width = '100%', height = '1em') {
  const el = document.createElement('div');
  el.className = 'loading-shimmer';
  el.style.width = width;
  el.style.height = height;
  el.setAttribute('aria-hidden', 'true');
  return el;
}

// ─── Estilos Partilhados (injetados em cada shadow DOM) ──

const SHARED_STYLES = `
  :host {
    display: block;
    font-family: var(--font-sans, system-ui);
    color: var(--color-text, #e5e7eb);
  }
  
  .loading-shimmer {
    background: linear-gradient(90deg, #111827 0%, #1a2332 50%, #111827 100%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 4px;
  }
  
  @keyframes shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
  }
  
  .error-state {
    padding: 1rem;
    border: 1px solid #ef4444;
    border-radius: 8px;
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
    text-align: center;
  }
  
  .empty-state {
    padding: 1rem;
    border: 1px dashed #6b7280;
    border-radius: 8px;
    color: #6b7280;
    text-align: center;
    font-style: italic;
  }
  
  /* Responsividade base */
  @media (max-width: 768px) {
    :host { font-size: 0.875rem; }
  }
`;

// ─── Componente: StatusIndicator ─────────────────────────

class StatusIndicator extends HTMLElement {
  static get observedAttributes() {
    return ['status', 'message'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open', delegatesFocus: true });
    this._status = 'loading';
    this._message = '';
  }
  
  connectedCallback() {
    this.render();
  }
  
  attributeChangedCallback(name, oldVal, newVal) {
    if (oldVal === newVal) return;
    if (name === 'status') this._status = newVal || 'loading';
    if (name === 'message') this._message = newVal || '';
    this.render();
  }
  
  render() {
    const statusConfig = {
      running: { color: '#10b981', label: 'Em execução', icon: '●' },
      stopped: { color: '#ef4444', label: 'Parado', icon: '○' },
      warning: { color: '#f59e0b', label: 'Atenção', icon: '◐' },
      loading: { color: '#3b82f6', label: 'A carregar...', icon: '◌' }
    };
    
    const config = statusConfig[this._status] || statusConfig.loading;
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .status {
          display: inline-flex;
          align-items: center;
          gap: 0.5rem;
          padding: 0.5rem 1rem;
          border-radius: 9999px;
          background: ${config.color}15;
          border: 1px solid ${config.color}40;
          font-family: var(--font-mono, monospace);
          font-size: 0.875rem;
        }
        .dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background: ${config.color};
          ${this._status === 'running' ? 'animation: pulse 2s infinite;' : ''}
          ${this._status === 'loading' ? 'animation: pulse 1s infinite;' : ''}
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      </style>
      <div class="status" role="status" aria-live="polite" aria-label="Estado do bot: ${config.label}">
        <span class="dot" aria-hidden="true"></span>
        <span>${config.icon} ${config.label}</span>
        ${this._message ? `<span style="color:#6b7280;margin-left:0.5rem;">— ${this._message}</span>` : ''}
      </div>
    `;
  }
}

// ─── Componente: PriceTicker ─────────────────────────────

class PriceTicker extends HTMLElement {
  static get observedAttributes() {
    return ['asset', 'price', 'change', 'loading'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._asset = 'BTC';
    this._price = 0;
    this._change = 0;
    this._loading = false;
  }
  
  connectedCallback() {
    this.render();
  }
  
  attributeChangedCallback(name, oldVal, newVal) {
    if (oldVal === newVal) return;
    switch(name) {
      case 'asset': this._asset = newVal || 'BTC'; break;
      case 'price': this._price = parseFloat(newVal) || 0; break;
      case 'change': this._change = parseFloat(newVal) || 0; break;
      case 'loading': this._loading = newVal === 'true'; break;
    }
    this.render();
  }
  
  render() {
    if (this._loading) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div style="padding:1rem;" aria-busy="true">
          ${createShimmer('60%', '1.5em').outerHTML}
          ${createShimmer('40%', '1em').outerHTML}
        </div>
      `;
      return;
    }
    
    const isPositive = this._change >= 0;
    const changeColor = isPositive ? '#10b981' : '#ef4444';
    const changeIcon = isPositive ? '▲' : '▼';
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .ticker {
          padding: 1rem;
          border: 1px solid #1f3a5f;
          border-radius: 8px;
          background: #111827;
          transition: border-color 0.15s;
        }
        .ticker:hover { border-color: #3b82f6; }
        .asset {
          font-family: var(--font-mono, monospace);
          font-size: 0.75rem;
          color: #6b7280;
          text-transform: uppercase;
          letter-spacing: 0.1em;
        }
        .price {
          font-family: var(--font-mono, monospace);
          font-size: 1.5rem;
          font-weight: 700;
          color: #f3f4f6;
          margin: 0.25rem 0;
        }
        .change {
          font-family: var(--font-mono, monospace);
          font-size: 0.875rem;
          color: ${changeColor};
        }
      </style>
      <div class="ticker" role="region" aria-label="Preço ${this._asset}">
        <div class="asset">${this._asset}/USD</div>
        <div class="price">${formatUSD(this._price)}</div>
        <div class="change" aria-label="Variação ${formatPct(this._change)}">
          ${changeIcon} ${formatPct(this._change)}
        </div>
      </div>
    `;
  }
}

// ─── Componente: SignalCard ──────────────────────────────

class SignalCard extends HTMLElement {
  static get observedAttributes() {
    return ['asset', 'direction', 'confidence', 'entry', 'stop', 'target', 'reason', 'timestamp', 'loading'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._data = {};
    this._loading = false;
  }
  
  connectedCallback() {
    this._readAttributes();
    this.render();
  }
  
  attributeChangedCallback() {
    this._readAttributes();
    this.render();
  }
  
  _readAttributes() {
    this._data = {
      asset: this.getAttribute('asset') || 'BTC',
      direction: (this.getAttribute('direction') || 'HOLD').toUpperCase(),
      confidence: parseFloat(this.getAttribute('confidence')) || 0,
      entry: parseFloat(this.getAttribute('entry')) || 0,
      stop: parseFloat(this.getAttribute('stop')) || 0,
      target: parseFloat(this.getAttribute('target')) || 0,
      reason: this.getAttribute('reason') || '',
      timestamp: this.getAttribute('timestamp') || ''
    };
    this._loading = this.getAttribute('loading') === 'true';
  }
  
  render() {
    if (this._loading) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div style="padding:1rem;" aria-busy="true">
          ${createShimmer('40%', '1.5em').outerHTML}
          ${createShimmer('80%', '1em').outerHTML}
          ${createShimmer('60%', '1em').outerHTML}
        </div>
      `;
      return;
    }
    
    const d = this._data;
    const isLong = d.direction === 'LONG';
    const isShort = d.direction === 'SHORT';
    const isHold = !isLong && !isShort;
    
    const directionColor = isLong ? '#10b981' : isShort ? '#ef4444' : '#6b7280';
    const directionBg = isLong ? 'rgba(16,185,129,0.1)' : isShort ? 'rgba(239,68,68,0.1)' : 'transparent';
    const directionBorder = isLong ? 'rgba(16,185,129,0.3)' : isShort ? 'rgba(239,68,68,0.3)' : '#1f3a5f';
    
    if (isHold) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div class="empty-state" role="status">
          ⏸️ Sem sinal ativo — aguardando oportunidade
        </div>
      `;
      return;
    }
    
    const rr = d.stop > 0 ? Math.abs((d.target - d.entry) / (d.entry - d.stop)).toFixed(2) : '—';
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .signal {
          padding: 1rem;
          border: 2px solid ${directionBorder};
          border-radius: 8px;
          background: ${directionBg};
          transition: transform 0.15s, box-shadow 0.15s;
        }
        .signal:hover {
          transform: translateY(-2px);
          box-shadow: 0 4px 20px ${directionColor}20;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 0.75rem;
        }
        .badge {
          display: inline-flex;
          align-items: center;
          gap: 0.25rem;
          padding: 0.25rem 0.75rem;
          border-radius: 4px;
          font-family: var(--font-mono, monospace);
          font-size: 0.75rem;
          font-weight: 700;
          text-transform: uppercase;
          background: ${directionColor}20;
          color: ${directionColor};
          border: 1px solid ${directionColor}40;
        }
        .confidence {
          font-family: var(--font-mono, monospace);
          font-size: 0.875rem;
          color: #6b7280;
        }
        .levels {
          display: grid;
          grid-template-columns: repeat(3, 1fr);
          gap: 0.75rem;
          margin: 0.75rem 0;
        }
        .level {
          text-align: center;
        }
        .level-label {
          font-size: 0.625rem;
          text-transform: uppercase;
          color: #6b7280;
          letter-spacing: 0.05em;
        }
        .level-value {
          font-family: var(--font-mono, monospace);
          font-size: 0.875rem;
          font-weight: 600;
          color: #f3f4f6;
        }
        .reason {
          font-size: 0.875rem;
          color: #9ca3af;
          margin-top: 0.5rem;
          padding-top: 0.5rem;
          border-top: 1px solid #1f3a5f;
        }
        .footer {
          display: flex;
          justify-content: space-between;
          margin-top: 0.5rem;
          font-size: 0.75rem;
          color: #6b7280;
        }
        @media (max-width: 480px) {
          .levels { grid-template-columns: 1fr; }
        }
      </style>
      <div class="signal" role="region" aria-label="Sinal ${d.direction} ${d.asset}">
        <div class="header">
          <span class="badge" aria-label="Direção ${d.direction}">
            ${isLong ? '🟢' : '🔴'} ${d.direction}
          </span>
          <span class="confidence">${Math.round(d.confidence * 100)}% confiança</span>
        </div>
        
        <div class="levels">
          <div class="level">
            <div class="level-label">Entrada</div>
            <div class="level-value">${formatUSD(d.entry)}</div>
          </div>
          <div class="level">
            <div class="level-label">Stop Loss</div>
            <div class="level-value" style="color:#ef4444">${formatUSD(d.stop)}</div>
          </div>
          <div class="level">
            <div class="level-label">Take Profit</div>
            <div class="level-value" style="color:#10b981">${formatUSD(d.target)}</div>
          </div>
        </div>
        
        ${d.reason ? `<div class="reason">💡 ${d.reason}</div>` : ''}
        
        <div class="footer">
          <span>R:R ${rr}</span>
          <span>${timeAgo(d.timestamp)}</span>
        </div>
      </div>
    `;
  }
}

// ─── Componente: PositionPanel ───────────────────────────

class PositionPanel extends HTMLElement {
  static get observedAttributes() {
    return ['asset', 'direction', 'entry', 'current', 'size', 'leverage', 'pnl', 'pnl-pct', 'loading'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._data = {};
    this._loading = false;
  }
  
  connectedCallback() {
    this._readAttributes();
    this.render();
  }
  
  attributeChangedCallback() {
    this._readAttributes();
    this.render();
  }
  
  _readAttributes() {
    this._data = {
      asset: this.getAttribute('asset') || 'BTC',
      direction: (this.getAttribute('direction') || 'FLAT').toUpperCase(),
      entry: parseFloat(this.getAttribute('entry')) || 0,
      current: parseFloat(this.getAttribute('current')) || 0,
      size: parseFloat(this.getAttribute('size')) || 0,
      leverage: parseFloat(this.getAttribute('leverage')) || 1,
      pnl: parseFloat(this.getAttribute('pnl')) || 0,
      pnlPct: parseFloat(this.getAttribute('pnl-pct')) || 0
    };
    this._loading = this.getAttribute('loading') === 'true';
  }
  
  render() {
    if (this._loading) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div style="padding:1rem;" aria-busy="true">
          ${createShimmer('50%', '2em').outerHTML}
          ${createShimmer('100%', '1em').outerHTML}
          ${createShimmer('100%', '1em').outerHTML}
        </div>
      `;
      return;
    }
    
    const d = this._data;
    const isFlat = d.direction === 'FLAT' || d.size === 0;
    
    if (isFlat) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div class="empty-state" role="status">
          📭 Sem posição aberta — aguardando sinal
        </div>
      `;
      return;
    }
    
    const isLong = d.direction === 'LONG';
    const pnlColor = d.pnl >= 0 ? '#10b981' : '#ef4444';
    const pnlBg = d.pnl >= 0 ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)';
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .position {
          padding: 1rem;
          border: 1px solid #1f3a5f;
          border-radius: 8px;
          background: #111827;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 1rem;
        }
        .asset {
          font-family: var(--font-mono, monospace);
          font-size: 1.25rem;
          font-weight: 700;
          color: #f3f4f6;
        }
        .badge {
          padding: 0.25rem 0.75rem;
          border-radius: 4px;
          font-size: 0.75rem;
          font-weight: 700;
          text-transform: uppercase;
          background: ${isLong ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.2)'};
          color: ${isLong ? '#10b981' : '#ef4444'};
          border: 1px solid ${isLong ? '#10b98140' : '#ef444440'};
        }
        .pnl-box {
          text-align: center;
          padding: 1rem;
          border-radius: 8px;
          background: ${pnlBg};
          border: 1px solid ${pnlColor}40;
          margin-bottom: 1rem;
        }
        .pnl-value {
          font-family: var(--font-mono, monospace);
          font-size: 2rem;
          font-weight: 700;
          color: ${pnlColor};
        }
        .pnl-label {
          font-size: 0.875rem;
          color: #6b7280;
          margin-top: 0.25rem;
        }
        .details {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 0.75rem;
        }
        .detail {
          display: flex;
          justify-content: space-between;
          padding: 0.5rem 0;
          border-bottom: 1px solid #1f3a5f;
        }
        .detail-label {
          font-size: 0.75rem;
          text-transform: uppercase;
          color: #6b7280;
          letter-spacing: 0.05em;
        }
        .detail-value {
          font-family: var(--font-mono, monospace);
          font-size: 0.875rem;
          color: #f3f4f6;
        }
        @media (max-width: 480px) {
          .details { grid-template-columns: 1fr; }
        }
      </style>
      <div class="position" role="region" aria-label="Posição ${d.direction} ${d.asset}">
        <div class="header">
          <span class="asset">${d.asset}</span>
          <span class="badge">${d.direction}</span>
        </div>
        
        <div class="pnl-box" role="status" aria-live="polite">
          <div class="pnl-value">${d.pnl >= 0 ? '+' : ''}${formatUSD(d.pnl)}</div>
          <div class="pnl-label">${formatPct(d.pnlPct)}</div>
        </div>
        
        <div class="details">
          <div class="detail">
            <span class="detail-label">Entrada</span>
            <span class="detail-value">${formatUSD(d.entry)}</span>
          </div>
          <div class="detail">
            <span class="detail-label">Atual</span>
            <span class="detail-value">${formatUSD(d.current)}</span>
          </div>
          <div class="detail">
            <span class="detail-label">Tamanho</span>
            <span class="detail-value">${formatUSD(d.size)}</span>
          </div>
          <div class="detail">
            <span class="detail-label">Alavancagem</span>
            <span class="detail-value">${d.leverage}x</span>
          </div>
        </div>
      </div>
    `;
  }
}

// ─── Componente: PerformanceChart ──────────────────────────

class PerformanceChart extends HTMLElement {
  static get observedAttributes() {
    return ['data', 'initial', 'loading'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._data = [];
    this._initial = 10000;
    this._loading = false;
  }
  
  connectedCallback() {
    this._readAttributes();
    this.render();
  }
  
  attributeChangedCallback() {
    this._readAttributes();
    this.render();
  }
  
  _readAttributes() {
    try {
      this._data = JSON.parse(this.getAttribute('data') || '[]');
    } catch {
      this._data = [];
    }
    this._initial = parseFloat(this.getAttribute('initial')) || 10000;
    this._loading = this.getAttribute('loading') === 'true';
  }
  
  render() {
    if (this._loading) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div style="padding:1rem;" aria-busy="true">
          ${createShimmer('100%', '200px').outerHTML}
        </div>
      `;
      return;
    }
    
    if (!this._data.length) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div class="empty-state" role="status">
          📊 Sem dados de performance — aguardando trades
        </div>
      `;
      return;
    }
    
    const values = this._data.map(d => d.value || d.y || 0);
    const min = Math.min(...values, this._initial);
    const max = Math.max(...values, this._initial);
    const range = max - min || 1;
    
    const width = 800;
    const height = 200;
    const padding = 40;
    
    const points = values.map((v, i) => {
      const x = padding + (i / (values.length - 1)) * (width - 2 * padding);
      const y = height - padding - ((v - min) / range) * (height - 2 * padding);
      return `${x},${y}`;
    }).join(' ');
    
    const last = values[values.length - 1];
    const totalReturn = ((last - this._initial) / this._initial * 100).toFixed(2);
    const returnColor = last >= this._initial ? '#10b981' : '#ef4444';
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .chart-container {
          position: relative;
        }
        .stats {
          display: flex;
          gap: 1.5rem;
          margin-bottom: 1rem;
          flex-wrap: wrap;
        }
        .stat {
          display: flex;
          flex-direction: column;
        }
        .stat-label {
          font-size: 0.625rem;
          text-transform: uppercase;
          color: #6b7280;
          letter-spacing: 0.05em;
        }
        .stat-value {
          font-family: var(--font-mono, monospace);
          font-size: 1.125rem;
          font-weight: 700;
          color: #f3f4f6;
        }
        svg {
          width: 100%;
          height: auto;
          max-height: 250px;
        }
        .grid-line {
          stroke: #1f3a5f;
          stroke-width: 1;
          stroke-dasharray: 4 4;
        }
        .data-line {
          fill: none;
          stroke: #3b82f6;
          stroke-width: 2;
          stroke-linecap: round;
          stroke-linejoin: round;
        }
        .data-area {
          fill: url(#gradient);
          opacity: 0.3;
        }
        .data-point {
          fill: #3b82f6;
          r: 4;
        }
        .data-point:last-child {
          fill: ${returnColor};
          r: 6;
        }
        .axis-label {
          font-family: var(--font-mono, monospace);
          font-size: 10px;
          fill: #6b7280;
        }
      </style>
      <div class="chart-container" role="img" aria-label="Gráfico de performance: retorno ${totalReturn}%">
        <div class="stats">
          <div class="stat">
            <span class="stat-label">Capital Inicial</span>
            <span class="stat-value">${formatUSD(this._initial)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Capital Atual</span>
            <span class="stat-value">${formatUSD(last)}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Retorno Total</span>
            <span class="stat-value" style="color:${returnColor}">${totalReturn >= 0 ? '+' : ''}${totalReturn}%</span>
          </div>
          <div class="stat">
            <span class="stat-label">Trades</span>
            <span class="stat-value">${this._data.length - 1}</span>
          </div>
        </div>
        
        <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="gradient" x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" style="stop-color:#3b82f6;stop-opacity:0.5" />
              <stop offset="100%" style="stop-color:#3b82f6;stop-opacity:0" />
            </linearGradient>
          </defs>
          
          <!-- Grid -->
          <line x1="${padding}" y1="${padding}" x2="${width-padding}" y2="${padding}" class="grid-line" />
          <line x1="${padding}" y1="${height/2}" x2="${width-padding}" y2="${height/2}" class="grid-line" />
          <line x1="${padding}" y1="${height-padding}" x2="${width-padding}" y2="${height-padding}" class="grid-line" />
          
          <!-- Area -->
          <polygon class="data-area" points="${points} ${width-padding},${height-padding} ${padding},${height-padding}" />
          
          <!-- Line -->
          <polyline class="data-line" points="${points}" />
          
          <!-- Points -->
          ${values.map((v, i) => {
            const x = padding + (i / (values.length - 1)) * (width - 2 * padding);
            const y = height - padding - ((v - min) / range) * (height - 2 * padding);
            return `<circle class="data-point" cx="${x}" cy="${y}" />`;
          }).join('')}
          
          <!-- Labels -->
          <text x="${padding}" y="${height - 10}" class="axis-label">${this._data[0]?.date || 'Start'}</text>
          <text x="${width - padding - 40}" y="${height - 10}" class="axis-label">${this._data[this._data.length - 1]?.date || 'Now'}</text>
        </svg>
      </div>
    `;
  }
}

// ─── Componente: TradeHistoryTable ─────────────────────────

class TradeHistoryTable extends HTMLElement {
  static get observedAttributes() {
    return ['trades', 'loading'];
  }
  
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._trades = [];
    this._loading = false;
  }
  
  connectedCallback() {
    this._readAttributes();
    this.render();
  }
  
  attributeChangedCallback() {
    this._readAttributes();
    this.render();
  }
  
  _readAttributes() {
    try {
      this._trades = JSON.parse(this.getAttribute('trades') || '[]');
    } catch {
      this._trades = [];
    }
    this._loading = this.getAttribute('loading') === 'true';
  }
  
  render() {
    if (this._loading) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div style="padding:1rem;" aria-busy="true">
          ${createShimmer('100%', '1em').outerHTML}
          ${createShimmer('100%', '1em').outerHTML}
          ${createShimmer('100%', '1em').outerHTML}
        </div>
      `;
      return;
    }
    
    if (!this._trades.length) {
      this.shadowRoot.innerHTML = `
        <style>${SHARED_STYLES}</style>
        <div class="empty-state" role="status">
          📋 Sem trades registados — aguardando execução
        </div>
      `;
      return;
    }
    
    const rows = this._trades.map(t => {
      const isWin = parseFloat(t.pnl) >= 0;
      const pnlColor = isWin ? '#10b981' : '#ef4444';
      return `
        <tr>
          <td class="cell-asset">${t.asset || '—'}</td>
          <td class="cell-direction ${t.direction?.toLowerCase()}">${t.direction || '—'}</td>
          <td class="cell-price">${formatUSD(parseFloat(t.entry))}</td>
          <td class="cell-price">${t.exit ? formatUSD(parseFloat(t.exit)) : '—'}</td>
          <td class="cell-pnl" style="color:${pnlColor}">${formatUSD(parseFloat(t.pnl))}</td>
          <td class="cell-pnl" style="color:${pnlColor}">${formatPct(parseFloat(t.pnl_pct))}</td>
          <td class="cell-reason">${t.exit_reason || '—'}</td>
          <td class="cell-time">${t.time || '—'}</td>
        </tr>
      `;
    }).join('');
    
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_STYLES}
        .table-container {
          overflow-x: auto;
          -webkit-overflow-scrolling: touch;
        }
        table {
          width: 100%;
          border-collapse: collapse;
          font-size: 0.875rem;
        }
        th {
          text-align: left;
          padding: 0.75rem 0.5rem;
          font-size: 0.625rem;
          text-transform: uppercase;
          color: #6b7280;
          letter-spacing: 0.05em;
          border-bottom: 2px solid #1f3a5f;
          white-space: nowrap;
        }
        td {
          padding: 0.75rem 0.5rem;
          border-bottom: 1px solid #1f3a5f;
          font-family: var(--font-mono, monospace);
        }
        tr:hover td {
          background: rgba(59, 130, 246, 0.05);
        }
        .cell-asset { font-weight: 600; }
        .cell-direction { font-weight: 700; text-transform: uppercase; }
        .cell-direction.long { color: #10b981; }
        .cell-direction.short { color: #ef4444; }
        .cell-price { color: #9ca3af; }
        .cell-pnl { font-weight: 600; }
        .cell-reason { font-size: 0.75rem; color: #6b7280; }
        .cell-time { font-size: 0.75rem; color: #6b7280; white-space: nowrap; }
        
        @media (max-width: 640px) {
          th, td { padding: 0.5rem 0.25rem; font-size: 0.75rem; }
          .cell-reason, .cell-time { display: none; }
        }
      </style>
      <div class="table-container" role="region" aria-label="Histórico de trades" tabindex="0">
        <table role="table">
          <thead>
            <tr role="row">
              <th role="columnheader">Asset</th>
              <th role="columnheader">Dir</th>
              <th role="columnheader">Entrada</th>
              <th role="columnheader">Saída</th>
              <th role="columnheader">PnL $</th>
              <th role="columnheader">PnL %</th>
              <th role="columnheader">Motivo</th>
              <th role="columnheader">Data</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    `;
  }
}

// ─── Registro de Componentes ─────────────────────────────

const components = [
  ['status-indicator', StatusIndicator],
  ['price-ticker', PriceTicker],
  ['signal-card', SignalCard],
  ['position-panel', PositionPanel],
  ['performance-chart', PerformanceChart],
  ['trade-history-table', TradeHistoryTable]
];

components.forEach(([name, Constructor]) => {
  if (!customElements.get(name)) {
    customElements.define(name, Constructor);
  }
});

// ─── Export para uso em módulos ────────────────────────────

export {
  StatusIndicator,
  PriceTicker,
  SignalCard,
  PositionPanel,
  PerformanceChart,
  TradeHistoryTable,
  formatUSD,
  formatPct,
  timeAgo
};

console.log('✅ Web Components carregados:', components.map(c => c[0]).join(', '));
