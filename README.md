# Hyperliquid Trading Bot

Bot de paper trading para Hyperliquid com framework de validação de estratégias.
O objectivo é encontrar edge estatístico rigoroso antes de qualquer exposição real.

---

## Estado actual

| Componente | Estado |
|---|---|
| Bot live (paper trading) | Funcional — `src/main.py` |
| Dashboard web | Funcional — `http://localhost:5000` |
| Crash recovery + graceful shutdown | Implementado |
| Estratégia com edge validado | Nenhuma ainda — ver histórico abaixo |

---

## Estrutura do repositório

```
trading-bot-hyperliquid/
├── src/                          # Bot live (NAO modificar durante validacao)
│   ├── main.py                   # Loop principal
│   ├── strategy.py               # Logica de entrada/saida
│   ├── paper_trading.py          # Motor de paper trading
│   ├── data_aggregator.py        # Agregacao OI/volume/funding
│   ├── exchange_client.py        # Cliente Hyperliquid
│   ├── risk_manager.py           # Gestao de risco
│   ├── dashboard.py / dashboard_web.py
│   └── ...
├── tests/                        # Scripts de validacao (sem imports de src/)
│   ├── validate_vp_strategies.py # Backtest VP + AVWAP — BTC 1h
│   ├── validate_vp_4h.py         # Backtest VP + AVWAP — BTC 4h
│   ├── validate_dual_engine.py   # Backtest dual-engine Z-score v1
│   ├── validate_dual_engine_v2.py# Backtest dual-engine confluencia v2
│   ├── diag_vp_b.py              # Diagnostico direccao/PnL
│   ├── diag_vp_b2.py             # Diagnostico MFE/ATR/regime
│   └── diag_vp_b3.py             # Investigacao trail-to-BE
├── config/
│   └── settings.yaml             # Configuracoes do bot
├── notepad plano_v3.txt          # Plano de implementacao VP/AVWAP
├── QUICKSTART.md                 # Como correr o bot
└── README.md
```

### Branches

| Branch | Conteudo |
|---|---|
| `main` | Bot live + paper trading |
| `vp-strategies` | Validacao Volume Profile + Anchored VWAP (v3) |
| `dual-engine-validation` | Validacao dual-engine Z-score (v1/v2) |

---

## Historico de validacao

Cada abordagem foi testada com rigor estatistico antes de ser abandonada.
Criterio de edge: **PF OOS >= 1.3, desvio-padrao entre janelas < 0.5, N >= 30**.

### v1 — Z-score de volume (BTC 5m, 90 dias)

Sinal: spike de volume (Z > 2.0–3.0) com OI estavel, entrada contrarian.

| Engine | Trades | WR | PF | Resultado |
|---|---|---|---|---|
| A (Spot Momentum) | 578 | 15.7% | 0.58 | FAIL |
| B (Perp + OI Rise) | 9 | 11.1% | 0.40 | FAIL (amostra insuficiente) |

Diagnostico: WR de ~16% e estavel em todos os parametros testados. O Z-score de
volume nao tem direccionalidade — o sinal e simetrico por construcao.

---

### v2 — Confluencia 4/6 condicoes (BTC 5m, 90 dias)

Sinal: score de 6 condicoes (volume Z, OI, funding, range, preco, vela).
Entrada quando score >= 4/6.

| Engine | Trades | WR | PF | Resultado |
|---|---|---|---|---|
| A-STRICT | 39 | — | 0.10 | FAIL |
| A-LENIENT | 160 | — | 0.22 | FAIL |
| B (OI_UP) | 731 | — | 0.35 | FAIL |

Diagnostico: matriz de co-ocorrencia revelou condicoes mortas:
- FUND_H (funding > 0.01%): 0% de activacao em 90 dias
- P_RISE (subida 1.5% em 30min) + RED_C (vela vermelha): mutuamente exclusivos

A confluencia nao filtrou ruido — filtrou sinal.

---

### v3 — Volume Profile + Anchored VWAP (BTC 1h e 4h, 6 meses)

Sinal: rejeicao em VAH/VAL de sessao diaria (VP) ou AVWAP de swing.
Entrada no open da vela seguinte. SL = 0.5 x ATR(14). Trail-to-BE em 50% do TP.

**BTC 1h (183 dias, 4392 candles):**

| Estrategia | Trades | WR | PF | OOS PF | Resultado |
|---|---|---|---|---|---|
| A — Volume Profile | 19 | 5.3% | 0.12 | 0.00 | FAIL |
| B — Anchored VWAP | 96 | 6.2% | 0.19 | 0.39 | FAIL |
| C — Confluencia A+B | 3 | 0.0% | 0.00 | 0.00 | FAIL |

**BTC 4h (183 dias, 1098 candles):**

| Estrategia | Trades | WR | PF | OOS PF | Resultado |
|---|---|---|---|---|---|
| A — Volume Profile | 1 | 0.0% | 0.00 | 0.00 | FAIL |
| B — Anchored VWAP | 17 | 23.5% | 0.77 | 1.02 | FAIL (N insuficiente) |
| C — Confluencia A+B | 0 | — | — | — | FAIL |

Diagnostico detalhado de Strategy B (96 trades, 1h):
- 89.6% dos trades fecham por SL — muito acima do esperado para random walk
- MFE mediana: 42% da distancia ao TP antes de reverter para SL
- 63.9% dos trades que activaram trail-to-BE: activacao e SL hit no mesmo candle
  (wick trap — o high toca o trail trigger intracandle e o low bate no BE stop)
- WR por regime: trending ADX>25 = 2.5%, ranging ADX<20 = 7.9% — perde nos dois
- Tese falsificada: AVWAPs de swing e VAH/VAL nao actuam como suporte/resistencia
  de forma estatisticamente exploravel em BTC nos periodos testados

---

## Metodologia de validacao

Todos os scripts de validacao seguem estas regras:

- **Zero look-ahead**: cada decisao usa apenas dados de candles fechadas
- **Walk-forward OOS**: janelas 90d treino + 30d teste, slide 30d (~3 janelas em 6 meses)
- **Custos realistas**: 0.045% taker + 0.015% maker + 0.02%x2 slip = 0.11% RT
- **Fill honesto**: se preco gapeia SL ou TP no open, fill ao open (nao ao nivel exacto)
- **Criterio de edge**: PF OOS >= 1.3, std < 0.5, N >= 30 → PASS / MARGINAL / FAIL
- **Sem optimizacao parametrica**: resultados reportados como estao, sem curve fitting

---

## Setup e utilizacao

### Requisitos

```bash
pip install -r requirements.txt
```

Copia `.env.example` para `.env` e configura as chaves da Hyperliquid.

### Bot (paper trading)

```bash
python src/main.py
```

Dashboard: `http://localhost:5000`

Para parar: `Ctrl+C` (graceful shutdown — fecha posicoes antes de sair)

### Validacao de estrategias

```bash
# Volume Profile + AVWAP em 1h
python tests/validate_vp_strategies.py

# Volume Profile + AVWAP em 4h
python tests/validate_vp_4h.py
```

Os dados sao descarregados automaticamente da Binance Futures e guardados em
`tests/data/` (ignorado pelo git — regeneravel).

---

## Notas de seguranca

- O bot corre em **paper trading por defeito** — sem dinheiro real
- A pasta `src/` nao e tocada durante validacao de estrategias
- Nenhuma estrategia sobe para o bot live sem passar os criterios de edge OOS
- Chaves e tokens em `.env` (nunca commitado)
