# Pre-registo — BorosLockedSpread (shadow, sem capital)

Registado: 2026-10-15 04:12 WEST, aprovado pelo Pedro. Nenhum dado forward foi
visto antes deste registo.
O snapshot de 2026-10-15 03:50 serve só para o desenho (ver
boros-study-2026-10-15-addendum.md).

## Hipótese

Um spread de funding cross-venue fixado no Boros (perna HL vs venue B), mantido
até ao vencimento, rende ≥8%/ano líquido sobre o capital comprometido.
Isto depois de execução realista, de fees e da deriva do basis de mark entre
venues. O lucro é conhecido à entrada, por isso o shadow mede a erosão (fills,
saída, basis, settlement), não uma previsão.

## Universo

- Mercados Boros em que uma perna é Hyperliquid (YU simples ou spread market
  HL−B).
- B ∈ {Binance, Gate, OKX, Bybit, Lighter, KuCoin}.
- Ativos BTC, ETH e HYPE no gate; os restantes são descritivos.
- Vencimento entre 14 e 90 dias à entrada.

## Snapshot

De hora a hora, ao minuto :05 UTC:

- Boros: open-api/v1/markets com bestBid/bestAsk/OI (e o order book, se a API
  o expuser).
- Books L2 dos perps em HL e em B, via APIs públicas.

Todos os snapshots são guardados.

## Trade de papel

- N = $10.000 por perna perp.
- Direção: a que recebe o maior fixo (short YU+perp na venue rica, long
  YU+perp na barata).
- Fixo bruto = bid YU da venue rica − ask YU da venue barata (ou o bid/ask do
  spread market, se existir; usa-se o melhor dos dois).
- Custos à entrada:
  - Perps: taker VIP0 (HL 4,5 bps; Binance 5; Gate 5; OKX 5; Bybit 5,5;
    Lighter 0; KuCoin 6), mais slippage VWAP no book para N, ×2 (abrir e
    fechar).
  - Boros: taker 0,05%/ano e settle fee por perna (extConfig), pro rata ao
    tempo até ao vencimento. Se não houver book Boros, soma-se 1 bp/ano por
    perna como slippage.
- Líquido sobre o nocional = bruto − fees Boros − (custos perps / T × 365).
- Capital (variante primária, POOLED tipo CrossEx) = 0,2·N (perps a 10x por
  perna, margem partilhada) + 0,1·N por perna Boros.
- ENTRADA se o líquido sobre o capital for ≥ 8%/ano. Só 1 trade aberto por
  (ativo, par de venues, vencimento) e no máximo 1 entrada nova por ativo por
  dia.
- Saída: no vencimento. Os perps fecham ao snapshot seguinte (taker + VWAP
  reais desse momento).

## PnL realizado por trade

- Fixo travado × N × T.
- Mais o descasamento floating: o funding real pago/recebido nos perps (APIs
  de funding HL/B) − o floating liquidado pelo Boros.
- Mais o basis: (ΔP perna long) − (ΔP perna short) × qty.
- Menos os custos reais da entrada e da saída.
- Expresso em % sobre o capital, anualizado.

## Liquidações (hipotéticas)

- POOLED: morte se a deriva acumulada do basis entre venues consumir a margem
  de 0,2·N até à MM (~0,5% do nocional por perna).
- Boros: morte se a MTM do YU passar a margem de 0,1·N.
- SPLIT (descritiva, contas separadas sem CrossEx, 3x por perna): regista-se
  se algum movimento adverso de uma perna ≥ 30% teria liquidado.

## Leitura

A n ≥ 10 clusters independentes fechados (o mesmo ativo com janelas
sobrepostas conta 1 cluster) ou a 2027-06-30, o que vier primeiro.
PASS se TODAS:

1. Média do realizado sobre o capital ≥ 8%/ano.
2. Mediana realizado/travado ≥ 0,7.
3. PF por trade > 1,5.
4. Zero liquidações hipotéticas POOLED e Boros.

KILL antecipado:

- Qualquer liquidação hipotética POOLED ou Boros; ou
- A n = 5 fechados, mediana realizado/travado < 0,5.

Sem outras espreitadelas ao PnL. Contagens de entradas e de quase-entradas
(≥5%) são permitidas.

## Notas

- Se houver poucas entradas, isso é um resultado em si (a oportunidade é rara
  a ≥8%). O limiar não se baixa sem uma emenda forward.
- Implementar no bot exigiria uma conta CrossEx (Gate, suporta HL) ou contas
  separadas (SPLIT, com rendimento muito menor).
- Isolamento: módulo novo e app pm2 nova 'boros-shadow', só com aprovação.
  Não toca em Jev, VWAP, CarryA1, UnlockShort, config_hash nem runtime_state.

---

## Notas de implementação (não alteram regras)

- `src/boros_shadow/` é o módulo; tabelas `boros_*` em
  `data/research/boros_shadow.db` (snapshots zlib + sha256).
- Venues normalizadas: HL `info l2Book`, Binance `fapi/v1/depth`, Gate
  `futures/usdt/order_book`, OKX `market/books`, Bybit `market/orderbook`,
  KuCoin `level2/depth20`, Lighter `orderBookOrders` (BTC=1, ETH=0, HYPE=24).
- "Sem book Boros" é o estado permanente — a API pública não expõe depth; o
  1 bp/ano por perna aplica-se sempre (documentado como condicional no código
  para o dia em que um book exista).
- Ativos fora do gate {BTC,ETH,HYPE}: avaliados e gravados com
  `block_reason=off_gate_asset` (descritivos), nunca elegíveis.
- Janela on-schedule = minuto UTC ∈ [5,10): tolera o atraso do loop
  (TICK_S=60 s). Passes fora da janela são descritivos (candidatos
  avaliados, `block_reason=off_schedule`, nunca abrem) e NÃO consomem a
  hora — o passe da janela corre à mesma. Dentro da janela, no máx. um
  passe por hora (`last_snapshot_hour`); descritivos idem
  (`last_pass_hour`). O meta do snapshot grava `on_schedule` e o
  `minute_utc` real. Medição operacional da cadência — não altera as
  regras.

## Notas de implementação — Fase 2 (fixadas 2026-10-15, antes de qualquer trade)

Medição fixada ex ante; a produção tinha 0 entradas quando escrito.

- **Marcação**: todos os passes (descritivo :00 e on-schedule :05–:09)
  gravam uma linha `boros_marks` por trade aberto: mid + VWAP de saída
  por perna perp, mid YU por perna Boros (`(bestBid+bestAsk)/2` do mesmo
  marketId), funding acumulado, floating acumulado, `on_schedule`. Perna
  sem book reusa o último mid (`stale=1`); stale > 6 h → `data_gap=1`.
- **Funding real**: HL `info fundingHistory` (POST), Binance
  `fapi/v1/fundingRate`, Gate `futures/usdt/funding_rate` (t em
  segundos), OKX `public/funding-rate-history`, Bybit
  `market/funding/history`, KuCoin `contract/funding-rates`. Incremental
  desde a entrada (cursor = último evento gravado; UNIQUE(trade_id, leg,
  ts_ms)). Sinal: short recebe quando rate>0; cash = ±rate×N por evento.
- **Lighter** (emendado 2026-10-15, entries=0): sem histórico público
  de funding. A rate corrente é amostrada a cada passe (`funding-rates`
  público, rows `exchange='lighter'` para os market_ids BTC=1, ETH=0,
  HYPE=24) e gravada em `boros_rates` — série própria acumulada
  forward desde a entrada: cash = sinal × rate × N × Δt/8h por
  intervalo de passe — a rate do endpoint é por período de 8h
  (pin empírico 2026-10-15: rate × 1095 == floatingApr Boros, exacto
  para BTC e ETH; guardado por teste network com tolerância 30%),
  `funding_source='lighter_sampled'`. Numa perna Lighter sem amostra
  no intervalo, e em qualquer venue cujo histórico falhe no intervalo,
  o funding da perna é proxied ao floating Boros medido —
  `funding_source='venue_proxy'`, descasamento da perna 0 por
  construção. Nunca funding=0 com o floating a contar (enviesaria o
  medido).
- **Floating liquidado pelo Boros**: a open-api não expõe histórico de
  settlement por marketId (sondado 2026-10-15: `/markets/{id}/*`,
  `/settlements`, historical-data.boros.finance é portal de downloads,
  não API). A fonte primária é `data.floatingApr` do próprio payload de
  markets — o índice que o Boros liquida a cada `paymentPeriod` (3600 s)
  — integrado por intervalo entre marcas: `fl × N × Δt/ano`, sinal da
  posição YU (long recebe, short paga). `floating_source =
  'boros_floating_apr'`. Se a row do marketId faltar/estalecer num
  intervalo → `floating_source='venue_proxy'`: o funding real da venue
  subjacente no mesmo intervalo com sinal oposto — descasamento 0 por
  construção nesse intervalo, como o pré-registo manda.
- **Spread markets**: a perna Boros única flutua o diferencial
  (funding_HL − funding_B); proxy = soma dos fundings das duas pernas
  perp com sinal oposto → também cancela a 0.
- **Settle fee Boros**: acumula pro rata ao vencimento — componente
  Boros dos custos = `costs_usd` gravado na Fase 1 menos a parte perp
  estimada ((taker+slip)·N·2). Em custos reais no fecho, a parte Boros
  é a mesma (taker+settle+1 bp/ano por perna — sem book, sempre).
- **Liquidações hipotéticas** (verificadas em cada passe, marks frescos
  ou stale — marcação a stale é o conservador):
  - POOLED: `equity = 0,2·N + uPnL_perps(mid) + funding_líquido`;
    morte se `equity ≤ 0,005·N × 2` (MM ~0,5%/perna).
  - BOROS: por perna, `MTM = sinal·(YU_mid − fixo_entrada)·N·T_rest`;
    morte se `MTM ≤ −0,1·N`.
  - SPLIT (descritivo): excursão adversa máxima de qualquer perna perp
    ≥ 30% desde a entrada → flag `split_liq` (nunca kill).
  - Morte POOLED/BOROS: `boros_flags.liquidation` + trade `liquidated`,
    realizado = −margem da camada (−0,2·N ou −0,1·N × nº pernas Boros).
- **Fecho**: primeiro passe ON-SCHEDULE com ts ≥ vencimento; perps a
  taker+VWAP real; YU expira sem custo. Book em falta → retry nos passes
  on-schedule até 24 h → depois mid com slippage = 2× slip de entrada
  (`close_fallback=true`). Market Boros fora do universo/state≠Normal
  antes do vencimento → fecho nesse passe, último mid conhecido,
  `anomaly` no flags.
- **PnL realizado** (`boros_close`, selado até ao gate): `fixed =
  fixo·N·T_held`, `floating = funding + floating_signed`,
  `basis = Σ sinal·(exit−entry)·qty` (VWAP entrada→saída),
  `costs = entrada real + saída real + fees Boros`, `realized = soma`,
  `realized_apy_cap = realized/capital × ano/dias`, `locked = net_apy à
  entrada × capital × T_plan`, `ratio = realized/locked`.
- **Clusters**: union-find sobre trades fechados — mesmo ativo e janelas
  [entrada, fecho] sobrepostas fundem; `cluster_id` = menor trade_id do
  componente (determinístico, recalculado a cada fecho).
- **KILL n=5**: a 5 fechados, mediana interna de `ratio` < 0,5 →
  `kill_flags.n5_kill = true`. Só o booleano é exposto; a mediana
  permanece selada.
- **Leitura** (`read.py`): recusa antes de 10 clusters fechados ou
  2027-06-30; depois reporta média `realized_apy_cap`, mediana `ratio`,
  PF por trade, nº de liquidações e os 4 critérios PASS/FAIL.
- **Backfill**: trades abertos antes deste deploy são re-marcados passe
  a passe a partir dos snapshots comprimidos (mesma pipeline live) antes
  de o daemon retomar — corre no `boot()`.
