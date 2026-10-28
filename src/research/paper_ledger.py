"""Unified paper ledger — one read-only row per strategy/experiment.

Post-cutover dashboard package (2026-10-28): every strategy that ever
touched paper PnL surfaces in ONE schema so the panel can show prereg
state, current n vs readout n, readout date and net PnL side by side.

Row schema (stable):

    id           strategy / experiment id
    name         display name
    kind         execution | shadow_engine | shadow_daemon | measure
    prereg_state prereg status string (sealed/in-progress/demoted/...)
    n            current counted n (unit depends on the prereg)
    n_readout    n required for the gated read/kill checkpoint
    readout_ms   scheduled read/review/ETA timestamp (ms) or None
    net          net pnl in `net_unit` (None = sealed/not counted yet)
    net_unit     usd | pct | bps
    detail       free-text note

Everything is opened read-only (sqlite mode=ro); missing ledgers degrade
to "—" rows, never 500s. No tables are written anywhere.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.config import load_config

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
_RESEARCH = ROOT / "data" / "research"
_LIVE = ROOT / "data" / "live"

_MS_DAY = 86_400_000


def _ro(path: Path, timeout: float = 2.0) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                              timeout=timeout)
        con.row_factory = sqlite3.Row
        return con
    except Exception:  # noqa: BLE001
        return None


def _row(id: str, name: str, kind: str, prereg_state: str,
         n=None, n_readout=None, readout_ms=None, net=None,
         net_unit="usd", detail=None) -> Dict[str, Any]:
    return {"id": id, "name": name, "kind": kind,
            "prereg_state": prereg_state, "n": n,
            "n_readout": n_readout, "readout_ms": readout_ms,
            "net": net, "net_unit": net_unit, "detail": detail}


def _ms(y, m, d) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


# ── engine-shadow strategies (persisted outcome boards) ───────────────

def _engine_shadow_rows(cfg: Any, now_ms: int) -> List[Dict[str, Any]]:
    from src.research.shadow_panel import (_load_gate_verdict,
                                           _load_persisted_boards)
    strat_cfg = cfg.get("strategy", {}) or {}
    names = (strat_cfg.get("phase08", {}) or {}).get(
        "shadow_strategies") or []
    latest = _load_persisted_boards(cfg)
    boards = latest[0] if latest else {}
    out = []
    for name in names:
        board = None
        if isinstance(boards, dict):
            for k, v in boards.items():
                if isinstance(v, dict) and v.get("strategy") == name:
                    board = v
                    break
        gate = _load_gate_verdict(name)
        state = (gate or {}).get("verdict") or "demoted-shadow"
        n = int((board or {}).get("n_independent")
                or (board or {}).get("n_evaluated") or 0)
        out.append(_row(
            id=f"{name}::phase08_shadow", name=name,
            kind="shadow_engine", prereg_state=state,
            n=n, n_readout=None, readout_ms=None,
            net=(board or {}).get("net_hypothetical_pnl_pct"),
            net_unit="pct",
            detail=(f"PF {board['net_profit_factor']:.3f}"
                    if board and board.get("net_profit_factor") is not None
                    else None)))
    return out


# ── JevJudge (paper execution experiment) ─────────────────────────────

def _jev_row(now_ms: int) -> Dict[str, Any]:
    n = net = None
    detail = None
    db = _ro(_LIVE / "bot.db")
    if db is not None:
        try:
            r = db.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) pnl"
                " FROM trades WHERE strategy='JevJudge' AND status='closed'"
            ).fetchone()
            n, net = int(r["c"]), float(r["pnl"])
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close()
    return _row(
        id="JevJudge::experiment", name="JevJudge (paper)",
        kind="execution", prereg_state="EXPERIMENT (v2 exits-only)",
        n=n, n_readout=100, readout_ms=_ms(2026, 12, 31), net=net,
        detail="kill read at n>=100 sealed until 2026-12-31")


# ── standalone daemon ledgers ─────────────────────────────────────────

def _carry_row(now_ms: int) -> Dict[str, Any]:
    row = _row(id="CarryA1::shadow", name="CarryA1 spot–perp (shadow)",
               kind="shadow_daemon", prereg_state="sealed",
               n_readout=10, readout_ms=_ms(2027, 4, 9),
               detail="§6 gate — net sealed by construction")
    con = _ro(_RESEARCH / "carry_shadow.db")
    if con is None:
        return row
    try:
        n = con.execute(
            "SELECT COUNT(*) c FROM episodes WHERE state='closed'"
        ).fetchone()["c"]
        row["n"] = int(n)
        dead = (con.execute(
            "SELECT value FROM meta WHERE key='dead'").fetchone())
        if dead and dead["value"]:
            row["prereg_state"] = f"DEAD — {dead['value'][:60]}"
        return row
    except Exception:  # noqa: BLE001
        return row
    finally:
        con.close()


def _unlock_row(now_ms: int) -> Dict[str, Any]:
    row = _row(id="UnlockShort::shadow", name="Unlock short (shadow)",
               kind="shadow_daemon", prereg_state="sealed",
               n_readout=15, readout_ms=_ms(2027, 10, 14),
               detail="read gate n>=15 eligible closes (interim n=8)")
    con = _ro(_RESEARCH / "unlock_shadow.db")
    if con is None:
        return row
    try:
        n = con.execute(
            "SELECT COUNT(*) c FROM unlock_sims WHERE status='closed'"
        ).fetchone()["c"]
        row["n"] = int(n)
        return row
    except Exception:  # noqa: BLE001
        try:
            row["n"] = int(con.execute(
                "SELECT COUNT(*) c FROM unlock_sims").fetchone()["c"])
        except Exception:  # noqa: BLE001
            pass
        return row
    finally:
        con.close()


def _boros_row(now_ms: int) -> Dict[str, Any]:
    row = _row(id="BorosLockedSpread::shadow",
               name="Boros locked spread (shadow)",
               kind="shadow_daemon", prereg_state="sealed",
               n_readout=5, detail="n=5 early-kill checkpoint; read gated")
    con = _ro(_RESEARCH / "boros_shadow.db")
    if con is None:
        return row
    try:
        n_open = con.execute(
            "SELECT COUNT(*) c FROM boros_trades WHERE state='open'"
        ).fetchone()["c"]
        closed = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(realized_usd),0) pnl"
            " FROM boros_close").fetchone()
        row["n"] = int(closed["c"])
        row["net"] = float(closed["pnl"])
        row["detail"] = (f"{row['detail']}; {int(n_open)} open")
        return row
    except Exception:  # noqa: BLE001
        return row
    finally:
        con.close()


def _resmom_row(now_ms: int) -> Dict[str, Any]:
    row = _row(id="ResidualMomentum::forward",
               name="ResidualMomentum forward (paper)",
               kind="shadow_daemon", prereg_state="in_progress",
               n_readout=26, readout_ms=_ms(2027, 5, 3),
               detail="26 closed weekly rebalances -> single read")
    db = _RESEARCH / "resmom_shadow.db"
    if not db.exists():
        return row
    try:
        from src.resmom_shadow.read import evaluate
        res = evaluate(str(db), now_ms=now_ms)
        row["n"] = res.get("closed_weeks")
        cum = res.get("cum_return")
        if isinstance(cum, (int, float)):
            row["net"] = round(cum * 10_000.0, 2)
        v = res.get("verdict")
        if v and v != "IN_PROGRESS":
            row["prereg_state"] = str(v)
        elif res.get("kill", {}).get("breached"):
            row["prereg_state"] = "KILL (descriptive)"
        return row
    except Exception:  # noqa: BLE001
        return row


def _latency_row(now_ms: int) -> Dict[str, Any]:
    row = _row(id="LatencyProbe::measure",
               name="trade.xyz vs Binance latency (measure)",
               kind="measure", prereg_state="in_progress",
               n_readout=100, readout_ms=_ms(2026, 11, 25),
               net_unit="bps",
               detail=">=10 sessions AND >=100 events, max 4 weeks")
    db = _RESEARCH / "latency_probe.db"
    if not db.exists():
        return row
    try:
        from src.latency_probe.read import evaluate
        res = evaluate(str(db), now_ms=now_ms)
        row["n"] = res.get("events_scored")
        row["net"] = res.get("mean_net_bps")
        row["detail"] = (f"{res.get('sessions', 0)} sessions; "
                         f"{res.get('events_total', 0)} events")
        v = res.get("verdict")
        if v and v != "IN_PROGRESS":
            row["prereg_state"] = str(v)
        return row
    except Exception:  # noqa: BLE001
        return row


# ── assembly ──────────────────────────────────────────────────────────

def build_paper_ledger_payload(config: Optional[Any] = None
                               ) -> Dict[str, Any]:
    cfg = config or load_config(ROOT / "config" / "settings.yaml")
    now_ms = int(time.time() * 1000)
    rows: List[Dict[str, Any]] = [_jev_row(now_ms)]
    try:
        rows.extend(_engine_shadow_rows(cfg, now_ms))
    except Exception as exc:  # noqa: BLE001 — degrade, never 500
        logger.debug("paper ledger: engine-shadow rows failed: %s", exc)
    rows.extend([
        _carry_row(now_ms), _unlock_row(now_ms), _boros_row(now_ms),
        _resmom_row(now_ms), _latency_row(now_ms),
    ])
    return {"rows": rows, "generated_ms": now_ms}
