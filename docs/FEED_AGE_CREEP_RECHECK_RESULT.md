# Feed Age Creep — recheck

Detector do **max age diário por feed contratado** (escada não-decrescente sobre o rollup `feed_age_history`).

- Feeds com creep ativo: **1**
- Janela: últimos 14d · mínimo 5d consecutivos · crescimento ≥ 15% do threshold

| Feed | Dias | 1º max (s) | Último max (s) | Cresc. (s) | Cresc. (% thr) |
|---|---|---|---|---|---|
| `taker_split` | 5 | 50.0 | 1323.0 | 1273.0 | 35% |


_Gerado por `scripts/research/feed_age_creep_recheck.py` — read-only, nunca trade._