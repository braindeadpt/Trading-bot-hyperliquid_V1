"""Regression tests for the dual-module-identity Config landmine.

``main.py`` adds both the repo root and ``src/`` to ``sys.path`` and uses a
long-standing bare-import convention (``from utils.config import ...``),
while newer modules (``src/research/*.py``, ``src/backtest/*.py``) import
via ``from src.utils.config import ...``. Because Python treats
``utils.config`` and ``src.utils.config`` as two distinct module objects —
even though both resolve to the identical file on disk — ``Config``
instances created via one import path fail nominal ``isinstance(x, Config)``
checks performed by code that imported ``Config`` via the other path.

Before the fix, this caused ``get_strategy_section`` (and every other
``isinstance(config, Config)`` call site in ``src/utils/config.py`` and
several ``src/backtest/*.py`` modules) to wrap the *Config object itself* as
the underlying ``_data`` dict, so every subsequent dot-path ``.get()`` call
silently returned ``None``/defaults. In production this manifested as
``main.py --mode paper`` deterministically raising
``PreregisterManifestError: frozen params drift`` on every startup with
``strategy.phase08.enabled: true``, because the "live" config snapshot
reported every VolatilityBreakout/VWAPDeviation param as ``None``.

These tests reproduce the two-module-identity scenario directly and confirm
the duck-typed ``coerce_config`` fix reads through it correctly.
"""
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ensure_dual_sys_path() -> None:
    """Mirror main.py's sys.path setup (repo root AND src/ both present)."""
    root = str(REPO_ROOT)
    src = str(REPO_ROOT / "src")
    if root not in sys.path:
        sys.path.insert(0, root)
    if src not in sys.path:
        sys.path.insert(0, src)


def test_bare_and_src_config_are_distinct_classes():
    """Sanity-check the premise: the two import paths really do yield
    different class objects, confirming the landmine this test guards
    against is real and not a hypothetical."""
    _ensure_dual_sys_path()
    from utils.config import Config as BareConfig
    from src.utils.config import Config as SrcConfig

    assert BareConfig is not SrcConfig


def test_get_strategy_section_reads_through_cross_module_config():
    """A Config built via the bare ``utils.config`` import path must still
    be readable by ``get_strategy_section`` imported via ``src.utils.config``."""
    _ensure_dual_sys_path()
    from utils.config import Config as BareConfig
    from src.utils.config import Config as SrcConfig, get_strategy_section

    data = {
        "strategy": {
            "volatility_breakout": {
                "enabled": True,
                "bb_period": 20,
                "bb_std": 2.0,
            }
        }
    }
    bare_cfg = BareConfig(data)
    assert not isinstance(bare_cfg, SrcConfig)

    section = get_strategy_section(bare_cfg, "volatility_breakout")
    assert section == {"enabled": True, "bb_period": 20, "bb_std": 2.0}


def test_coerce_config_duck_types_via_raw_property():
    """Directly exercise coerce_config with a minimal duck-typed fake that
    deliberately is NOT an instance of src.utils.config.Config, to prove
    the fix works for any structurally-Config-like object, not just the
    two real module identities."""
    from src.utils.config import Config, coerce_config

    class FakeConfig:
        """Duck-typed Config-alike: has `.raw` but isn't a Config subclass."""

        def __init__(self, data):
            self._data = data

        @property
        def raw(self):
            return self._data

    fake = FakeConfig({"risk": {"max_positions": 7}})
    assert not isinstance(fake, Config)

    coerced = coerce_config(fake)
    assert isinstance(coerced, Config)
    assert coerced.get("risk.max_positions") == 7


def test_coerce_config_passthrough_and_dict_paths():
    """coerce_config must also handle a genuine Config instance (passthrough,
    no re-wrap) and a plain dict (wrap directly)."""
    from src.utils.config import Config, coerce_config

    cfg = Config({"a": 1})
    assert coerce_config(cfg) is cfg

    wrapped = coerce_config({"a": 1})
    assert isinstance(wrapped, Config)
    assert wrapped.get("a") == 1


def test_compute_config_hash_and_kelly_and_sizing_cross_module():
    """Every other isinstance(config, Config) call site in
    src/utils/config.py fixed via coerce_config must also read through a
    bare-module-identity Config correctly."""
    _ensure_dual_sys_path()
    from utils.config import Config as BareConfig
    from src.utils.config import (
        compute_config_hash,
        get_sizing_version,
        get_trading_symbols,
        phase08_enabled,
        resolve_kelly_enabled,
    )

    data = {
        "symbols": ["BTC", "ETH"],
        "backtest": {"sizing_version": "phase05-risk-at-equity-v1", "kelly_override": None},
        "strategy": {
            "phase08": {"enabled": True},
            "kelly": {"enabled": True},
        },
    }
    bare_cfg = BareConfig(data)

    assert get_trading_symbols(bare_cfg) == ["BTC", "ETH"]
    assert phase08_enabled(bare_cfg) is True
    assert resolve_kelly_enabled(bare_cfg, for_backtest=True) is True
    assert get_sizing_version(bare_cfg) == "phase05-risk-at-equity-v1"
    # Must not raise, and must actually hash real content (not an empty/None
    # placeholder that would result from the object-wrapped-as-dict bug).
    h1 = compute_config_hash(bare_cfg)
    h2 = compute_config_hash(data)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 16


def test_phase08_preregister_no_drift_with_bare_config():
    """Direct regression guard for the actual production bug: load config
    via the bare `utils.config` path (as main.py does), then verify
    `assert_config_matches_preregister` (imported via `src.research`) does
    NOT raise a spurious drift error and the frozen params genuinely match.
    """
    _ensure_dual_sys_path()
    from utils.config import load_config
    from src.research.phase08_preregister import (
        assert_config_matches_preregister,
        persist_preregister_manifest,
    )

    cfg = load_config("config/settings.yaml")
    # The manifest file already exists in this repo (immutable once written);
    # persist_preregister_manifest() is a no-op if content is unchanged and
    # only writes if the file is missing, so this does not mutate frozen state.
    persist_preregister_manifest(cfg)

    # Must not raise PreregisterManifestError. Before the fix this raised
    # deterministically because get_strategy_section() silently returned
    # {} for every strategy section on a bare-module-identity Config.
    assert_config_matches_preregister(cfg)


def test_build_backtest_config_from_yaml_with_bare_config():
    """Same landmine existed in src/backtest/engine.py (hit via
    main.py --mode backtest passing a bare Config as risk_config /
    build_backtest_config_from_yaml's cfg argument). Confirm it now reads
    real values instead of silently falling back to defaults."""
    _ensure_dual_sys_path()
    from utils.config import load_config
    from backtest.engine import build_backtest_config_from_yaml

    cfg = load_config("config/settings.yaml")
    bt_cfg = build_backtest_config_from_yaml(cfg)
    assert bt_cfg.commission_pct == pytest.approx(cfg.get("backtest.commission_pct"))
