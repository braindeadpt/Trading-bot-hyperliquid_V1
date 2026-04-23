# 📈 Estratégia de Trading Explicada

> _Escrito para o Pedro, que entende de trading mas nunca programou. Nenhuma equação assustadora, prometo._

---

## O que é a Estratégia de Momentum?

**Momentum** = movimento + força.

Imagina que estás na bancada de uma praia a ver as ondas. De repente, vês uma onda que é **mais alta que o normal** e que está a **ganhar velocidade rapidamente**. Isso é momentum — movimento com força por detrás.

No trading, momentum significa que o preço está a mover-se numa direção com **força suficiente para continuar** (pelo menos a curto prazo). Não é adivinhar — é reconhecer quando "algo grande está a acontecer".

O nosso bot não tenta adivinhar se o Bitcoin vai subir ou descer amanhã. Ele tenta capturar **os momentos em que o dinheiro está a entrar em massa** e o preço reage a isso.

---

## Open Interest (OI) — O Indicador Secreto

### O que é?

**Open Interest** = número total de contratos em aberto num mercado de futuros.

Pensa no OI como o **"contador de apostas ativas"**. Quando o OI sobe, significa que **mais dinheiro novo está a entrar no mercado**. Mais pessoas estão a fazer apostas. Mais interesse.

### Porque é que importa?

| OI a subir + Preço a subir | = | Dinheiro novo a entrar para COMPRAR (forte) |
| OI a subir + Preço a descer | = | Dinheiro novo a entrar para VENDER (forte) |
| OI a descer + Preço a subir | = | Pouca convicção — pode desabar |

O bot usa o OI como **confirmação**. Um spike de volume sozinho pode ser "ruído". Volume + OI a subir = "alguém com dinheiro sério está a mover-se".

> 💡 **O nosso bot busca OI agregado** de 3 exchanges (Binance, Bybit, OKX). Isto dá uma visão **global** do mercado, não só da Hyperliquid.

### Threshold do bot

O bot dispara quando o OI sobe **mais de 1%** (`oi_change_threshold: 0.01`). Parece pouco, mas 1% em bilhões de dólares é **muito dinheiro novo**.

---

## Spikes de Volume — Como o Bot Deteta

### O que é um spike de volume?

Normalmente, o Bitcoin troca de mãos a um certo ritmo (ex: $500 milhões por hora). De repente, numa janela de 15 minutos, trocam $2 bilhões. Isso é um **spike** — 4x o volume normal.

### Como o bot calcula

1. Olha para os últimos **100 períodos** de volume (em 15m, isto são ~25 horas)
2. Calcula a **média** desse volume
3. Compara o volume atual com essa média
4. Se o volume atual for **4x ou mais** (`volume_spike_threshold: 4.0`) → spike confirmado

> 🧠 **Porque 4x?** Porque 2x pode ser coincidência. 4x é "algo a sério está a acontecer". Os nossos backtests mostraram que 4.0 é o valor mais rentável para BTC em 15m.

### O funding rate entra na equação

O **funding rate** é uma taxa que os traders pagam para manterem posições abertas.

- Funding **muito positivo** = muita gente comprada (aposta a subir) → risco de "squeeze" para baixo
- Funding **muito negativo** = muita gente vendida (aposta a descer) → risco de "squeeze" para cima

O bot **evita trades** quando o funding está extremo (`> 1%` ou `< -1%`). Isto é uma proteção contra "armadilhas".

---

## Trailing Stop — O Guardião do Teu Dinheiro

### O que é?

Um **trailing stop** é como um "seguro de viagem" que se move contigo.

- **Stop loss fixo:** "Se perder 2%, saio." → Fica sempre nos mesmos 2%.
- **Trailing stop:** "Se o preço sobe 5%, o meu stop sobe para 3.5%. Se sobe mais, o stop sobe mais." → **Acompanha o lucro.**

### Como funciona no bot

1. Entras num trade a $70,000
2. O preço sobe para $71,500 (lucro de +1.5%)
3. O **trailing stop ativa-se** (trailing_activation_pct: 1.5%)
4. O stop coloca-se a $70,785 (1.5% abaixo do máximo atingido)
5. Se o preço continuar a subir para $72,000, o stop sobe para $71,100
6. Se o preço começar a descer, o stop **mantém-se no ponto mais alto** — protege os teus ganhos

> 🛡️ **O trailing stop é a tua arma número um.** É o que transforma um trade que subiu 5% num trade que **ganhou** 3.5%, em vez de um trade que **perdeu** 2% porque ficaste a esperar demais.

### Os números do bot

```yaml
trailing_activation_pct: 0.015   # Ativa quando lucro ≥ 1.5%
trailing_stop_pct: 0.015         # Coloca o stop 1.5% abaixo do máximo
```

---

## Porque 15 Minutos (Timeframe)?

### Testámos vários timeframes:

| Timeframe | Profit Factor | Win Rate | Observação |
|-----------|---------------|----------|------------|
| **15m** | **2.50** | **72.2%** | 🏆 Vencedor claro |
| 5m | 1.45 | ~55% | Longs perdem dinheiro (PF 0.73) |
| 30m | Não testado | — | Demasiado lento para este estilo |

### Porque é que 15m ganha?

1. **Não é demasiado rápido:** Em 1m ou 5m, há muito "ruído" — movimentos aleatórios que parecem sinais mas não são. O bot fica confuso.
2. **Não é demasiado lento:** Em 1h, o momentum já passou. Entras tarde demais.
3. **15m é o "sweet spot":** Captura movimentos reais (com volume real por detrás) sem reagir a cada fluctuação pequena.

### Mas os traders profissionais olham para timeframes menores, certo?

Sim, mas eles têm **anos de experiência** e reagem em segundos. O bot é automático — precisa de um timeframe onde os sinais sejam **claros e robustos**. 15m oferece isso.

> 📊 **Os dados provam:** 36 trades em 30 dias de backtest. 72.2% de acerto. Profit Factor 2.50. Longs PF 1.75, Shorts PF 3.61. Os números não mentem.

---

## Métricas de Performance Explicadas

### Profit Factor (PF)

> **PF = (soma dos ganhos) / (soma das perdas)**

- **PF > 2.0** → Excelente. Cada euro perdido é recuperado com 2 euros ganhos.
- **PF > 1.5** → Bom. Estratégia tem edge.
- **PF < 1.0** → Perdes dinheiro. Não usar.

O nosso bot: **PF 2.50** em 15m. Isto significa que, em média, quando ganha, ganha 2.5x mais do que quando perde.

### Win Rate (WR)

> **WR = (trades ganhos / total de trades) × 100**

- **WR > 60%** → Bom.
- **WR > 70%** → Muito bom.

O nosso bot: **WR 72.2%**. Quase 3 em cada 4 trades são ganhos.

> ⚠️ **Atenção:** Um WR alto não basta. Se ganhas 1€ em 7 trades e perdes 10€ no 8º, o WR é 87.5% mas perdes dinheiro. É por isso que o **Profit Factor é mais importante**.

### Max Drawdown (DD)

> **DD = queda máxima do capital desde o pico mais alto.**

- **DD < 5%** → Excelente gestão de risco.
- **DD < 10%** → Aceitável.
- **DD > 20%** → Perigoso.

O nosso bot: **DD 0.29%** em backtest. Isto significa que, mesmo na pior altura, só perdeste 0.29% do teu capital desde o pico. Isto é **excecionalmente baixo** — graças aos stops apertados.

### Total Trades / Trades Diários

O bot limita-se a **5 trades por dia** no máximo. Isto evita "overtrading" — a tentação de fazer trade por trade, que é como os traders amadores quebram.

---

## Resumo da Estratégia (para Colar na Parede)

```
ENTRADA LONG (comprar) quando:
  1. Volume atual é 4x maior que a média dos últimos 100 períodos
  2. OI global subiu mais de 1%
  3. Funding rate NÃO está extremo (>1% ou <-1%)
  4. Preço está acima da média móvel de 100 períodos (confirmação)

SAÍDA quando:
  1. Stop loss de 2% é atingido
  2. Trailing stop ativa (depois de +1.5% de lucro)
  3. OI começa a descer (>0.5%) — momentum a esgotar-se

POSIÇÃO:
  - Máximo $100 por trade
  - Alavancagem máxima 2x
  - Máximo 5 trades por dia
```

---

> 🎯 **Lembra-te:** A estratégia não é perfeita. Nenhuma é. Mas é **testada**, **disciplinada**, e **protegida por risco**. Isso é mais do que 90% dos traders amadores têm.
