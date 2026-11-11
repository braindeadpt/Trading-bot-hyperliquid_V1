# Prereg: ResidualMomentum forward shadow (paper, HL)
Registado: 2026-10-28 15:20 UTC, antes de qualquer dado forward. Base: notes/resmom-prereg-2026-10-27.md (PASS histórico).

Regra (congelada = Braço A do histórico)
• Universo: top 25 perps HL por volume médio notional 30d, ≥90 dias de histórico HL, sem stables. BTC entra no ranking, sai das pernas.
• Sinal: s = resid15 / vol30 (beta OLS 60d vs BTC, retornos log diários, close 00:00 UTC).
• Carteira: long quintil topo, short quintil fundo (floor(0.2·n)), pesos ∝ 1/vol30, gross 0,5/perna, beta-neutral vs BTC, gross total 1,0.
• Rebalance: segunda 00:00 UTC com dados até ao close de domingo; fill paper ao open da 1.ª vela 1m após 00:05 UTC.
• Custos: 6,5 bps/lado sobre turnover; funding HL horário real aplicado.
• Notional paper: 10 000 USD gross. Sem drawdown stop (stop 25% só descritivo). Sem capital real.
• Braço B (+low vol: média do rank percentil de A e de −vol30) calculado em paralelo, só descritivo.

Nulos e referências
• 200 carteiras de sinal aleatório no mesmo universo/pesos/custos, mesmas semanas forward.
• Referência descritiva: mesma regra em Binance USDT-M forward.

Leitura
• Única, a 26 rebalances fechados (primeiro 2026-11-02, leitura ~2027-05-03).
• PASS (todos): retorno líquido acumulado > 0; Sharpe anualizado > 0,5; Sharpe > p75 dos nulos; PnL ex-top-3 contribuintes > 0.
• KILL antecipado (só isto, verificado semanalmente): drawdown líquido ≤ −35%, ou erro de implementação que impeça reproduzir o sinal.
• Caso contrário: C para HL. Sem tuning de lookbacks, quantis, universo ou dia de rebalance.
• Ressalva: 26 semanas têm pouco poder estatístico; PASS = consistência, não prova.

## Amendment 1 (operacional, sem dados)

Primeiro rebalance passa de 2026-11-02 para 2026-11-09 por causa do
cutover Mac → bots-01. Regra, parâmetros e leitura (26 rebalances)
inalterados.
