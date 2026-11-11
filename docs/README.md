# docs/ index

One line per file. `ops/` = runbooks, deploy, gates and active
preregistrations in use; `archive/` = closed research reports
kept for the record.

## ops/

- [BACKTEST_WORKFLOW](ops/BACKTEST_WORKFLOW.md) — Backtest Workflow — Decisões baseadas em dados
- [BASELINE_SIGNAL_GATE](ops/BASELINE_SIGNAL_GATE.md) — Baseline-signal gate
- [DATA_ARCHITECTURE](ops/DATA_ARCHITECTURE.md) — Data architecture — operation vs research vs protection
- [GATES_REFERENCE](ops/GATES_REFERENCE.md) — Gate Reference — live vs backtest entry-gate contract
- [INCIDENT_RUNBOOK](ops/INCIDENT_RUNBOOK.md) — Incident Runbook (Operator Guide)
- [L2_BOOK_RECORDING](ops/L2_BOOK_RECORDING.md) — L2 book recording (research) — design & usage
- [LINUX_DEPLOY](ops/LINUX_DEPLOY.md) — Linux Deploy — bots-01 (Oracle A1 arm64) — Fase A packaging
- [LIQUIDATION_AGGREGATOR](ops/LIQUIDATION_AGGREGATOR.md) — Multi-venue liquidation aggregator
- [MAINNET_READINESS](ops/MAINNET_READINESS.md) — Mainnet Readiness Assessment (Fase 10)
- [MARKET_DATA](ops/MARKET_DATA.md) — Market Data Pipeline
- [NODE_TRADES_REBUILD](ops/NODE_TRADES_REBUILD.md) — Node Trades Rebuild — GoldRush Parity Fallback
- [OVERNIGHT_RESEARCH_LOG](ops/OVERNIGHT_RESEARCH_LOG.md) — Overnight research ledger — append-only
- [OVERNIGHT_WEEKLY_DIGEST](ops/OVERNIGHT_WEEKLY_DIGEST.md) — Overnight Weekly Digest
- [PAPER_OOS_90D_PROTOCOL](ops/PAPER_OOS_90D_PROTOCOL.md) — Paper / OOS 90-day evidence protocol
- [PHASE03_MAINNET_READINESS](ops/PHASE03_MAINNET_READINESS.md) — Phase 03 — Mainnet Readiness Report (Exchange Truth + Native SL/TP)
- [PREREGISTER_BOROS_SPREAD](ops/PREREGISTER_BOROS_SPREAD.md) — Pre-registo — BorosLockedSpread (shadow, sem capital)
- [PREREGISTER_CARRY_SHADOW](ops/PREREGISTER_CARRY_SHADOW.md) — PREREGISTER — Carry-Shadow: live L2 evidence for the A1 spot–perp carry
- [PREREGISTER_JEV_OOS_KILL_2026-10-08](ops/PREREGISTER_JEV_OOS_KILL_2026-10-08.md) — PREREGISTER — JevJudge geometry-v2 kill criterion + random control
- [PREREGISTER_UNLOCK_SHADOW](ops/PREREGISTER_UNLOCK_SHADOW.md) — PREREGISTER — UnlockShadow: token-unlock cliff short, forward-registered
- [PREREGISTER_VWAP_IV_GATE_2026-10-08](ops/PREREGISTER_VWAP_IV_GATE_2026-10-08.md) — PREREGISTER — VWAPDeviation `iv_gate_shadow` confirmation sample
- [RESEARCH_BACKLOG](ops/RESEARCH_BACKLOG.md) — Research Backlog
- [RESEARCH_DB_COMPACTION_PLAN](ops/RESEARCH_DB_COMPACTION_PLAN.md) — Research DB compaction — measured plan (2026-10-06)
- [RESEARCH_RETENTION_DESIGN](ops/RESEARCH_RETENTION_DESIGN.md) — Research DB Retention — Design (NOT implemented)
- [RESEARCH_WATCHDOGS](ops/RESEARCH_WATCHDOGS.md) — Research watchdogs — auto-rerun evidence gates (unified supervisor)
- [ROADMAP](ops/ROADMAP.md) — Roadmap — Hyperliquid Premium Bot
- [SECURITY](ops/SECURITY.md) — Hyperliquid Bot Premium — Security Documentation
- [STRATEGY_AUDIT](ops/STRATEGY_AUDIT.md) — Strategy Audit — Backtest Profundo
- [TESTNET_E2E_GUIDE](ops/TESTNET_E2E_GUIDE.md) — Testnet End-to-End Test Suite (Fase 10)
- [TRADING_LITERATURE_SOURCES](ops/TRADING_LITERATURE_SOURCES.md) — Trading literature — curated source map (2026-09-16)

## archive/

- [ATR_PERCENTILE_LONG_REVALIDATION](archive/ATR_PERCENTILE_LONG_REVALIDATION.md) — ATR Percentile Long-History Revalidation
- [AUDIT_2026-08-14](archive/AUDIT_2026-08-14.md) — Auditoria completa — 2026-08-14
- [AUDIT_PERFORMANCE](archive/AUDIT_PERFORMANCE.md) — Auditoria de performance — Hyperliquid Bot v3.1
- [BASELINE_V3_1_47](archive/BASELINE_V3_1_47.md) — BASELINE v3.1.47 — Fase 00 (read-only)
- [CANARY_PROPOSAL](archive/CANARY_PROPOSAL.md) — Canary Configuration Proposal (Fase 10) — PROPOSED, NOT APPLIED
- [CARRY_SPOTPERP_FEASIBILITY](archive/CARRY_SPOTPERP_FEASIBILITY.md) — A1 — Spot–perp funding carry feasibility (delta-neutral, maker)
- [DEPENDENCY_CVE_AUDIT](archive/DEPENDENCY_CVE_AUDIT.md) — Dependency CVE Audit — requirements.txt (2026-08-13)
- [DISK_AND_CODE_INVENTORY](archive/DISK_AND_CODE_INVENTORY.md) — Disk & Code Inventory (cleanup proposal)
- [DVOL_FEED](archive/DVOL_FEED.md) — DVOL feed — Deribit volatility index → research DB
- [ENTRY_DEBOUNCE](archive/ENTRY_DEBOUNCE.md) — Entry Debounce — Live / Backtest Parity
- [FEATURE_SCREENING_24M_CANDLES](archive/FEATURE_SCREENING_24M_CANDLES.md) — Feature Screening — 24m candle-only re-screen
- [FEATURE_SCREENING_24M_STRUCTURE](archive/FEATURE_SCREENING_24M_STRUCTURE.md) — Feature Screening — 24m price STRUCTURE (enlarged FDR family)
- [FEATURE_SCREENING_BASIS_LIQ_VALID](archive/FEATURE_SCREENING_BASIS_LIQ_VALID.md) — Feature Screening — BASIS & LIQUIDATIONS (valid windows only)
- [FEATURE_SCREENING_OI_POSITIONING](archive/FEATURE_SCREENING_OI_POSITIONING.md) — Feature Screening — OI / positioning (Bybit proxy)
- [FEATURE_SCREENING_REPORT](archive/FEATURE_SCREENING_REPORT.md) — Feature Screening Report
- [FEATURE_SCREENING_TAPE_NATIVE](archive/FEATURE_SCREENING_TAPE_NATIVE.md) — Feature Screening — Tape-native CVD / aggression / real OIR
- [FEATURE_SCREENING_TOP_TRADER_BIAS](archive/FEATURE_SCREENING_TOP_TRADER_BIAS.md) — Feature screening probe — top-trader bias (level & delta)
- [FEED_AGE_CREEP_LEADTIME_VALIDATION](archive/FEED_AGE_CREEP_LEADTIME_VALIDATION.md) — Feed Age Creep — lead-time validation
- [FEED_AGE_CREEP_RECHECK_RESULT](archive/FEED_AGE_CREEP_RECHECK_RESULT.md) — Feed Age Creep — recheck
- [FEED_CADENCE_LEADTIME_VALIDATION](archive/FEED_CADENCE_LEADTIME_VALIDATION.md) — Cadence detector — lead-time validation
- [FEED_CADENCE_REPORT](archive/FEED_CADENCE_REPORT.md) — Feed Cadence Report
- [FEED_CONTAMINATION_AUDIT](archive/FEED_CONTAMINATION_AUDIT.md) — Feed Contamination Audit — Binance fstream outage
- [FUNDING_EXTREME_ALTS_FEASIBILITY](archive/FUNDING_EXTREME_ALTS_FEASIBILITY.md) — A2 — Extreme-funding mean reversion in non-major HL perps
- [IV_HIGH_ONLY_AB_SPLIT](archive/IV_HIGH_ONLY_AB_SPLIT.md) — IV 'ambas em high_iv only' — A/B com janelas independentes de 30d
- [IV_PERCENTILE_REGIME_GATE](archive/IV_PERCENTILE_REGIME_GATE.md) — IV-percentile regime gate (Deribit DVOL) — A/B sobre VB + VWAP
- [IV_VS_ADX_DISAGREEMENT](archive/IV_VS_ADX_DISAGREEMENT.md) — IV (DVOL implícito) vs ADX router (realizado) — onde discordam
- [IV_VS_ADX_DISAGREEMENT_OOS_0813](archive/IV_VS_ADX_DISAGREEMENT_OOS_0813.md) — IV (DVOL implícito) vs ADX router (realizado) — prova OUT-OF-SAMPLE 08-07→08-13
- [LIQUIDATION_CATCHER_REAL_BACKTEST](archive/LIQUIDATION_CATCHER_REAL_BACKTEST.md) — LiquidationCatcher — real-feed backtest (08-09+)
- [LIQUIDATION_CATCHER_VARIANTS](archive/LIQUIDATION_CATCHER_VARIANTS.md) — LiquidationCatcher — variantes que quebram o loop entrada→stop-out
- [LIQUIDATION_FLUSH_RECHECK_RESULT](archive/LIQUIDATION_FLUSH_RECHECK_RESULT.md) — Liquidation Flush Recheck — 30-day real-feed comparison
- [LIQUIDATION_FLUSH_SHADOW_LIVE](archive/LIQUIDATION_FLUSH_SHADOW_LIVE.md) — Liquidation Flush Shadow-Live (ETH p90 / hold 30m / fade) — 7-day paper run
- [LIQUIDATION_FLUSH_VARIANTS_EVIDENCE](archive/LIQUIDATION_FLUSH_VARIANTS_EVIDENCE.md) — Variantes do fade ETH p90/30m — evidência (fonte real)
- [LIQUIDATION_MAP_PHASE2_FINDINGS](archive/LIQUIDATION_MAP_PHASE2_FINDINGS.md) — Liquidation Map — Phase 2 Findings (reaction validation)
- [LIQUIDATION_STOPOUT_FLOOR_CALIBRATION](archive/LIQUIDATION_STOPOUT_FLOOR_CALIBRATION.md) — Liquidation stop-out floor — calibration against real multi-venue data
- [LONG_HORIZON_COST_TEST](archive/LONG_HORIZON_COST_TEST.md) — Long-Horizon Directional Cost Test
- [MAKER_FILL_ADVERSE_SELECTION_24M](archive/MAKER_FILL_ADVERSE_SELECTION_24M.md) — Maker Fill + Adverse Selection — 24m candle proxy
- [MAKER_FILL_ADVERSE_SELECTION_L2](archive/MAKER_FILL_ADVERSE_SELECTION_L2.md) — Maker fill + adverse-selection (L2 real) - ret_lag_15m@1h fade
- [MARKET_MAKING_FEASIBILITY](archive/MARKET_MAKING_FEASIBILITY.md) — Market Making Feasibility — Hyperliquid
- [MARKET_MAKING_FEASIBILITY_LIQUIDITY_SPECTRUM](archive/MARKET_MAKING_FEASIBILITY_LIQUIDITY_SPECTRUM.md) — MM Feasibility — Liquidity Spectrum Addendum
- [OI_BACKFILL_VIABILITY](archive/OI_BACKFILL_VIABILITY.md) — OI Backfill Viability — sources & depths
- [PERFORMANCE_REPORT](archive/PERFORMANCE_REPORT.md) — Relatório de Performance & Plano para PnL Positivo
- [PRE_PARITY_BACKTEST_RESULTS](archive/PRE_PARITY_BACKTEST_RESULTS.md) — Pre-Parity Backtest Results Inventory (Fase 05)
- [PREREGISTER_FUNDING_EXTREME_ALTS_2026-10-06](archive/PREREGISTER_FUNDING_EXTREME_ALTS_2026-10-06.md) — PREREGISTER — A2: extreme funding mean-reversion in alts
- [PREREGISTER_SPOTPERP_CARRY_2026-10-06](archive/PREREGISTER_SPOTPERP_CARRY_2026-10-06.md) — PREREGISTER — A1: delta-neutral spot–perp funding carry (maker entry)
- [Q13_PHASE_A_RESULT_2026-10-07](archive/Q13_PHASE_A_RESULT_2026-10-07.md) — Q13 Phase A — wallet markout persistence screen (result)
- [REVERSION_COST_TEST](archive/REVERSION_COST_TEST.md) — Reversion Cost Test
- [TOP_TRADER_BIAS_LAG_ANALYSIS](archive/TOP_TRADER_BIAS_LAG_ANALYSIS.md) — Top-Trader Bias — Lag de Publicação vs Snapshot Real
- [TOP_TRADER_BIAS_RECHECK_RESULT](archive/TOP_TRADER_BIAS_RECHECK_RESULT.md) — Top-Trader Bias Recheck — screening gate at ≥20 dates
- [TOP_TRADER_COLLAPSE_GATE_EVALUATION](archive/TOP_TRADER_COLLAPSE_GATE_EVALUATION.md) — Top-Trader Collapse Test — State Evaluation (2026-08-13)
- [VB_EXPANSION_ONLY_REWORK](archive/VB_EXPANSION_ONLY_REWORK.md) — VB expansion-only rework — A/B split confirma que a W2 deixou de custar
- [VB_FAILED_BREAKOUT_LIQ_CROSSCHECK](archive/VB_FAILED_BREAKOUT_LIQ_CROSSCHECK.md) — Failed_breakout do VB × liquidações — o cruzamento literal é impossível, o proxy mostra reversão fraca mas o r
- [VB_GAP_0524_0625_ROOT_CAUSE](archive/VB_GAP_0524_0625_ROOT_CAUSE.md) — VB não tradeou entre 05-24 e 06-25 — causa raiz: gate de funding stale no replay
- [VB_LONG_FOLLOWTHROUGH_VARIANT](archive/VB_LONG_FOLLOWTHROUGH_VARIANT.md) — VB long-only + follow-through — mesma amostra forense, nova simulação
- [VB_SHORTS_LIQ_CROSSCHECK](archive/VB_SHORTS_LIQ_CROSSCHECK.md) — VB shorts × liquidações — a hipótese "o flush reverte" refina-se
- [VB_STOPOUT_AB](archive/VB_STOPOUT_AB.md) — VB stop-out A/B — impacto do stop-out por liquidação calibrado no sangramento dos shorts
- [WALLET_FILLS_COVERAGE_2026-10-04](archive/WALLET_FILLS_COVERAGE_2026-10-04.md) — top_trader_fills — per-wallet coverage, snapshot 2026-10-04
- [XS_MOMENTUM_FEASIBILITY](archive/XS_MOMENTUM_FEASIBILITY.md) — Cross-Sectional Slow Momentum Feasibility
