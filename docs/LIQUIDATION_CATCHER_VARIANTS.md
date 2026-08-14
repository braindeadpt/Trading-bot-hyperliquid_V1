# LiquidationCatcher — variantes que quebram o loop entrada→stop-out

**Data:** 2026-08-14 · **Feed:** real okx+bybit, 08-09 → 08-14 (10.589 eventos)
· **Script:** `scripts/backtest_liquidation_catcher_real.py --variants`

## O loop (baseline)

O backtest real (docs/LIQUIDATION_CATCHER_REAL_BACKTEST.md) expôs o problema:
o LiquidationCatcher entra no **pico do flush** e o **stop-out por liquidação**
— que valida o side da posição a partir da **mesma** janela de liquidações que
gerou o sinal — sai ~1 minuto depois. 17/17 trades saem por
`liquidation_stop_out`, WR 0%, **−150.90 USD**.

Duas famílias de variantes foram testadas para quebrar o loop:

1. **Delay de confirmação** (`--delay-min N`) — entrada espera N minutos
   pós-flush (a mesma ideia do harness fade ETH p90/30m) para o fade apanhar a
   reversão em vez do pico.
2. **Stop-out bypass** (`--stopout-off`) — desliga o stop-out por liquidação
   para esta estratégia (o fade precisa de **deixar o flush reverter**, não de
   sair quando a janela valida o side). Hash-neutral:
   `liquidation_stopout_min_notional_usd = inf` no BacktestConfig.

## Resultados (grid completo)

| variante | n | WR | pnl | delta vs baseline |
|---|---|---|---|---|
| **baseline** (delay=0, stopout=ON) | 17 | 0.0% | **−150.90** | — |
| delay=1 · stopout=ON | 17 | 0.0% | −144.57 | +6.33 |
| delay=3 · stopout=ON | 17 | 0.0% | −162.51 | −11.61 |
| delay=5 · stopout=ON | 18 | 11.1% | −146.35 | +4.55 |
| delay=10 · stopout=ON | 19 | 21.1% | −136.48 | +14.42 |
| delay=30 · stopout=ON | 17 | 0.0% | −220.22 | −69.32 |
| **delay=0 · stopout=OFF** | 19 | 15.8% | **−65.18** | **+85.72** |
| delay=1 · stopout=OFF | 19 | 21.1% | −91.91 | +58.99 |
| delay=3 · stopout=OFF | 19 | 15.8% | −112.06 | +38.84 |
| delay=5 · stopout=OFF | 19 | 26.3% | −105.44 | +45.46 |
| delay=10 · stopout=OFF | 19 | 21.1% | −146.13 | +4.77 |
| delay=30 · stopout=OFF | 17 | 5.9% | −222.68 | −71.78 |

## Leitura — o loop é o EXIT, não a entrada

**O vencedor é o bypass do stop-out, sozinho: +85.72 USD (−150.90 → −65.18,
WR 0% → 15.8%).** Com o stop-out desligado, os trades duram até ao
`max_hold_30min`, o flush reverter (3/19 trades vencem) e o sangramento cai
~57%.

**O delay de confirmação não quebra o loop** — e é revelador o porquê:

* Com o stop-out **ON**, o delay só atrasa a morte: os trades continuam a sair
  por `liquidation_stop_out` (6-17/19), porque o stop-out valida o side de
  qualquer janela subsequente, não apenas a do sinal. Delay=3 até **piora**
  (−162.51).
* Com o stop-out **OFF**, o delay **dilui** o ganho do bypass: delay=0 é o
  melhor (−65.18); cada minuto de espera adicional compra a entrada mais
  cara, porque o flush já reverteu quando o fade entra. delay=10+ destrói o
  edge (≈ baseline).

O harness ETH p90/30m funciona com delay de 1 bar porque **não tem stop-out
de liquidação** — a sua célula vencedora é "fade + hold 30m sem SL". Aqui o
paralelo exacto é `delay=0 + stopout=OFF`: a mesma filosofia (deixar o flush
reverter), não o delay em si.

## Decisão proposta (não aplicada — revisão humana)

1. **Bypass do stop-out para LiquidationCatcher** — candidato a mudança de
   produção: hash-neutral, um `strategy.liquidation_catcher` knob ou o
   per-strategy stop-out. Ganho medido +85.72 na janela real.
2. **Sem delay de confirmação** — o delay sozinho não ajuda e o delay+bypass
   é pior que o bypass puro. Manter entrada imediata.
3. **Rigor**: n=19 é pequeno e o sample é 5 dias de um regime; a célula
   vencedora deve ser validada em shadow live (o feed real continua a
   acumular) antes de tocar no router.

## Artefactos

* Grid completo: `data/research/liq_catcher_variants.json` (gitignored)
* Família curta: `data/research/liq_catcher_short_delay.json` (gitignored)
* Script: `scripts/backtest_liquidation_catcher_real.py --variants`
