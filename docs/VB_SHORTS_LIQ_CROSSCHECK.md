# VB shorts × liquidações — a hipótese "o flush reverte" refina-se

**Data:** 2026-08-13 · **Script:** `scripts/vb_shorts_liq_crosscheck.py` (read-only)
**Fonte:** forensics VB `data/backtests/vb_forensics_20260813_040003.csv` + `data/live/bot.db`
(liq okx/bybit real 08-09+, proxy 06-08..06-29, candles_1m).

## A pergunta

Os shorts do VB perdem (n=39, **WR 7.7%**, net **−$66.32** vs longs WR 25%,
−$15.47) — a hipótese era: "shorts perdem porque o flush reverte" (um flush de
liquidações empurra o preço para baixo, o VB short monta o breakout, e o flush
reverte, atropelando o short).

## Limitação estrutural imediata

A janela dos trades do VB (06-28 → 08-07) **não se sobrepõe** ao feed real
(okx/bybit começa em 08-09). Só o proxy (06-08..06-29) sobrepõe: **1/39 shorts
tem liquidação proxy ±1 min, 2/39 ±5 min** — amostra nula para o cruzamento
literal "mesmo minuto". O teste divide-se em duas partes independentes.

## PARTE A — o flush reverte? (feed real, 4 símbolos) — **SIM, confirmado**

Flushes 1m ≥ p90 do notional dominante, retorno 30m pós-flush por lado dominante:

| Símbolo | long-liq (vendas forçadas) | short-liq (compras forçadas) |
|---|---|---|
| BTC | 30 flushes → **+0.02%** | 12 → **−0.19%** |
| ETH | 25 → **+0.04%** | 23 → **−0.27%** |
| SOL | 20 → **+0.04%** | 13 → **−0.17%** |
| HYPE | 15 → **+0.07%** | 15 → **−0.01%** |

**Em todos os 4 símbolos, o preço reverte contra o movimento forçado** — vendas
forçadas (long-liq) seguem-se de subida; compras forçadas (short-liq) seguem-se
de queda. A peça mecânica da hipótese é real. (É também o fundamento do fade de
liquidações do harness shadow-live.)

## PARTE B — os shorts do VB são "flush rides"? — **NÃO**

| Métrica | Valor |
|---|---|
| Pre-drop pré-entrada (worst low 30m vs open) | min −0.80%, **mediana −0.08%**, média −0.16% |
| Post30 após entrada | **+0.025%** média; 49% sobem |
| corr(pre_drop, post_ret) | **−0.099** (fraca; significaria mean-reversion se forte) |
| t-stat(post30 "violento" < −0.6% vs calmo) | +0.93 (n=2 vs 37 — não significativo) |

**Os shorts do VB NÃO entram após quedas violentas** — entram após micro-dips
(mediana 0.08%!). Não são "flush rides". O subconjunto mais próximo de um flush
(< −0.6%, n=2) até reverteu (+0.79%) mas é amostra nula.

## Onde as perdas realmente vivem (por exit reason)

| Exit | n | post30 | % sobem | PnL |
|---|---|---|---|---|
| **stop_loss** | 14 | **+0.47%** | **71%** | **−54.01** |
| **failed_breakout_above_mid** | 8 | +0.08% | 75% | −24.56 |
| sl_to_be_hit_r1.0 | 14 | −0.39% | 7% | −2.27 |
| max_hold / trailing / gap | 3 | — | — | +14.53 |

Os dois maiores sangradores (stop_loss −54 + failed_breakout −25 = **−79 de −66
líquido… antes de ganhos**) têm **71-75% dos trades com o preço a SUBIR depois da
entrada**. Isto não é "reversão pós-flush" — é o mercado a ir contra o short
**desde o início**: o breakout down é falso (falha acima do mid), o stop apanha
o movimento contrário imediato.

## Veredito — hipótese refinada, não confirmada como causa

1. **"O flush reverte": CONFIRMADO como fenômeno de mercado** (Parte A, 4/4
   símbolos, feed real) — e é o fundamento do fade de liquidações.
2. **"Os shorts perdem PORQUE o flush reverte": NÃO CONFIRMADO** — os shorts do
   VB não são flush rides (pre-drop mediano 0.08%, nenhum > 0.8%). A causa real
   é seleção de entrada: shorts montados em **breakouts down falsos** (pre-drop
   minúsculo), atropelados pelo mercado que não continua a queda.
3. **Implicação prática:** o rework expansion-only (commit 72f6a38) já bloqueia
   VB fora de expansion, onde viviam 27/39 shorts — a decisão correta não é
   "cortar shorts porque o flush reverte" (a hipótese não explica), mas manter o
   VB longe de trend/low_vol onde os breakouts down falsos sangram.
4. **Próximo passo natural:** o cruzamento literal "shorts × liquidações no mesmo
   minuto" só é possível quando o feed real acumular dados sobrepostos a trades
   VB — re-correr quando real ≥ 30d (o recheck já agenda esse gatilho).
