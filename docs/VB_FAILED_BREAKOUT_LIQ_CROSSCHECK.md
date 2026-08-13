# Failed_breakout do VB × liquidações — o cruzamento literal é impossível, o proxy mostra reversão fraca mas o rework já resolve

**Data:** 2026-08-13 · **Script:** `scripts/vb_shorts_liq_crosscheck.py` (PART C)
**Fonte:** forensics VB `data/backtests/vb_forensics_20260813_040003.csv` + candles_1m

## A pergunta

Os 20 trades `failed_breakout` do VB (WR 5%, net **−$55.47**) — a hipótese era
"shorts perdem porque o flush reverte": um flush de liquidações empurra o preço,
o short monta o breakout, e a reversão pós-flush atropela o short.

## Limitação estrutural (igual ao estudo anterior)

Os 20 trades vão de **07-07 a 08-07** — **0/20 sobrepõem qualquer feed de
liquidações** (real okx/bybit começa 08-09; proxy acaba 06-29). O cruzamento
literal "mesmo minuto" é impossível com os dados atuais.

## PART C — proxy de reversão (post30, pre_drop)

| Grupo | n | pre_drop | post30 | % sobem | PnL |
|---|---|---|---|---|---|
| **shorts** (todos above_mid) | 8 | −0.19% | +0.083% | **75%** | −24.56 |
| **longs** (todos below_mid) | 12 | −0.77% | +0.039% | 58% | −30.91 |
| total | 20 | −0.54% | +0.057% | 65% | −55.47 |

Leitura:
1. **Os 8 shorts failed_breakout sobem 75% das vezes após a entrada** — o preço
   reverte contra o short na maioria dos casos. Consistente com a direção da
   hipótese (o movimento contra o short após a entrada).
2. **Mas a magnitude é fraca** (+0.083% post30 médio, pre_drop minúsculo de
   −0.19%) — os shorts do VB entram em micro-movimentos, não após flushes
   violentos (igual à descoberta do estudo anterior: pre-drop mediano 0.08%).
3. **Os longs também perdem** (−30.91, 58% sobem depois) — o padrão não é
   exclusivo dos shorts: é o *failed breakout* em si (entrar contra a continuação
   do movimento, e o movimento continua). A leitura "só os shorts são vítimas do
   flush" não se sustenta — ambos os lados sangram quando o breakout falha.

## O facto que torna o debate académico: o rework já resolve

| | total | shorts |
|---|---|---|
| failed_breakout em trend/low_vol (bloqueados pelo rework expansion-only) | **19/20** | **7/8** |
| sobrevivem em expansion | 1/20 | 1/8 |

O rework expansion-only (commit `72f6a38`, `VB_REGIMES = {"expansion"}`) **já
remove 19 dos 20 failed_breakout** — a esmagadora maioria destes trades vive em
trend (14) e low_vol (5), precisamente os regimes que o rework agora bloqueia.
Independentemente da causa (flush reversal ou continuação), o problema está
quase todo eliminado pelo gate de regime.

## Veredito

1. **Cruzamento literal: impossível hoje** (0/20 na janela de qualquer feed).
2. **Proxy:** a direção da hipótese (preço contra o short após entrada) aparece em
   75% dos shorts, mas com magnitude fraca e replicação nos longs — o *failed
   breakout* é o denominador comum, não o lado.
3. **Implicação prática:** o rework expansion-only já remove 19/20 destes trades;
   não é necessário nenhum gate adicional de failed_breakout. Re-correr o
   cruzamento literal quando o feed real ≥ 30d (o recheck já o agenda).
