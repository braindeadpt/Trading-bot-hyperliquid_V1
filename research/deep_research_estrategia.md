Análise Deep Research - Estratégia Hyperliquid v2.0
Data: 2026-04-25
Base de Dados: hyperliquid.db (87,305 candles, 266 paper trades)

═══════════════════════════════════════════════════════════════════════════════
1. EXECUTIVE SUMMARY — DIAGNÓSTICO CRÍTICO
═══════════════════════════════════════════════════════════════════════════════

🔴 PROBLEMA IDENTIFICADO: Estratégia com 8.3% Win Rate

Métricas Atuais:
• Total Trades:     266
• Wins:              22 (8.3%)
• Losses:           244 (91.7%)
• Direção:          100% LONG (0 SHORTS!)
• Razão saída:      244 STOP_LOSS vs 22 TRAILING_STOP
• Dias trading:      2 dias (concentrado demais)

VEREDICTO: Estratégia PERDE DINHEIRO. NÃO usar em live.

═══════════════════════════════════════════════════════════════════════════════
2. ANÁLISE DOS PADRÕES DE FALHA
═══════════════════════════════════════════════════════════════════════════════

2.1 Problema #1: Win Rate de 8.3%
─────────────────────────────────
Esperado para estratégia momentum: 40-55%
Observado: 8.3%

Causas prováveis:
• Stop Loss muito apertado (2%?) — mercado respira mais do que isso
• Entradas em topo local (FOMO entries)
• Volume spike threshold pode estar a gerar sinais tardios

2.2 Problema #2: 100% LONG, 0 SHORT
──────────────────────────────────────
Mercado não sobe sempre. Em 266 trades, deveria haver pelo menos 30-40% shorts.

Causas prováveis:
• Threshold de SHORT muito apertado (volume 3x vs 2x do LONG)
• Bearish count requer 3 candles vs 2 do bullish
• Estratégia biasada para o lado long

2.3 Problema #3: 244/266 = 91.7% fechados por STOP_LOSS
─────────────────────────────────────────────────────────
Isso significa que o stop está a ser atingido quase sempre ANTES do trailing.

Causas prováveis:
• Stop Loss de 2% é muito apertado para timeframe 15m
• Sem buffer para volatilidade intracandle
• Possível bug: trades repetidos no mesmo candle?

═══════════════════════════════════════════════════════════════════════════════
3. ANÁLISE TÉCNICA DOS PARÂMETROS
═══════════════════════════════════════════════════════════════════════════════

Configuração Atual (estimada):
• Volume Threshold LONG:  2.0x
• Volume Threshold SHORT: 3.0x (50% mais apertado!)
• Min Bullish Candles:    2
• Min Bearish Candles:    3
• Stop Loss:              2.0%
• Trailing Stop:          1.5%
• Max Funding:            0.01 (1%)
• Min Funding:            -0.01 (-1%)

Problemas:
1. SHORT muito mais difícil de entrar que LONG → bias long
2. Stop Loss 2% em 15m com leverage pode ser tight demais
3. Trailing activation 1.5% — se o stop é 2%, o trailing quase nunca ativa

═══════════════════════════════════════════════════════════════════════════════
4. COMPARAÇÃO COM ESTRATÉGIA DE REFERENCE (KIMI RESEARCH)
═══════════════════════════════════════════════════════════════════════════════

A estratégia que analisaste anteriormente (Volume Profile + Momentum):
• Usava confirmação em múltiplos timeframes
• Tinha filtro de regime de mercado
• Usava volume profile para evitar entradas em zonas de resistência

O teu bot atual:
• NÃO tem filtro de regime operacional (só loga)
• NÃO verifica se está a entrar em resistência/suporte
• NÃO ajusta parâmetros consoante volatilidade

═══════════════════════════════════════════════════════════════════════════════
5. RECOMENDAÇÕES — PLANO DE CORREÇÃO
═══════════════════════════════════════════════════════════════════════════════

5.1 FIXES IMEDIATOS (Semana 2)
──────────────────────────────

A) EQUILIBRAR LONG vs SHORT:
   • Volume threshold SHORT: 3.0x → 2.0x (igual ao LONG)
   • Min bearish candles: 3 → 2 (igual ao LONG)
   • Ativar short_enabled: true (confirmar que está ligado)

B) AJUSTAR STOP LOSS:
   • Stop Loss: 2.0% → 3.5% (para dar mais respiro)
   • Ou: usar ATR-based stop (2x ATR(14))

C) VERIFICAR BUG DE ENTRADAS REPETIDAS:
   • 230 trades num dia só? Isso é MUITO.
   • Possível bug: bot a entrar múltiplas vezes no mesmo candle
   • Adicionar cooldown de 1 candle entre trades

5.2 MELHORIAS ESTRATÉGICAS (Semana 3)
──────────────────────────────────────

A) FILTRO DE REGIME:
   • Não tradar quando volatilidade > 5% ( chop / news event )
   • Não tradar quando preço está > 5% acima/baixo da SMA200

B) VOLUME PROFILE INTEGRADO:
   • Evitar entradas LONG quando preço está na zona de POC+VAH
   • Evitar entradas SHORT quando preço está na zona de VAL-POC

C) CONFIRMATION LAG:
   • Em vez de entrar no candle do spike, esperar 1 candle de confirmação
   • Reduz FOMO entries em topos locais

5.3 MÉTRICAS ALVO
─────────────────
Antes de considerar "fixed":
• Win Rate: > 40%
• Profit Factor: > 1.3
• Long/Short ratio: 60/40 a 40/60 (não 100/0!)
• Avg Trade Duration: > 30 min (evitar whipsaws)

═══════════════════════════════════════════════════════════════════════════════
6. PRÓXIMOS PASSOS SUGERIDOS
═══════════════════════════════════════════════════════════════════════════════

1. Fazer git pull e CORRIGIR os parâmetros em config.yaml
2. Correr backtest com dados históricos (não paper trading)
3. Monitorizar por 3-5 dias com os novos parâmetros
4. Só depois: considerar LLM sentiment layer

═══════════════════════════════════════════════════════════════════════════════
7. NOTA IMPORTANTE
═══════════════════════════════════════════════════════════════════════════════

NÃO implementar LLM sentiment até a estratégia base estar lucrativa.
O LLM é um "boost" — não vai salvar uma estratégia que perde 92% do tempo.

"O que está bom mais vale não mexer" — mas neste caso, NÃO está bom.
Precisa de fix antes de avançar.

═══════════════════════════════════════════════════════════════════════════════
Gerado por: Braindead Trading Bot v2.0
Timestamp: 2026-04-25
