# Prereg: Funding Carry cross-sectional (paper, HL)

Registado: 2026-11-21 22:46 UTC, aprovado pelo dono, ANTES de qualquer dado ter sido olhado
(nenhum funding, retorno ou backtest foi consultado para esta regra). Base de
implementação: a maquinaria do ResMom (`notes/resmom-forward-prereg-2026-10-28.md`)
com outro sinal. Alterações depois deste commit só por emenda datada, nunca
por edição.

## Hipótese (uma frase, falsificável)

Numa carteira market-neutral de perps HL, long nos perps com funding
persistentemente negativo e short nos de funding persistentemente positivo
tem retorno líquido positivo depois de custos, porque recebe funding dos dois
lados e o prémio de carry não é anulado pelo movimento de preço.

## Regra (congelada ao registar)

- Universo: igual ao ResMom: top 25 perps HL por volume médio notional 30d,
  >= 90 dias de histórico HL, sem stables. **Point-in-time**: universo
  recalculado em cada data, incluindo moedas delistadas depois (sem viés de
  sobrevivência). BTC entra no ranking, sai das pernas.
- Sinal: `s = -media(funding horário HL, últimos 7 dias)`.
- Carteira: long quintil topo de `s`, short quintil fundo (`floor(0.2*n)`),
  pesos proporcionais a `1/vol30`, gross 0,5 por perna, beta-neutral vs BTC
  (beta OLS 60d), gross total 1,0.
- Tecto por moeda: nenhuma perna acima de 10 % do gross total; o excesso é
  redistribuído pelas restantes pernas do mesmo lado, proporcionalmente aos
  pesos 1/vol. Motivo, fixado antes dos dados: no ResMom forward uma só semana
  teve pernas de micro-caps a ±100 % (AKE +121 %, JCT −81 %, BTW −54 %) a
  dominar o resultado, e os funding extremos vivem nessas moedas.
- Rebalance: semanal, segunda 00:00 UTC com dados até ao close de domingo
  (cadência do ResMom: reaproveita o daemon e mantém o turnover baixo).
- Custos: 6,5 bps por lado sobre o turnover; funding HL horário real aplicado
  (o carry é o objecto do teste, não um custo).
- Notional paper: 10 000 USD gross. Sem capital real.

## Fase 1 — histórico (decide se avança)

- Janela: todo o histórico disponível, em janelas de 30 dias sem
  sobreposição; **mínimo 4** (abaixo disso: INCONCLUSIVO, não corre).
- Nulos: 200 carteiras de sinal aleatório no mesmo universo, pesos e custos.
- PASS (todos):
  1. retorno líquido acumulado > 0;
  2. Sharpe anualizado > 0,5 **e** acima do p95 dos nulos;
  3. retorno líquido > 0 na maioria das janelas, com teste de sign-flip por
     janela p <= 0,10;
  4. PnL sem os 3 maiores contribuintes > 0 (não depender de 3 moedas).
- **KILL:** qualquer critério falhado com >= 4 janelas -> C. Sem segunda volta
  de lookbacks, quantis, universo ou dia de rebalance.

## Fase 2 — forward (só se a Fase 1 passar)

- Mesma regra, sem alterações. Leitura única aos 26 rebalances, com os
  critérios de PASS do ResMom forward.
- KILL antecipado: drawdown líquido <= -35 %, ou erro de implementação que
  impeça reproduzir o sinal.

## Diagnósticos obrigatórios (não são critério)

- Correlação do sinal com o sinal do ResMom e do PnL com o PnL do ResMom: se
  forem altas, isto é momentum disfarçado de carry, e diz-se.
- Decomposição do PnL em funding recebido/pago e movimento de preço.
- Turnover semanal e custo total pago.

## Riscos declarados

- Carry anulado por momentum (funding alto costuma existir porque o preço
  sobe).
- Squeezes nas pernas short de alts com funding alto: o market-neutral não os
  cobre.
- Liquidez e slippage nas alts pequenas do universo.
- 26 semanas têm pouco poder estatístico: PASS = consistência, não prova.

## Visualização

Ledger no formato que o `src/research/paper_trades.py` lê: cartão, posições
abertas, trades fechados, equity e drawdown na dashboard, como os outros
experimentos em paper.

## Dados e infraestrutura

`fundingHistory` (API pública HL, horário por moeda) e candles diários.
Nenhum colector novo, nenhuma escrita no `bot.db`, nenhuma alteração ao
`settings.yaml` nem ao motor.
