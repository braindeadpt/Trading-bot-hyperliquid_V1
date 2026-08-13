# Top-Trader Bias — Lag de Publicação vs Snapshot Real

Generated: 2026-08-13. Questão: **"o bias de hoje reflete posições de quando?"**
— para calibrar a janela do sinal de mean-reversion a 24h.

## Resposta curta

**O bias que o probe usa reflete posições de há ~1–2 minutos, não de horas.**
O lag de publicação do leaderboard é **irrelevante para o valor do bias** —
o bias vem do `clearinghouseState` (estado real das wallets na exchange),
não do leaderboard publicado. O leaderboard só decide **quem** é seguido.

## Arquitetura do feed (porque é que isto é assim)

```
stats-data.hyperliquid.xyz/Mainnet/leaderboard   (snapshot estático, sem timestamps por row)
        │  refresh 24h — só para escolher AS 10 WALLETS (top_traders.json)
        ▼
TopTraderTracker.poll_once()
        │  clearinghouseState(address) por wallet — estado REAL ao vivo
        │  poll_interval_sec = 60   (config: top_trader_tracker.poll_interval_sec)
        ▼
top_trader_bias_samples (research DB)
        │  timestamp_ms = now_ms do poll (local)
        │  ingested_at_ms = now_ms do persist
        ▼
feature_screening_top_trader_bias.py (probe do IC a 24h)
```

## Lag de medição (empírico, DB real)

| componente | valor | fonte |
|---|---|---|
| Poll interval | **65 s** (p50; p95 67 s; max 508 s) | gaps entre `timestamp_ms` consecutivos (BTC, n=2043) |
| Lag de ingestão (`ingested_at − timestamp_ms`) | **~5 s** (p50 4.9 s, p95 5.9 s) | 5000 rows |
| Latência rede ao `clearinghouseState` | **~0.4 s** (server `time` field vs clock local) | probe ao vivo |

**Confirmação direta:** recomputei o bias AGORA (clearinghouseState das 10
wallets) vs a última amostra do DB → **Δnet = 0.000 para BTC/ETH/HYPE**,
notionals idênticos até ao dólar. O snapshot persistido é o estado atual.

## Lag de publicação do leaderboard (irrelevante para o bias, mas medido)

O leaderboard publicado é um **snapshot estático**:
- 0 de 41 629 rows mudou entre dois fetches a 45 s de distância.
- Ao longo de 6 min (3 fetches): `accountValue` publicado **inalterado** para
  todas as 10 wallets, enquanto o real muda a cada poll.
- Divergência do `accountValue` publicado vs real: mediana **−88.7%**
  (ex1: publicado $76.3M vs real $0.65M; ex5: $5.79M vs $5.78M ≈ atual).
  → snapshot de dias/semanas para a maioria; não tem timestamp por row.

Consequência: se alguém construísse o bias a partir do leaderboard publicado,
teria um lag de **dias** não quantificável. O pipeline atual **não faz isso** —
usa `clearinghouseState`, logo o lag é o de medição (~1–2 min).

## O que realmente importa para a janela de 24h (persistência do sinal)

O lag de publicação **não é o problema** — a persistência é. Net bias por hora:

| símbolo | comportamento | autocorr. lag-1 (65 s) |
|---|---|---|
| BTC | +0.41 → +0.56 em 40 h, deriva suave, sem flips | **0.971** |
| ETH | +0.73 até 08-12 14:00 → **−1.00 desde então** (flip discreto) | — |
| HYPE | −0.54 até 08-12 14:00 → **−1.00 desde então** (flip discreto) | — |
| SOL | 6 h de amostra, depois **parou** (n_wallets < 3) | — |

- ETH/HYPE congelados em −1.000 por **24+ h**: os shorts não mudaram de
  tamanho; o "bias de hoje" é bit-a-bit o de ontem.
- BTC flips são raros; quando um flip acontece é discreto e completo
  (a wallet saiu do símbolo ou inverteu a posição toda).
- **SOL parou de ser persistido em 08-11 19:08** — não por lag, mas pelo gate
  `min_publish_wallets = 3` (só 2 wallets tinham posição). O probe a 24h
  tinha só 6 h de amostra SOL.

## Implicações para calibrar a janela do sinal a 24h

1. **Não há lag de publicação a compensar**: o bias é quasi-tempo-real
   (~1–2 min). A janela de 24h pode usar o bias do momento sem desconto.
2. **O caveat real é a variância do sinal, não o lag**: com autocorrelação
   ~0.97 e flips discretos, um sinal "de hoje a 24h" mede quase o mesmo valor
   de ontem — a janela de 24h adiciona pouca informação nova face a uma
   janela de 1–6 h. Isto explica em parte o IC fraco/borderline do probe
   (p_NW 0.059–0.137): o regressor quase não varia dentro da janela.
3. **Se o objetivo é capturar a mudança** (mean-reversion do *delta* de
   bias), o sinal útil está nos **flips discretos** (ETH/HYPE em 08-12 14:00)
   e nas derivas rápidas do BTC (08-12 04:00→09:00: +0.62 → +0.99 em 5 h) —
   janelas curtas (1–6 h) capturam-nos; a janela de 24h alisa-os.
4. **Recomendação operacional**: manter a janela de 24h só se o sinal for o
   *nível* (persistente); usar 1–6 h se o sinal for o *delta* (flips). O
   probe atual mistura os dois (feature `tt_bias_level` + horizonte 24h).

## Como re-medir quando a amostra crescer

O script do probe é idempotente e regenera o relatório sozinho; re-correr
quando `top_trader_bias_samples` cobrir ≥20 datas (~3 semanas de polling)
mantém o gate n>=20 do bootstrap intacto.
