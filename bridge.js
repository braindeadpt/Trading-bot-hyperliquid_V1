// ===== BRIDGE UNIVERSAL =====
// Funciona em 3 modos:
// 1. STANDALONE (browser): usa localStorage + simulação
// 2. DESKTOP (pywebview): usa window.pywebview.api
// 3. FLASK (localhost): usa fetch('/api/...')

const isDesktop = typeof window.pywebview !== 'undefined' && window.pywebview.api !== undefined;
const isFlask = window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost';
const isStandalone = !isDesktop && !isFlask;

console.log('🖥️ Modo:', isDesktop ? 'DESKTOP (pywebview)' : isFlask ? 'FLASK (localhost)' : 'STANDALONE (browser)');

// Wrapper universal para chamar backend
async function backendCall(method, ...args) {
    if (isDesktop && window.pywebview.api[method]) {
        try {
            return await window.pywebview.api[method](...args);
        } catch (e) {
            console.error('Erro Python API:', e);
            return null;
        }
    }
    
    if (isFlask) {
        try {
            let url = '/api/' + method.replace('_js_', '').replace(/_/g, '');
            let options = { method: 'GET' };
            
            // Mapear métodos para endpoints
            const endpointMap = {
                '_js_start_bot': { url: '/api/start', method: 'POST' },
                '_js_stop_bot': { url: '/api/stop', method: 'POST' },
                '_js_get_status': { url: '/api/status', method: 'GET' },
                '_js_get_logs': { url: '/api/logs', method: 'GET' },
                '_js_get_trades': { url: '/api/trades', method: 'GET' },
                '_js_force_long': { url: '/api/force/long', method: 'POST' },
                '_js_force_short': { url: '/api/force/short', method: 'POST' },
                '_js_emergency_close': { url: '/api/emergency', method: 'POST' },
                '_js_save_config': { url: '/api/config', method: 'POST' },
                '_js_load_config': { url: '/api/config', method: 'GET' },
            };
            
            if (endpointMap[method]) {
                url = endpointMap[method].url;
                options.method = endpointMap[method].method;
            }
            
            if (args.length > 0 && options.method === 'POST') {
                options.headers = { 'Content-Type': 'application/json' };
                options.body = JSON.stringify(args[0]);
            }
            
            const resp = await fetch(url, options);
            if (!resp.ok) return null;
            return await resp.json();
        } catch (e) {
            console.error('Erro Flask API:', e);
            return null;
        }
    }
    
    return null;
}

// ===== OVERRIDES DO DASHBOARD =====
// Quando em modo desktop ou Flask, substituir funções para usar backend Python

if (isDesktop || isFlask) {
    
    // Override startBot
    const originalStartBot = window.startBot;
    window.startBot = async function() {
        if (botRunning) { log('⚠️ Já está a correr', 'warn'); return; }

        console.log('🚀 A iniciar bot via backend Python...');
        const result = await backendCall('_js_start_bot');
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

            startStatePolling();
        } else {
            log('❌ Falha a iniciar bot: ' + (result ? result.message : 'unknown'), 'error');
        }
    };

    // Override stopBot
    const originalStopBot = window.stopBot;
    window.stopBot = async function() {
        if (!botRunning) { log('⚠️ Já está parado', 'warn'); return; }

        console.log('🛑 A parar bot via backend Python...');
        const result = await backendCall('_js_stop_bot');
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

        const status = await backendCall('_js_get_status');
        if (!status) {
            console.log('⚠️ Backend não respondeu, fallback para API directa');
            return originalFetchRealData();
        }

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

        document.getElementById('conn-dot').className = 'status-dot on';
        document.getElementById('conn-text').textContent = 'ONLINE';
        document.getElementById('conn-text').style.color = 'var(--accent)';

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

        const equity = status.equity || status.capital || paperCapital;
        document.getElementById('cap-current').textContent = fmt$(equity);
        const totalPnl = equity - paperCapital;
        document.getElementById('total-pnl').textContent = (totalPnl >= 0 ? '+' : '') + fmt$(totalPnl);
        document.getElementById('total-pnl').className = 'value ' + (totalPnl >= 0 ? 'up' : 'down');

        document.getElementById('update-count').textContent = status.update_count || 0;
        document.getElementById('last-update').textContent = now();
    };

    // Override fetch logs
    window.fetchLogs = async function() {
        const logs = await backendCall('_js_get_logs');
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
        const result = await backendCall('_js_force_long');
        log(result.message, result.success ? 'signal' : 'error');
    };

    window.forceShort = async function() {
        const result = await backendCall('_js_force_short');
        log(result.message, result.success ? 'signal' : 'error');
    };

    window.emergencyClose = async function() {
        const result = await backendCall('_js_emergency_close');
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

    // Quando parar o bot, parar também o polling
    const originalStopBot2 = window.stopBot;
    window.stopBot = async function() {
        if (statePollInterval) {
            clearInterval(statePollInterval);
            statePollInterval = null;
        }
        return originalStopBot2();
    };
}
