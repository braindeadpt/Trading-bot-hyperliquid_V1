# IV (DVOL implícito) vs ADX router (realizado) — prova OUT-OF-SAMPLE 08-07→08-13

Gerado: 2026-08-13 13:06 UTC · janela **2026-08-07 -> 2026-08-13** (fora da
amostra 05-18→08-07 que gerou ambos os gates) · IV keep = DVOL pct(30d) > 66.7 ·
ADX keep = VB {expansion} / VWAP {low_vol, unknown} · ADX(14) 15m fechado
(range 20 / trend 25). Mesma maquinaria de `scripts/iv_vs_adx_disagreement.py`.

## Concordância / discordância (janela fresca, 15 trades)

| célula | n | net | WR |
|---|---|---|---|
| ambos mantêm | 0 | +0.00 | 0.0% |
| IV mantém / ADX bloqueia | 0 | +0.00 | 0.0% |
| **IV bloqueia / ADX mantém** | **4** | **−6.76** | 25.0% |
| ambos bloqueiam | 11 | −4.14 | 36.4% |

| keep-set | n | net |
|---|---|---|
| IV (DVOL) | **0** | +0.00 |
| ADX (realizado) | 4 | −6.76 |
| união | 4 | −6.76 |
| interseção (ambos) | 0 | +0.00 |

## In-sample vs OOS (mesmas células)

| célula | in-sample 05-18..08-07 | **OOS 08-07..08-13** |
|---|---|---|
| ambos mantêm | n=4, **+30.24** | n=0 |
| IV mantém / ADX bloqueia | n=9, **+12.75** | n=0 |
| IV bloqueia / ADX mantém | n=25, **−12.81** | n=4, **−6.76** |
| ambos bloqueiam | n=90, −134.76 | n=11, −4.14 |
| IV keep-set | n=13, **+42.99** | n=0 |
| ADX keep-set | n=29, +17.43 | n=4, −6.76 |

## Leitura honesta

1. **A direcção sobrevive na única célula de discordância com dados.** O DVOL
   voltou a "ter razão" onde os gates discordam: o ADX manteve 4 trades que o IV
   bloquearia e **todos sangraram** (net −6.76, WR 25%). Fora da amostra, o lado
   "IV bloqueia sangria" continua correcto — os trades que o router ADX mantém
   são exactamente os que perdem.
2. **Mas a prova é unilateral e minúscula.** **Zero trades high_iv** na janela
   fresca (o percentil DVOL recuou abaixo de 66.7 após o pico de junho e ficou lá
   os 6 dias). O lado "IV mantém vencedores" (o +42.99 in-sample) fica
   **inobservável** — não há confirmação nem contradição; simplesmente não houve
   nada para manter. A célula "IV mantém/ADX bloqueia" (n=9, +12.75 in-sample)
   não tem OOS.
3. **A janela foi sangrenta no geral** (15 trades, net −10.90): ambos bloqueiam
   11/15 (−4.14) e o keep-set ADX inteiro é negativo (−6.76). Isto bate com a
   análise anterior dos "6 dias frescos" (trades permitidos ≈ −6.7). Num regime
   assim, um gate que bloqueia **tudo** teria poupado os −10.90 — o que favorece
   filtros conservadores, mas não distingue IV de ADX.
4. **Tabela conjunta (OOS):** low_vol/low_iv −5.41, expansion/low_iv −6.36,
   trend/low_iv +1.79 — o único slice positivo é trend/low_iv (n=4), enquanto o
   resto sangra. Nada de high_iv para comparar com a coluna positiva do in-sample.

## Veredito

**Direcional, não confirmatório.** O DVOL continua a vencer o ADX onde discordam
na direcção testável (bloqueia os 4 bleeders do ADX, −6.76), mas n=4 é ruído, o
lado "mantém vencedores" não tem amostra OOS (0 trades high_iv) e o keep-set IV
vazio não permite afirmar que o gate completo teria ganho. A hipótese
"o implícito antecipa o realizado" **não é refutada** — precisa de mais janelas
com alta IV (shadow-live já a recolher: `variant=iv_gate_shadow`).

## Contexto

* Mesmos trades raw (sem gate); DVOL implícito (Deribit diário, trailing 30d)
  vs ADX realizado (candles 15m). Nenhuma mudança à janela congelada.
* JSON: `iv_vs_adx_disagreement_20260813_140652.json`.
