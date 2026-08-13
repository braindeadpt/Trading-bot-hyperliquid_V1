# Top-Trader Collapse Test — State Evaluation (2026-08-13)

Pergunta: os colapsos de wallet de top traders estão a caminho de atingir o
gate de evidência (≥8/mês) para validar a hipótese "colapso = liquidação
forçada"?

## Estado dos dados

| fonte | n | span | observação |
|---|---|---|---|
| `top_trader_collapse_events` | **0** | — | nenhum colapso gravado |
| `top_trader_bias_samples` | 6.4k | 08-11 14:03 → 08-13 05:27 | poll 65s, lag ingestão ~5s |
| liquidações reais (okx/bybit) | ~10.5k | 08-09 → 08-13 | feed real, 4 símbolos |

## Achado 1 — O detector funciona, mas só existe desde as 02:17 UTC de hoje

`detect_collapses()` foi adicionado no commit `f940466` (02:17 UTC
2026-08-13). O bot em execução (uptime 3.45h ≈ arrancou 02:55 UTC) é o
primeiro com o detector ativo. **Zero colapsos gravados não é falha do
detector** — testei os 4 casos (posição saiu / wallet sumiu / prev vazio /
drop parcial): o detector dispara corretamente em colapsos reais.

## Achado 2 — Existem 18 flips históricos de long > $50k no agregado

Reconstruí a partir do bias agregado: long notional passa de ≥$50k para $0
entre polls consecutivos (65-127s). Mas a maioria é **artefacto**, não
liquidação:

- **HYPE (8×)** — o long de ~$3M aparece/some/volta com valores quase
  idênticos ($2.98M, $3.02M, $3.08M…). Uma liquidação forçada não volta com
  o mesmo tamanho. Padrão consistente com a mesma wallet a entrar/sair ou
  falha de polling.
- **SOL (7×)** — valores de ~$62k (pouco acima do gate $50k), flips
  repetidos de igual magnitude. Mesmo padrão suspeito.
- **BTC (1×)** — long $43.9M → $0 em 65s, **0 liquidações reais** na janela
  ±10min. Saída voluntária gigante, não liquidação (nenhuma exchange
  rastreada reportou).
- **ETH (2×)** — o caso forte: 08-11 $20.0M (2 liq, $5.4k) e **08-12 14:08
  $21.3M → $0 definitivo (n_long 1→0, nunca mais voltou)**.

## Achado 3 — O cruzamento com o feed de liquidações tem uma limitação estrutural

O feed real é **okx/bybit** — **não Hyperliquid**. As wallets do leaderboard
são HL; uma liquidação forçada HL **não aparece** no feed okx/bybit (o proxy
HL — candle+OI heuristic — foi rejeitado como não-fiável e só cobre junho).
Logo o cruzamento só dá suporte indireto (mercado em movimento), nunca prova.

Melhor caso cruzado — **ETH 08-12 14:08**:
- wallet long ETH de **$21.3M → $0** num poll (65s)
- **75 liquidações reais okx, $6.5M** na janela ±10min (incl. prints de
  $467k e $952k)
- único flip com: tamanho material + definitivo + mercado a liquidar em
  massa no mesmo minuto → **candidato genuíno a liquidação forçada**

## Veredicto do gate (≥8/mês)

**NÃO está a caminho de ser atingido — o gate está no limite e é
inderterminável com a amostra atual.**

- Desde que o detector está ativo (02:55 UTC, ~2.5h): **0 colapsos gravados,
  0 flips novos >$50k**. Ritmo observado com o detector ligado: 0/mês.
- A amostra real útil (feed okx/bybit desde 08-09, ~4.5 dias) contém **1
  candidato genuíno** (ETH 08-12 14:08). Ritmo: ~1/4.5 dias ≈ **6-7/mês** —
  **abaixo do gate de 8/mês**.
- A maioria dos "flips" históricos (15/18) são artefactos (HYPE/SOL
  repetitivos) ou saídas voluntárias sem liquidação (BTC) — inflamariam o
  contador se o detector os apanhasse, mas não são evidência.

**Risco metodológico adicional:** o detector usa `wallet_positions` bruto
antes da filtragem de erros — uma wallet que falhe a responder num poll
(timeout) desaparece do `cur` e parece um colapso de 100%. O gate de
`min_from=$50k` reduz, mas não elimina, falsos positivos por falha de rede.

## Decisão

1. **Não construir estratégia sobre colapsos de top wallets.** O gate ≥8/mês
   não é atingível com o ritmo atual de colapsos genuínos (~6-7/mês) e a
   amostra tem 1 evento forte em 4.5 dias.
2. **Deixar o detector a correr e re-avaliar aos 30 dias** — se o ritmo de
   ~6-7/mês se mantiver com ≥8 colapsos genuínos, o gate é atingido e a
   hipótese ganha tração; se ficar perto de 2-3, morre por falta de eventos.
3. **Endurecer o detector** antes de confiar nos números: ignorar flips com
   `errors>0` no poll (a wallet pode não ter respondido) e deduplicar por
   wallet-símbolo (as repetições HYPE/SOL contam 1 vez, não 8).
4. **Não usar o cruzamento okx/bybit como prova** — é suporte indireto;
   a prova exigiria um feed de liquidações HL (inexistente, fstream bloqueado).

## Próximo passo concreto

Se quiseres manter a hipótese viva com custo ~zero: endurecer o
`detect_collapses` (ignorar polls com erros + deduplicação) e agendar a
re-avaliação aos 30 dias. Alternativa de maior valor: canalizar o mesmo
orçamento de pesquisa para o sinal de bias em si (que já tem pipeline e
gate definidos, só precisa de ≥20 datas).
