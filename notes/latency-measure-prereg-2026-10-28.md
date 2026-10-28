# Prereg: medição passiva de latência trade.xyz vs Binance (sem trades)
Registado: 2026-10-28 15:20 UTC, antes de dados.

Pergunta: os movimentos da Binance que o trade.xyz (HIP-3 HL) só reflete ~450–550 ms depois, no fecho dos EUA, pagam o taker HL (~11 bps ida e volta) quando medidos a partir do bots-01?

Medição
• Janela: 19:45–20:15 America/New_York em dias úteis, mais 30 min de controlo às 15:00 NY.
• Feeds: Binance perp (bookTicker/aggTrade) e trade.xyz via WS HL, timestamp local no bots-01 (inclui a nossa latência).
• Evento: mid Binance move ≥15 bps em ≤1 s (símbolos com par nos dois venues).
• Por evento: atraso até o mid trade.xyz mover metade; ganho hipotético = movimento trade.xyz entre t0+500 ms (entrada taker no ask/bid) e t0+5 s, menos 11 bps.

Leitura
• Com ≥10 sessões E ≥100 eventos, máximo 4 semanas.
• "Vale a pena estudar" (todos): ganho médio líquido > 0 com IC95% por sessão (cluster) acima de 0; >50% das sessões positivas; não explicado por 1–2 eventos.
• Caso contrário: latência fechada (C) definitivamente.
• Só medição; nenhuma ordem, nem paper.
