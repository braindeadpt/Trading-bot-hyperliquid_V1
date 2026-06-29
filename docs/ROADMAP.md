# Roadmap — Hyperliquid Premium Bot

Prioridades de evolução de dados e infraestrutura.  
**Live trading** usa Hyperliquid WS em tempo real; o backfill Binance serve apenas histórico na SQLite.

---

## P0 — Crítico (funcionamento live)

| Item | Estado |
|------|--------|
| HL WebSocket (price, trades, ctx, L2) | Feito |
| CandleBuilder live (velas a partir de ticks HL) | Feito |
| Engine + risk + execution paper/testnet/mainnet | Feito |
| Backfill Binance spot (BTC/ETH/SOL) para warm-up / DB | Feito |

Sem estes itens o bot não opera de forma fiável.

---

## P1 — Importante (qualidade operacional)

| Item | Estado |
|------|--------|
| Funding + OI backfill (Binance USD-M) | Feito |
| `buy_volume` / `sell_volume` no backfill de candles (CVD) | Feito |
| Binance perp + liquidation proxy replay (backtest) | Feito |
| Persistência live de funding/OI/liquidações na DB | Feito |
| Backfill Binance **futures** quando não há spot (ex.: HYPE) | Feito |
| Dashboard chart (lightweight-charts + `/api/candles`) | Feito |

Melhoram warm-up após restart, backtest e observabilidade. **Não bloqueiam** live se a DB tiver dados mínimos ou o bot tiver tempo para aquecer indicadores.

---

## P2 — Desejável (paridade histórica / backtest)

### Backfill de velas nativo Hyperliquid (`candleSnapshot`)

**Problema:** O histórico na DB vem da Binance (spot ou USD-M futures). Em live, preço, execução e velas são **HL perp**. Para BTC/ETH/SOL a diferença é pequena; para alts só em perp (ex. **HYPE**) o proxy Binance é aceitável em paper mas **menos fiável em backtest**.

**Proposta:**

- Novo módulo `src/data/hl_candle_backfill.py` — REST HL `candleSnapshot` (ou equivalente) por símbolo/timeframe
- Script `scripts/backfill_hl_candles.py`
- Preferência: usar velas HL na DB quando existirem; Binance como fallback
- Benefícios: backtest alinhado ao venue de execução, warm-up com preços HL, chart com histórico HL

**Importância:**

| Uso | Necessidade |
|-----|-------------|
| Paper / mainnet live | Baixa — CandleBuilder HL cobre em minutos/horas |
| Dashboard (só visual) | Baixa — Binance futures chega |
| Backtest sério (especialmente HYPE) | Média–alta |
| Mainnet com capital real + backtest como referência | Média–alta |

**Esforço estimado:** ~1 módulo + script CLI + testes de integração (médio).

### Feed spot Hyperliquid (HIP-1) — opcional, mesmo P2

Só relevante se activarmos estratégias **basis HL spot vs HL perp**. Hoje o bot usa Binance spot para `SpotPerpCarry` / LeadLag basis mode, não spot HL.

---

## P3 — Futuro / nice-to-have

- Walk-forward automático pós-backfill HL
- Unificação de fonte única “venue of record” na DB (metadado `candle_source: hl | binance_spot | binance_perp`)
- Coinalyze / OI histórico para símbolos novos sem mapeamento manual

---

*Última actualização: 2026-06-26*
