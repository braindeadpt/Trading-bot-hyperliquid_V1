# 🔥 VOLUME FIX — Instruções de Instalação Rápida

## Problema
O teu bot estava a rejeitar **100% dos sinais** porque:
- Volume calculado com média teórica 24h/288 (falsa)
- Threshold 4.4x impossível de atingir
- OI bloqueava trades sozinho

## Solução Entregue

### Ficheiros Criados:
1. **`src/volume_fix.py`** — Cálculo de volume com média móvel REAL
2. **`settings_volume_fix.yaml`** — Config atualizada (threshold 2.0x, OI não bloqueante)
3. **`patch_paper_trading.py`** — Instruções para aplicar no teu bot

---

## ⚡ Instalação em 3 Passos

### PASSO 1: Copiar volume_fix.py para o teu projeto

No teu PC Windows, PowerShell:
```powershell
cd C:\Users\Braindead\Documents\trading-bot-hyperliquid
```

Copia o ficheiro `volume_fix.py` para a pasta `src/`.

### PASSO 2: Backup do settings.yaml atual

```powershell
copy config\settings.yaml config\settings.yaml.backup
```

### PASSO 3: Aplicar o patch no paper_trading.py

Abre `src/paper_trading.py` no VS Code/Notepad++ e:

#### 3.1 — Adicionar import no topo:
```python
from src.volume_fix import calculate_volume_metrics_fixed
```

#### 3.2 — Substituir a função onde calcula volume_ratio:

**PROCURA por algo como:**
```python
def _calculate_volume_metrics(self, ...):
    volume_ratio = current_volume / (volume_24h / 288)
    ...
```

**SUBSTITUI por:**
```python
def _calculate_volume_metrics(self, candles=None):
    if candles is None:
        candles = self._get_recent_candles()  # ou método que já tens
    
    result = calculate_volume_metrics_fixed(
        candles=candles,
        ma_period=self.config.get('volume', {}).get('ma_period', 20),
        spike_threshold=self.config.get('volume', {}).get('spike_threshold', 2.0)
    )
    return result
```

#### 3.3 — Modificar onde decide rejeitar:

**PROCURA por algo como:**
```python
if not volume_ok or not oi_ok:
    reject_trade()
```

**SUBSTITUI por:**
```python
vol = self._calculate_volume_metrics(candles)

# Volume é gatekeeper
if vol['status'] == 'INSUFFICIENT_DATA':
    reject_trade("VOLUME: dados insuficientes")
    return

if vol['ratio'] < 1.0:
    reject_trade(f"VOLUME: {vol['ratio']:.1f}x < média")
    return

# OI não bloqueia — só penaliza confiança
confidence = signal['confidence']
if not oi_confirms_direction:
    confidence *= 0.8  # Penaliza 20%

# Funding bloqueia só se extremo (>1%)
if abs(funding) > 0.01:
    reject_trade(f"FUNDING EXTREMO: {funding:.2%}")
    return

# Confiança mínima
if confidence < 0.6:
    reject_trade(f"Confiança baixa: {confidence:.0%}")
    return

# PASSOU TUDO — entra!
enter_trade(direction, confidence=confidence)
```

---

## 📊 Configurações Recomendadas (settings.yaml)

Substitui a secção `volume:` e `open_interest:`:

```yaml
volume:
  calculation_method: "ma_real"
  ma_period: 20          # 20 candles de 15m = 5h
  spike_threshold: 2.0   # 2x acima da média
  delta_threshold: 0.3
  spike_cooldown_seconds: 300

open_interest:
  required: false        # NÃO bloqueia!
  confidence_penalty: 0.2  # Só penaliza 20%
```

---

## 🧪 Testar

Depois de aplicar:
```powershell
python src/paper_trading.py
```

Deves ver:
- ✅ Volume ratio a 2x-8x em vez de 0.1x-1.5x
- ✅ OI a aparecer como "warning" em vez de "oi_insufficient"
- ✅ Sinais a passarem (especialmente em momentos de volatilidade)

---

## 🆘 Precisas de Ajuda?

Se não queres mexer no código manualmente, posso criar um subagente que:
1. Faz o patch automaticamente no teu `paper_trading.py`
2. Gera um diff para veres o que mudou
3. Testa se compila

Só tens de dizer: **"aplica o patch"** e eu trato de tudo! 🔥
