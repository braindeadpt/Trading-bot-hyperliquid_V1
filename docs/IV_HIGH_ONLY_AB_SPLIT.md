# IV 'ambas em high_iv only' — A/B com janelas independentes de 30d

Gerado: 2026-08-13 10:32 UTC · janela 2026-05-18 -> 2026-08-07 · split 30d · high_iv = DVOL percentil(30d) > 66.7 · DVOL diário Deribit (BTC + ETH vol index; SOL/HYPE = proxy BTC).

## Sumário por janela independente (sem sobreposição)

| janela | sem gate | high_iv only | bloqueados | n high_iv |
|---|---|---|---|---|
| 2026-05-18..2026-06-16 | +0.00 (n=0) | **+0.00** (n=0, WR 0%) | +0.00 (n=0) | 0 |
| 2026-06-17..2026-07-16 | -12.61 (n=49) | **+38.09** (n=12, WR 58%) | -50.70 (n=37) | 12 |
| 2026-07-17..2026-08-07 | -92.01 (n=79) | **+4.89** (n=1, WR 100%) | -96.90 (n=78) | 1 |
| **TOTAL** | **-104.62** | **+42.98** | **-147.60** | 13 |

high_iv-only positivo em **2/3** janelas independentes; net total +42.98 USD.

## Por estratégia (high_iv only)

| janela | VB high_iv | VWAP high_iv |
|---|---|---|
| 2026-05-18..2026-06-16 | +0.00 (n=0) | +0.00 (n=0) |
| 2026-06-17..2026-07-16 | +7.68 (n=5) | +30.41 (n=7) |
| 2026-07-17..2026-08-07 | +0.00 (n=0) | +4.89 (n=1) |

## Veredito

Net high_iv-only **+42.98 USD** (n=13) em janelas independentes — **o +42.99
sobrevive ao caveat de sobreposição no sentido estrito**: positivo nas duas
janelas não-vazias (W2 +38.09, W3 +4.89), nunca negativo. **MAS é
INCONCLUSIVO**: n=13 (<<30) e **12 dos 13 trades estão na W2 (pico de DVOL de
junho)** — a janela mais recente (W3, 22d) tem **1 único** trade high_iv
(VWAP +4.89). É um filtro de perdas (bloqueia −147.60), não um gerador de
PnL, e o sinal é demasiado esparso para promover.

## Contexto

* A variante veio de `docs/IV_PERCENTILE_REGIME_GATE.md` (+42.99, n=13, janela única 05-18..08-07, dominada pelo pico de DVOL de junho).
* A classificação high/low-IV é *rolling* (janela 30d) — independente do split; o split só muda a agregação dos trades.
* DVOL é informação implícita nova, aplicada post-hoc ao nível do trade — nenhuma mudança ao settings.yaml nem à janela congelada.
* JSON: `iv_high_only_ab_split_20260813_113227.json`.

