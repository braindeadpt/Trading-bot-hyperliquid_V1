# Kronos — Review & Security Audit

**Repo:** https://github.com/shiyu-coder/Kronos  
**Commit:** `HEAD` (shallow clone, May 2026)  
**Author:** shiyu-coder / NeoQuasar  
**License:** Apache 2.0 (per README)

---

## 1. O que é o Kronos?

Kronos é um **modelo fundacional** (foundation model) para previsão de séries temporais financeiras — especificamente **K-lines / candlesticks OHLCV** (Open-High-Low-Close-Volume).

### Arquitetura de alto nível

```
Input:  OHLCV time-series (t-N ... t-1)  →  Tokenizer  →  Compressed tokens
                                                              ↓
                                                          Predictor (Transformer)
                                                              ↓
Output: OHLCV predictions (t ... t+M)  ←  Detokenizer  ←  Latent sequence
```

| Componente | O que faz | Paper base |
|---|---|---|
| **KronosTokenizer** | Comprime OHLCV em tokens discretos via **Binary Spherical Quantization** | TiTok (Tokenize by Compression, 2024) |
| **KronosPredictor** | Modelo Transformer autoregressivo que prediz próximos tokens | GPT-style decoder |
| **BSQuantizer** | Quantização diferenciável que mapeia floats → códigos binários esféricos | BSQ (arXiv:2406.07548) |

### Tamanhos disponíveis

| Modelo | Parâmetros | Contexto | Uso |
|---|---|---|---|
| Kronos-mini | 4.1M | 2048 tokens | Protótipos rápidos |
| Kronos-small | 24.7M | 512 tokens | Equilíbrio |
| Kronos-base | 102.3M | 512 tokens | Qualidade máxima |

### Input / Output

- **Input:** `lookback` candles (default 400) → `[batch, seq_len, 4 or 5]` (OHLC ou OHLCV)
- **Output:** `pred_len` candles (default 120) → mesma dimensão
- **Temperatura / top-p:** sampling parameters para controlar criatividade (1.0 / 0.9 default)

---

## 2. Arquitetura de código

```
Kronos/
├── model/
│   ├── kronos.py          # KronosTokenizer + KronosPredictor classes
│   ├── module.py          # BSQuantizer, TransformerBlock, attention
│   └── __init__.py        # Exports
├── examples/
│   ├── prediction_example.py        # CLI demo básico
│   ├── prediction_new.py            # CLI avançado com batch
│   ├── prediction_new_GUI.py        # CLI + gráficos
│   ├── get_date_new.py              # Data fetching (requests, subprocess)
│   ├── run_backtest_kronos.py       # Backtest simples
│   └── yuce/                        # Scripts chineses (historical_backtest)
├── webui/
│   ├── app.py             # Flask web server (CORS habilitado)
│   └── templates/         # HTML frontend
├── finetune/
│   ├── train_tokenizer.py # Fine-tune tokenizer em dataset custom
│   ├── train_predictor.py # Fine-tune predictor
│   ├── qlib_data_preprocess.py # Preprocessamento Qlib (pickle)
│   └── qlib_test.py       # Testes Qlib
├── finetune_csv/
│   ├── finetune_base_model.py    # Pipeline completo CSV
│   ├── finetune_tokenizer.py     # Tokenizer-only fine-tuning
│   ├── train_sequential.py       # Training sequencial
│   └── config_loader.py          # YAML config parser
└── tests/                 # Regression tests
```

---

## 3. Security Audit

### 🔴 HIGH — Deserialization arbitrária (pickle)

**Ficheiros afetados:**
- `finetune/qlib_data_preprocess.py:115` — `pickle.dump()`
- `finetune/qlib_test.py:338,353` — `pickle.load()`
- `dataset.py:42` — `pickle.load()`

**Risco:** `pickle.load()` executa código arbitrário durante deserialization. Se alguém substituir um `.pkl` por um payload malicioso, o modelo é comprometido.

**Mitigação recomendada:**
```python
# Em vez de pickle.load(f):
import joblib  # ou torch.save/torch.load com weights_only=True
joblib.load(f)
```

### 🟡 MEDIUM — Path Traversal na WebUI

**Ficheiro:** `webui/app.py`

```python
# /api/load-data (POST)
file_path = data.get('file_path')  # <- user-controlled!
df, error = load_data_file(file_path)
```

**Risco:** Um atacante pode enviar `file_path: "../../../etc/passwd"` e ler ficheiros do sistema.

**Mitigação recomendada:**
```python
from pathlib import Path
base_dir = Path(os.path.dirname(__file__)).parent / "data"
requested = Path(file_path).resolve()
if not str(requested).startswith(str(base_dir)):
    return jsonify({'error': 'Path not allowed'}), 403
```

### 🟡 MEDIUM — CORS aberto no Flask

**Ficheiro:** `webui/app.py:14`

```python
from flask_cors import CORS
CORS(app)  # <- permite requests de QUALQUER origem
```

**Risco:** Se a web UI corre numa rede pública, qualquer website pode fazer requests à API.

**Mitigação recomendada:**
```python
CORS(app, origins=["http://localhost:5000", "http://127.0.0.1:5000"])
```

### 🟡 MEDIUM — Auto-download de modelos (HuggingFace)

**Ficheiros:** Vários `from_pretrained()` calls

```python
tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
model = Kronos.from_pretrained("NeoQuasar/Kronos-base")
```

**Risco:**
1. **Supply chain:** Se a conta HuggingFace `NeoQuasar` for comprometida, o modelo malicioso é descarregado automaticamente.
2. **Man-in-the-middle:** Sem verificação de checksums, um atacante na rede pode injetar um modelo alterado.

**Mitigação recomendada:**
- Fixar `revision` (commit hash) em produção:
```python
model = Kronos.from_pretrained("NeoQuasar/Kronos-base", revision="abc123...")
```
- Verificar checksum SHA256 após download.

### 🟢 LOW — `subprocess.check_call(["pip", "install", ...])`

**Ficheiros:** `get_date_new.py`, `get_akshare_date_2024-2025_x.py`, `run.py`

```python
subprocess.check_call(["pip", "install", "requests", "numpy", "pandas"])
```

**Risco:** Baixo — apenas instala packages conhecidos. Mas pode quebrar ambientes virtualizados.

**Mitigação:** Usar `requirements.txt` + `pip install -r requirements.txt` em vez de runtime install.

### 🟢 LOW — `input()` em run.py

**Ficheiro:** `run.py:46`

```python
if input().lower() == 'y':
```

**Risco:** Bloqueia execução em ambientes headless (CI, Docker, servidores).

---

## 4. Qualidade de código

### Pontos positivos ✅

| Aspecto | Avaliação |
|---|---|
| Documentação | README com fórmulas matemáticas, arquitetura, tamanhos de modelo |
| Modularidade | Separação clara tokenizer / predictor / quantizer |
| Fine-tuning | Suporte completo: tokenizer-only, predictor-only, end-to-end |
| Web UI | Flask + Plotly para visualização interativa |
| Tests | Regression tests com expected outputs |
| HuggingFace | Integração nativa via `PyTorchModelHubMixin` |

### Pontos a melhorar ⚠️

| Aspecto | Problema | Severidade |
|---|---|---|
| Error handling | Muitos `try/except` genéricos que mascaram erros | Média |
| Type hints | Poucos annotations; dificulta manutenção | Baixa |
| Logging | Print statements em vez de logging module | Baixa |
| Config | Dicionários aninhados vs. dataclasses/attrs | Baixa |
| Caching | Sem cache de modelos/tokenizers carregados | Média |
| Rate limiting | Web UI sem rate limiting nos endpoints | Média |

---

## 5. Veredicto

### Para uso pessoal / research ✅ Recomendado

Kronos é um projeto **impressionante** academicamente:
- Arquitetura bem fundamentada (TiTok + Transformer)
- Paper de qualidade (publicado)
- Datasets abertos (120 mercados)
- Fine-tuning pipeline completa

### Para produção / trading ao vivo ⚠️ Cuidado

| Risco | Impacto |
|---|---|
| Modelo preditivo ≠ sistema de trading | Previsões são *estimativas*, não sinais de trade |
| Overfitting em dados históricos | Modelo pode não generalizar para regimes de mercado novos |
| Latência | Predição de 120 candles futuros não é útil para HFT |
| Supply chain (HF Hub) | Modelo pode ser alterado sem aviso |
| Pickle deserialization | Vulnerabilidade de segurança real |

---

## 6. Recomendações para integrar no teu bot

Se quiseres usar Kronos como **input adicional** (não como sinal primário):

```python
# src/strategies/kronos_input.py
"""Kronos forecast as auxiliary signal input.

Uses Kronos predictions as additional context for other strategies.
NEVER trades directly on Kronos output — it's a forecast, not a signal.
"""

class KronosInput:
    def __init__(self, model_path="NeoQuasar/Kronos-mini"):
        self.tokenizer = KronosTokenizer.from_pretrained(model_path)
        self.model = Kronos.from_pretrained(model_path)
        
    def get_forecast_bias(self, df_ohlcv: pd.DataFrame) -> float:
        """Return directional bias [-1, 1] from Kronos forecast.
        
        > 0 = model predicts upward movement
        < 0 = model predicts downward movement
        """
        pred = self.model.predict(df_ohlcv)
        # Compare predicted close vs current close
        return (pred['close'].iloc[-1] - df_ohlcv['close'].iloc[-1]) / df_ohlcv['close'].iloc[-1]
```

### Regras de integração segura:

1. **NUNCA** fazer trade direto no output do Kronos
2. Usar como **input adicional** para strategies existentes (ex: +0.1 confidence se Kronos confirma)
3. **Pin revision** do modelo no HuggingFace
4. **Desativar** auto-download em produção (carregar de cache local)
5. **Validar** checksum SHA256 do modelo antes de carregar

---

## 7. Scorecard

| Categoria | Score | Notas |
|---|---|---|
| Arquitetura | ⭐⭐⭐⭐⭐ | Muito bem desenhada, paper-backed |
| Documentação | ⭐⭐⭐⭐☆ | Bom README, faltam docstrings |
| Segurança | ⭐⭐☆☆☆ | Pickle, CORS aberto, path traversal |
| Testes | ⭐⭐⭐☆☆ | Regression tests, mas pouca cobertura |
| Manutenibilidade | ⭐⭐⭐☆☆ | Faltam type hints, configs espalhadas |
| Produção-ready | ⭐⭐☆☆☆ | Não pronto para deploy público |

**Overall: 7/10** — Excelente projeto académico/pesquisa. Requer hardening significativo para produção.

---

*Audit realizado em 2026-05-07. O código foi analisado via shallow clone do repositório GitHub.*
