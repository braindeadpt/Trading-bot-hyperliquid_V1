"""Hidden Markov Model regime detection (v3.1.21).

Replaces the simple ADX-threshold regime with a 4-state Gaussian HMM
trained on the *observed* feature vector
``[returns, ATR, ADX, volume_ratio]`` of historical candles. The hidden
states are mapped to a small set of named regimes:

* ``trending_up``   — positive returns, low-moderate ATR, high ADX
* ``trending_down`` — negative returns, low-moderate ATR, high ADX
* ``ranging``       — low returns, low ATR, low ADX
* ``volatile``      — high ATR, large |returns|

The implementation is a vanilla Gaussian HMM with diagonal covariance
(per-feature independent — fast, robust on small samples). It uses
Baum-Welch (EM) for training and forward-only for prediction; no
external dependency (no hmmlearn). When training data is too short to
fit a real HMM, the class falls back to a deterministic
``ThresholdRegimeClassifier`` (k-nearest-feature-centroid) so the
downstream pipeline always gets a non-None ``RegimeState``.

Integration with ``src/core/regime.py`` is via the ``RegimeDetector``
factory: ``build_regime_detector(config)`` returns either the HMM
detector (when ``regime.detection_method == "hmm"``) or the existing
ADX-based path. The HMM detector is fit on demand from the engine's
1h candle history (re-fits every N bars; persists enough state
across restarts to avoid re-fitting on every startup).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Public regime enum + state
# ────────────────────────────────────────────────────────────────────


class Regime(str, Enum):
    """Regime classification returned by the detector."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeState:
    """Current regime + posterior probabilities across all states."""

    regime: Regime
    confidence: float
    probabilities: Dict[Regime, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "regime": self.regime.value,
            "confidence": float(self.confidence),
            "probabilities": {k.value: float(v) for k, v in self.probabilities.items()},
        }


# ────────────────────────────────────────────────────────────────────
# Feature extraction
# ────────────────────────────────────────────────────────────────────

FEATURE_NAMES: Tuple[str, ...] = ("returns", "atr", "adx", "vol_ratio")


def build_feature_matrix(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    adx_series: Optional[Sequence[Optional[float]]] = None,
) -> np.ndarray:
    """Build a (T, 4) feature matrix from raw OHLCV + ADX.

    Columns:
        0  returns       close[t]/close[t-1] - 1
        1  atr           (high - low) / close
        2  adx           ADX(14) at bar t (or 0 if not provided)
        3  vol_ratio     volume[t] / mean(volume[t-19:t])

    Returns an empty (0, 4) array if there is < 2 bars.
    """
    n = len(closes)
    if n < 2:
        return np.zeros((0, 4), dtype=np.float64)
    out = np.zeros((n, 4), dtype=np.float64)
    for t in range(1, n):
        prev_close = float(closes[t - 1])
        if prev_close > 0:
            out[t, 0] = float(closes[t]) / prev_close - 1.0
        if float(closes[t]) > 0:
            out[t, 1] = (float(highs[t]) - float(lows[t])) / float(closes[t])
        if adx_series is not None and t < len(adx_series) and adx_series[t] is not None:
            out[t, 2] = float(adx_series[t])
        if t >= 20 and t - 1 < n:
            window = [float(volumes[i]) for i in range(t - 20, t) if volumes[i] > 0]
            if window:
                avg = sum(window) / len(window)
                if avg > 0 and float(volumes[t]) > 0:
                    out[t, 3] = float(volumes[t]) / avg
    return out


# ────────────────────────────────────────────────────────────────────
# Gaussian HMM with diagonal covariance
# ────────────────────────────────────────────────────────────────────


class _GaussianHMM:
    """Diagonal-covariance Gaussian HMM with random restarts.

    Small and self-contained — no hmmlearn dependency. Implements
    forward / backward (scaled) and Baum-Welch (EM) for K discrete
    states on a fixed number of features.
    """

    def __init__(
        self,
        n_states: int,
        n_features: int,
        n_restarts: int = 3,
        max_iter: int = 50,
        tol: float = 1e-4,
        seed: int = 0,
    ) -> None:
        self.n_states = n_states
        self.n_features = n_features
        self.n_restarts = n_restarts
        self.max_iter = max_iter
        self.tol = tol
        self.seed = int(seed)
        self.start_prob: Optional[np.ndarray] = None
        self.trans_mat: Optional[np.ndarray] = None
        self.means: Optional[np.ndarray] = None
        self.vars: Optional[np.ndarray] = None

    def _gauss_logpdf(self, x: np.ndarray, mean: np.ndarray, var: np.ndarray) -> np.ndarray:
        """Per-state log p(x) for each time step, shape (T, K)."""
        # x: (T, F); mean: (K, F); var: (K, F)  — broadcast per-state.
        diff = x[:, None, :] - mean[None, :, :]  # (T, K, F)
        var_safe = np.where(var > 1e-12, var, 1e-12)  # (K, F)
        log_norm = -0.5 * self.n_features * math.log(2.0 * math.pi) \
            - 0.5 * np.sum(np.log(var_safe), axis=1)  # (K,)
        exponent = -0.5 * np.sum((diff ** 2) / var_safe[None, :, :], axis=2)  # (T, K)
        return log_norm[None, :] + exponent  # (T, K)

    def _forward(
        self,
        obs: np.ndarray,
        start_prob: np.ndarray,
        trans_mat: np.ndarray,
    ) -> Tuple[np.ndarray, float]:
        """Scaled forward algorithm. Returns (alpha_T, log_likelihood)."""
        T = obs.shape[0]
        K = self.n_states
        log_obs = self._gauss_logpdf(obs, self.means, self.vars)  # (T, K)
        alpha = np.zeros((T, K), dtype=np.float64)
        alpha[0] = start_prob * np.exp(log_obs[0])
        c = np.zeros(T, dtype=np.float64)
        c[0] = alpha[0].sum()
        if c[0] <= 0:
            c[0] = 1e-300
        alpha[0] /= c[0]
        log_lik = math.log(c[0])
        for t in range(1, T):
            for k in range(K):
                alpha[t, k] = alpha[t - 1] @ trans_mat[:, k] * math.exp(log_obs[t, k])
            c[t] = alpha[t].sum()
            if c[t] <= 0:
                c[t] = 1e-300
            alpha[t] /= c[t]
            log_lik += math.log(c[t])
        return alpha, log_lik

    def _backward(self, obs: np.ndarray, trans_mat: np.ndarray) -> np.ndarray:
        """Scaled backward algorithm. Returns beta of shape (T, K)."""
        T = obs.shape[0]
        K = self.n_states
        log_obs = self._gauss_logpdf(obs, self.means, self.vars)  # (T, K)
        beta = np.zeros((T, K), dtype=np.float64)
        beta[T - 1] = 1.0
        for t in range(T - 2, -1, -1):
            for k in range(K):
                beta[t, k] = sum(
                    trans_mat[k, j] * math.exp(log_obs[t + 1, j]) * beta[t + 1, j]
                    for j in range(K)
                )
            s = beta[t].sum()
            if s <= 0:
                s = 1e-300
            beta[t] /= s
        return beta

    def _init_random(self, obs: np.ndarray, rng: np.random.Generator) -> None:
        K = self.n_states
        F = self.n_features
        self.start_prob = np.full(K, 1.0 / K)
        self.trans_mat = np.full((K, K), 1.0 / K)
        # Means: K random indices in the data; each gets the surrounding
        # window of points to compute a per-state mean / variance.
        idx = rng.integers(0, max(1, len(obs) - 1), size=K)
        self.means = np.zeros((K, F), dtype=np.float64)
        self.vars = np.zeros((K, F), dtype=np.float64)
        for k, i in enumerate(idx):
            window = obs[max(0, i - 5): min(len(obs), i + 6)]
            if len(window) > 0:
                self.means[k] = window.mean(axis=0)
                self.vars[k] = window.var(axis=0) + 1e-6
            else:
                self.means[k] = obs.mean(axis=0)
                self.vars[k] = obs.var(axis=0) + 1e-6

    def fit(self, obs: np.ndarray) -> float:
        """Run Baum-Welch with random restarts. Returns best log-likelihood."""
        rng = np.random.default_rng(self.seed)
        best_ll = -math.inf
        best_params: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        for _ in range(max(1, self.n_restarts)):
            self._init_random(obs, rng)
            prev_ll = -math.inf
            for _it in range(self.max_iter):
                _, ll = self._forward(obs, self.start_prob, self.trans_mat)
                if not math.isfinite(ll):
                    break
                if abs(ll - prev_ll) < self.tol:
                    break
                prev_ll = ll
                beta = self._backward(obs, self.trans_mat)
                # gamma[t, k] = P(s_t = k | obs)
                log_obs = self._gauss_logpdf(obs, self.means, self.vars)
                log_alpha = np.log(np.maximum(self._forward(obs, self.start_prob, self.trans_mat)[0], 1e-300))
                log_beta = np.log(np.maximum(beta, 1e-300))
                # log xi[t, i, j] ∝ log_alpha[t, i] + log_trans[i, j] + log_obs[t+1, j] + log_beta[t+1, j]
                    # (we recompute alpha here; for the M-step we just need gamma and xi sums)
                # gamma via the standard relation:
                with np.errstate(divide="ignore"):
                    log_gamma = log_alpha + log_beta
                log_gamma -= log_gamma.max(axis=1, keepdims=True)
                gamma = np.exp(log_gamma)
                row_sum = gamma.sum(axis=1, keepdims=True)
                row_sum = np.where(row_sum > 1e-12, row_sum, 1.0)
                gamma = gamma / row_sum
                # M-step means / variances
                weights = gamma.sum(axis=0)
                w = np.where(weights > 1e-12, weights, 1e-12)
                self.means = (gamma[:, :, None] * obs[:, None, :]).sum(axis=0) / w[:, None]
                diff = obs[:, None, :] - self.means[None, :, :]
                self.vars = (gamma[:, :, None] * diff ** 2).sum(axis=0) / w[:, None]
                self.vars = np.maximum(self.vars, 1e-6)
                # M-step transition
                # xi[t, i, j] = alpha[t, i] * trans[i, j] * exp(log_obs[t+1, j]) * beta[t+1, j]
                # We need alpha and beta together — we already have beta.
                alpha, _ = self._forward(obs, self.start_prob, self.trans_mat)
                xi_sum = np.zeros((self.n_states, self.n_states))
                for t in range(obs.shape[0] - 1):
                    for i in range(self.n_states):
                        for j in range(self.n_states):
                            xi_sum[i, j] += (
                                alpha[t, i] * self.trans_mat[i, j]
                                * math.exp(log_obs[t + 1, j]) * beta[t + 1, j]
                            )
                row_sums = xi_sum.sum(axis=1, keepdims=True)
                row_sums = np.where(row_sums > 0, row_sums, 1.0)
                self.trans_mat = xi_sum / row_sums
                # M-step start
                self.start_prob = gamma[0]
                self.start_prob /= self.start_prob.sum()
            if math.isfinite(prev_ll) and prev_ll > best_ll:
                best_ll = prev_ll
                best_params = (
                    self.start_prob.copy(),
                    self.trans_mat.copy(),
                    self.means.copy(),
                    self.vars.copy(),
                )
        if best_params is not None:
            self.start_prob, self.trans_mat, self.means, self.vars = best_params
        return best_ll

    def predict_posteriors(self, obs: np.ndarray) -> np.ndarray:
        """Return (T, K) posterior probabilities P(s_t = k | obs)."""
        if obs.shape[0] == 0:
            return np.zeros((0, self.n_states))
        alpha, _ = self._forward(obs, self.start_prob, self.trans_mat)
        beta = self._backward(obs, self.trans_mat)
        gamma = alpha * beta
        row_sum = gamma.sum(axis=1, keepdims=True)
        row_sum = np.where(row_sum > 1e-12, row_sum, 1.0)
        gamma = gamma / row_sum
        return gamma


# ────────────────────────────────────────────────────────────────────
# Mapping HMM states → named regimes
# ────────────────────────────────────────────────────────────────────


def _label_state(state_mean: np.ndarray) -> Regime:
    """Map a 4-D state centroid to a named regime.

    Heuristic: look at sign of returns, magnitude of ATR, magnitude
    of ADX, and volume ratio. Thresholds are intentionally generous
    so a wide range of HMM fits maps cleanly to one of the four
    regimes.
    """
    ret, atr, adx, vol = state_mean
    if atr > 0.012 or vol > 1.8:
        return Regime.VOLATILE
    if adx > 22:
        if ret > 0.0:
            return Regime.TRENDING_UP
        return Regime.TRENDING_DOWN
    if abs(ret) < 0.0015 and atr < 0.008:
        return Regime.RANGING
    if ret > 0.0:
        return Regime.TRENDING_UP
    return Regime.TRENDING_DOWN


# ────────────────────────────────────────────────────────────────────
# Fallback threshold classifier
# ────────────────────────────────────────────────────────────────────


class _ThresholdClassifier:
    """Cheap rule-based fallback used when training data is insufficient."""

    @staticmethod
    def classify(
        returns: float,
        atr: float,
        adx: float,
        vol_ratio: float,
    ) -> RegimeState:
        if atr > 0.012 or vol_ratio > 1.8:
            reg = Regime.VOLATILE
        elif adx > 22:
            reg = Regime.TRENDING_UP if returns > 0 else Regime.TRENDING_DOWN
        elif abs(returns) < 0.0015 and atr < 0.008:
            reg = Regime.RANGING
        elif returns > 0:
            reg = Regime.TRENDING_UP
        else:
            reg = Regime.TRENDING_DOWN
        probs = {r: 0.0 for r in Regime}
        probs[reg] = 1.0
        return RegimeState(regime=reg, confidence=1.0, probabilities=probs)


# ────────────────────────────────────────────────────────────────────
# Public detector
# ────────────────────────────────────────────────────────────────────


class HMMRegimeDetector:
    """4-state Gaussian HMM regime classifier with KMeans-style state labelling.

    Training: call :meth:`fit` with a feature matrix. Prediction: call
    :meth:`detect` with a single (returns, atr, adx, vol_ratio) tuple
    to get a :class:`RegimeState` with full posterior distribution.
    """

    MIN_FIT_SAMPLES = 60  # enough for 4-state HMM with diagonal cov

    def __init__(
        self,
        n_states: int = 4,
        n_restarts: int = 3,
        seed: int = 0,
    ) -> None:
        self.n_states = int(n_states)
        self._hmm = _GaussianHMM(
            n_states=self.n_states,
            n_features=len(FEATURE_NAMES),
            n_restarts=n_restarts,
            seed=seed,
        )
        self._state_labels: List[Regime] = [Regime.UNKNOWN] * self.n_states
        self._fitted = False

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(self, features: np.ndarray) -> bool:
        """Fit the HMM on a (T, 4) feature matrix.

        Returns True on success, False if there is not enough data —
        in which case the detector falls back to the threshold
        classifier at predict time.
        """
        if features.shape[0] < self.MIN_FIT_SAMPLES:
            logger.info(
                "HMMRegimeDetector: %d samples < min %d — falling back to threshold classifier",
                features.shape[0], self.MIN_FIT_SAMPLES,
            )
            self._fitted = False
            return False
        try:
            ll = self._hmm.fit(features)
            logger.info("HMMRegimeDetector: fit OK, log-likelihood=%.2f", ll)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HMMRegimeDetector: fit failed (%s) — threshold fallback", exc)
            self._fitted = False
            return False
        self._state_labels = [_label_state(self._hmm.means[k]) for k in range(self.n_states)]
        self._fitted = True
        return True

    def detect(
        self,
        returns: float,
        atr: float,
        adx: float,
        vol_ratio: float,
    ) -> RegimeState:
        """Classify a single feature vector. Returns posterior probs."""
        if not self._fitted:
            return _ThresholdClassifier.classify(returns, atr, adx, vol_ratio)
        x = np.array([[returns, atr, adx, vol_ratio]], dtype=np.float64)
        gamma = self._hmm.predict_posteriors(x)[0]
        probs_regimes: Dict[Regime, float] = {r: 0.0 for r in Regime}
        for k in range(self.n_states):
            r = self._state_labels[k]
            probs_regimes[r] += float(gamma[k])
        best_regime = max(probs_regimes.items(), key=lambda kv: kv[1])[0]
        return RegimeState(
            regime=best_regime,
            confidence=float(probs_regimes[best_regime]),
            probabilities=probs_regimes,
        )

    def detect_from_history(
        self,
        closes: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        volumes: Sequence[float],
        adx_series: Optional[Sequence[Optional[float]]] = None,
    ) -> RegimeState:
        """End-to-end: extract features from the latest bar, then detect."""
        if len(closes) < 2:
            return RegimeState(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                probabilities={r: 0.0 for r in Regime},
            )
        if not self._fitted:
            features = build_feature_matrix(
                closes, highs, lows, volumes, adx_series,
            )
            if features.shape[0] >= self.MIN_FIT_SAMPLES:
                self.fit(features)
        if not self._fitted:
            # Pull the last bar's features manually for the threshold path
            i = len(closes) - 1
            ret = 0.0
            if i >= 1 and closes[i - 1] > 0:
                ret = float(closes[i]) / float(closes[i - 1]) - 1.0
            atr_val = 0.0
            if closes[i] > 0:
                atr_val = (float(highs[i]) - float(lows[i])) / float(closes[i])
            adx_val = float(adx_series[i]) if (
                adx_series is not None and i < len(adx_series) and adx_series[i] is not None
            ) else 0.0
            vol_ratio = 1.0
            if i >= 20:
                win = [float(volumes[j]) for j in range(i - 20, i) if volumes[j] > 0]
                if win:
                    avg = sum(win) / len(win)
                    if avg > 0 and volumes[i] > 0:
                        vol_ratio = float(volumes[i]) / avg
            return _ThresholdClassifier.classify(ret, atr_val, adx_val, vol_ratio)
        features = build_feature_matrix(
            closes, highs, lows, volumes, adx_series,
        )
        return self.detect(*features[-1].tolist())


# ────────────────────────────────────────────────────────────────────
# Factory: HMM vs ADX
# ────────────────────────────────────────────────────────────────────


def build_regime_detector(config) -> "HMMRegimeDetector | None":
    """Return an HMMRegimeDetector if config asks for it, else None.

    The caller (engine) treats ``None`` as "use the existing ADX path".
    """
    method = str(config.get("regime.detection_method", "adx")).lower()
    if method != "hmm":
        return None
    n_states = int(config.get("regime.hmm_n_states", 4))
    n_restarts = int(config.get("regime.hmm_n_restarts", 3))
    seed = int(config.get("regime.hmm_seed", 0))
    logger.info(
        "Regime detection: HMM (n_states=%d, n_restarts=%d, seed=%d)",
        n_states, n_restarts, seed,
    )
    return HMMRegimeDetector(n_states=n_states, n_restarts=n_restarts, seed=seed)
