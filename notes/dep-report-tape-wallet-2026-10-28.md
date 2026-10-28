# Relatório de dependências — `trade_tape` e `top_trader_fills`
Data: 2026-10-28 · branch `feat/post-cutover` · config_hash `4077927fec6a880c` (inalterado)

## `trade_tape` (research DB `hyperliquid.db`)

### Writers
| origem | estado |
|---|---|
| `src/data/research_microstructure.py` (WS raw tap, `save_trade_tape`) | **OFF desde 2026-10-07** — `research.ws_microstructure_enabled: false`; o hash corrente já reflecte isto (`reregister_phase10_tape_off.py`) |
| `src/data/research_sampler.py` (REST poll, `save_trade_tape`) | **OFF** — `research.rest_sampling_enabled: false` ("NOT Tier A; do not enable") |
| `src/data/hl_research_backfill.py` (backfill manual) | ferramenta ops on-demand; não é colector contínuo |

### Readers
| origem | tipo |
|---|---|
| `archive/research_closed/feature_screening_tape_native.py` | research (movido p/ archive — família fechada) |
| `archive/research_closed/mm_feasibility_study.py` | research (movido — MM fechado) |
| `src/backtest/data_contract.py`, `src/backtest/strategy_feed_requirements.py` | só checks de disponibilidade para backtest — **nenhum consumo por estratégia viva** |
| `scripts/ops/backup_research_data.py` | manifesto de backup (lista a tabela) |
| `src/data/research_database.py::count_trade_tape_in_window` | helper dos checks acima |

**Conclusão:** `trade_tape` já está desligada na prática (0 writers activos); serve apenas research de famílias fechadas (CVDOrderFlow removida 2026-10-08). Tabela **não apagada** (regra do pacote). Retenção: **configurável, default OFF** — `ResearchDatabase.prune_trade_tape(days)` + `scripts/ops/prune_trade_tape.py` (recusa sem `--days`/policy file `data/research/trade_tape_retention.json`). Decisão 30–60d fica para o owner. Sem flag em `settings.yaml` — a secção `research` é hashed; um toggle em YAML quebraria o hash congelado.

## `top_trader_fills` (research DB `hyperliquid.db`)

### Writers
| origem | estado |
|---|---|
| `scripts/research/wallet_fills_collector.py` → `archive/research_closed/` | era job pm2/timer **no Mac** (`deploy/macos/run_wallet_fills.sh` → `archive/deploy_macos/`). **Nunca existiu no compose do bots-01** — "desligar o colector no Linux" = já está desligado; flag = não o adicionar |

### Readers
| origem | tipo |
|---|---|
| `archive/research_closed/wallet_markout_screen.py` | research (movido — família TopTraderFlow **removida 2026-10-08**, PF 0.40) |

Runtime: **nenhum**. Nota: a cadeia `top_trader_tracker → top_trader_store/virtual_book/panel` continua wired (`enabled: true` em config hashed; engine + outcome-eval + dashboard importam-na) — **não movida**, ver §pendências.

## Movido para `archive/` (famílias fechadas)

**`archive/strategies_closed/`** (classes fora do registry desde 2026-09-10):
`mean_reversion` (VWAPDeviation reversion), `donchian_breakout` (Donchian 4H), `range_grid` (DipGrid), `funding_arbitrage` (A2 funding-extreme), `sfp_reversion` + `va_rejection` (S/R + pivots / rejection family).

**`archive/research_closed/`** (scripts folha, zero importers vivos):
MM (`mm_feasibility_study`, `mm_feasibility_liquidity_spectrum`), candle features (`feature_screening`, `_24m_candles`, `_24m_structure`, `_basis_liq_valid`, `_l2_depth`, `_oi_positioning`, `_tape_native`), XS momentum antigo (`xs_momentum_feasibility`), A2 (`funding_extreme_alts_feasibility`), TopTraderFlow (`wallet_fills_collector`, `wallet_markout_screen`, `top_wallet_liquidation_report`), S/R+pivots research (`backtest_sfp_walkforward`, `backtest_va_walkforward`, `reversion_cost_test`).

**`archive/deploy_macos/`**: `run_wallet_fills.sh`.

## NÃO movido — wired ao runtime (flag para o owner decidir)

| peça | dependência viva |
|---|---|
| `scripts/research/feature_screening_top_trader_bias.py` | invocada por path em `top_trader_bias_recheck.py` |
| `scripts/research/top_trader_bias_recheck.py` | importada pelo watchdog supervisor + `research_watchdog_status` (serviço `watchdogs` no compose) |
| `scripts/research/toptrader_fade_replay.py` | import lazy em `overnight_runner.py` (serviço `overnight`) |
| `src/exchanges/top_trader_tracker.py`, `src/research/top_trader_{store,virtual_book,panel}.py` | engine (`enabled: true`, hashed), shadow_outcome_evaluator, dashboard |

→ Estas servem só a família TopTraderFlow fechada + histórico do outcome-eval. Desligar exige: flip de `top_trader_tracker.enabled` (**muda config_hash** — decisão do owner pós-cutover), remover o binding `top_trader_bias` do watchdog, e remover a família do overnight_runner. **Flag: candidatas a desligar no Linux**, tables intactas.

## Não encontrados no repo
- **Pairs 4H** e **Kronos** — sem ficheiros dedicados (prováveis aliases de famílias já retiradas ou experimentos externos ao repo). Nada a mover.

## Não tocado (regra explícita)
`jev_shadow_judge`, `vwap_deviation` (iv-gate shadow vivo), `carry-shadow`, `unlock-shadow`, `boros-shadow`, `outcome-eval` + seus módulos, `lead_lag`/`funding_momentum`/`spot_perp_carry`/`volatility_breakout`/`liquidation_catcher`/`vwap_deviation`/`jev_judge` (registry vivo), `trend_follow`, `trend_pyramid`, `vwap_trend`, `opening_range_breakout`, `cvd_orderflow*` (retired mas fora da lista do pacote).
