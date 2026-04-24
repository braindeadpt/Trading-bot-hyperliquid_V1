// ===== PYTHON BRIDGE =====
// Verifica se está a correr dentro da app desktop (pywebview)
const isDesktop = typeof window.pywebview !== 'undefined' && window.pywebview.api !== undefined;

console.log('🖥️ Modo:', isDesktop ? 'DESKTOP (Python backend)' : 'STANDALONE (browser)');

// Wrapper para chamar Python ou fallback para standalone
async function pyCall(method, ...args) {
    if (isDesktop && window.pywebview.api[method]) {
        try {
            const result = await window.pywebview.api[method](...args);
            return result;
        } catch (e) {
            console.error('Erro Python API:', e);
            return null;
        }
    }
    return null;
}

// ===== OVERRIDES DO DASHBOARD =====
// Quando em modo desktop, substituir funções para usar backend Python

if (isDesktop) {
    // Override startBot
    const originalStartBot = window.startBot;
    window.startBot = async function() {
        if (botRunning) { log('⚠️ Já está a correr', 'warn'); return; }

        console.log('🚀 A iniciar bot via Python backend...');
        const result = await pyCall('_js_start_bot');
        if (result && result.success) {
            botRunning = true;
            document.getElementById('btn-start').classList.add('active');
            document.getElementById('bot-dot').className = 'status-dot on';
            document.getElementById('bot-text').textContent = 'RUNNING';
            document.getElementById('bot-text').style.color = 'var(--accent)';
            document.getElementById('thread-monitor').textContent = 'ON';
            document.getElementById('monitor-status').textContent = 'ON';

            paperCapital = parseFloat(document.getElementById('capital').value) || 10000;
            equityHistory = [paperCapital];

            log('🚀 BOT INICIADO! 🐍 (Python backend)', 'signal');
            log('📡 Backend Python real a correr...', 'info');

            // Iniciar polling do estado
            startStatePolling();
        } else {
            log('❌ Falha a iniciar bot: ' + (result ? result.message : 'unknown'), 'error');
        }
    };

    // Override stopBot
    const originalStopBot = window.stopBot;
    window.stopBot = async function() {
        if (!botRunning) { log('⚠️ Já está parado', 'warn'); return; }

        console.log('🛑 A parar bot via Python backend...');
        const result = await pyCall('_js_stop_bot');
        if (result && result.success) {
            botRunning = false;
            document.getElementById('btn-start').classList.remove('active');
            document.getElementById('bot-dot').className = 'status-dot off';
            document.getElementById('bot-text').textContent = 'STOPPED';
            document.getElementById('bot-text').style.color = '';
            document.getElementById('pos-dot').className = 'status-dot off';
            document.getElementById('pos-text').textContent = 'FLAT';
            document.getElementById('thread-monitor').textContent = 'OFF';
            document.getElementById('monitor-status').textContent = 'OFF';

            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            if (currentPosition) closePosition('BOT_STOPPED');
            if (statePollInterval) { clearInterval(statePollInterval); statePollInterval = null; }
            log('⏹ BOT PARADO (Python backend)', 'warn');
        }
    };

    // Override fetchRealData
    const originalFetchRealData = window.fetchRealData;
    window.fetchRealData = async function() {
        if (!botRunning) return;

        const status = await pyCall('_js_get_status');
        if (!status) {
            // Fallback para API directa se Python falhar
            console.log('⚠️ Python backend não respondeu, fallback para API directa');
            return originalFetchRealData();
        }

        // Actualizar UI com dados do backend Python
        const price = status.price || 0;
        const markPrice = status.mark_price || price;
        const oraclePrice = status.oracle_price || price;

        if (price > 0) {
            document.getElementById('price').textContent = fmt$(price);
            document.getElementById('mark-price').textContent = fmt$(markPrice);
            document.getElementById('oracle-price').textContent = fmt$(oraclePrice);
            lastPrice = price;
        }

        document.getElementById('oi').textContent = status.oi > 0 ? fmtNum(status.oi, 4) : '--';
        document.getElementById('oi-usd').textContent = status.oi_usd > 0 ? fmt$(status.oi_usd) : '--';
        document.getElementById('funding').textContent = status.funding !== 0 ? fmtPct(status.funding * 100) : '--';
        document.getElementById('volume').textContent = status.volume > 0 ? fmt$(status.volume) : '--';

        // Status
        document.getElementById('conn-dot').className = 'status-dot on';
        document.getElementById('conn-text').textContent = 'ONLINE';
        document.getElementById('conn-text').style.color = 'var(--accent)';

        // Position
        if (status.position) {
            const p = status.position;
            const pnlPct = price > 0 ? ((price - p.entryPrice) / p.entryPrice * 100 * (p.direction === 'LONG' ? 1 : -1)) : 0;

            document.getElementById('pos-direction').textContent = p.direction;
            document.getElementById('pos-direction').className = 'value ' + (p.direction === 'LONG' ? 'up' : 'down');
            document.getElementById('pos-entry').textContent = fmt$(p.entryPrice);
            document.getElementById('pos-size').textContent = fmt$(p.size);
            document.getElementById('pos-pnl').textContent = (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%';
            document.getElementById('pos-pnl').className = 'value ' + (pnlPct >= 0 ? 'up' : 'down');
            document.getElementById('pos-dot').className = 'status-dot ' + (pnlPct >= 0 ? 'on' : 'warn');
            document.getElementById('pos-text').textContent = p.direction + ' ' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(1) + '%';
            document.getElementById('btn-close').disabled = false;

            // Actualizar posição global para consistência
            currentPosition = {
                direction: p.direction,
                entryPrice: p.entryPrice,
                size: p.size,
                stopLoss: p.stopLoss,
                trailingStop: p.trailingStop,
                openTime: p.openTime,
            };
        } else {
            document.getElementById('pos-direction').textContent = 'FLAT';
            document.getElementById('pos-direction').className = 'value';
            document.getElementById('pos-entry').textContent = '--';
            document.getElementById('pos-size').textContent = '--';
            document.getElementById('pos-pnl').textContent = '--';
            document.getElementById('pos-dot').className = 'status-dot off';
            document.getElementById('pos-text').textContent = 'FLAT';
            document.getElementById('btn-close').disabled = true;
            currentPosition = null;
        }

        // Equity
        const equity = status.equity || status.capital || paperCapital;
        document.getElementById('cap-current').textContent = fmt$(equity);
        const totalPnl = equity - paperCapital;
        document.getElementById('total-pnl').textContent = (totalPnl >= 0 ? '+' : '') + fmt$(totalPnl);
        document.getElementById('total-pnl').className = 'value ' + (totalPnl >= 0 ? 'up' : 'down');

        // Update count
        document.getElementById('update-count').textContent = status.update_count || 0;
        document.getElementById('last-update').textContent = now();
    };

    // Override fetch logs
    window.fetchLogs = async function() {
        const logs = await pyCall('_js_get_logs', 100);
        if (logs && logs.length > 0) {
            const container = document.getElementById('log-container');
            container.innerHTML = '';
            logs.forEach(l => {
                const line = document.createElement('div');
                line.className = 'log-line';
                const cls = l.level === 'warn' ? 'log-warn' : l.level === 'error' ? 'log-error' : 'log-info';
                line.innerHTML = `<span class="log-time">${l.time}</span> <span class="${cls}">${l.message}</span>`;
                container.appendChild(line);
            });
            container.scrollTop = container.scrollHeight;
        }
    };

    // Override force long/short
    window.forceLong = async function() {
        const result = await pyCall('_js_force_long');
        log(result.message, result.success ? 'signal' : 'error');
    };

    window.forceShort = async function() {
        const result = await pyCall('_js_force_short');
        log(result.message, result.success ? 'signal' : 'error');
    };

    window.emergencyClose = async function() {
        const result = await pyCall('_js_emergency_close');
        log(result.message, result.success ? 'warn' : 'error');
    };

    // Polling do estado
    let statePollInterval = null;
    window.startStatePolling = function() {
        if (statePollInterval) clearInterval(statePollInterval);
        statePollInterval = setInterval(async () => {
            if (botRunning) {
                await fetchRealData();
                await fetchLogs();
            }
        }, 3000);
    };
}
