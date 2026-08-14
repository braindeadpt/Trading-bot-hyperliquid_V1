# Feed Age Creep — recheck

Detector do **max age diário por feed contratado** (escada não-decrescente sobre o rollup `feed_age_history`).

- Feeds com creep ativo: **0**
- Janela: últimos 14d · mínimo 5d consecutivos · crescimento ≥ 15% do threshold

| Feed | Dias | 1º max (s) | Último max (s) | Cresc. (s) | Cresc. (% thr) |
|---|---|---|---|---|---|

_Sem feeds com creep — todos os maxes diários estáveis._

_Gerado por `scripts/feed_age_creep_recheck.py` — read-only, nunca trade._