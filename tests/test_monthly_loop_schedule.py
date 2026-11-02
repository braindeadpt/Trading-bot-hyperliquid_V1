"""backup-monthly scheduling — the monthly run must never fire on boot.

Incident 2026-11-02: the hl-backup-monthly container command ran
``backup_research_data.py`` BEFORE ``monthly_loop.sh``, so every docker
restart/recreate triggered a full ~9.4GB monthly snapshot+verify —
disk usage on bots-01 hit 84% (warn at 80, pause at 85). The fix sleeps
until the next 1st-of-month 09:00 Europe/Lisbon FIRST and only then
runs the backup, so a boot can never start a run. Local retention also
dropped to a single verified monthly run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.unit


def _backup_monthly_block() -> str:
    text = (ROOT / "deploy" / "linux" / "compose.yaml").read_text(
        encoding="utf-8")
    m = re.search(r"^\s+backup-monthly:\n(?P<body>(?:\s{4,}.*\n)+)",
                  text, re.M)
    assert m, "backup-monthly service block not found in compose.yaml"
    return m.group("body")


def test_monthly_sleeps_before_backing_up():
    """A container boot must reach the scheduler sleep BEFORE the run."""
    body = _backup_monthly_block()
    sleep_idx = body.index("monthly_loop.sh")
    backup_idx = body.index("backup_research_data.py")
    assert sleep_idx < backup_idx, (
        "backup runs before the monthly sleep — a recreate would fire "
        "a full run on boot")


def test_monthly_local_retention_is_one_run():
    """MONTHLY_KEEP=1 — only the newest verified monthly run stays."""
    from scripts.ops import backup_research_data as br
    assert br.MONTHLY_KEEP == 1


def test_monthly_loop_script_targets_first_of_month():
    """The scheduler must aim at the 1st of the month, not a 30d drift."""
    script = (ROOT / "deploy" / "linux" / "monthly_loop.sh").read_text(
        encoding="utf-8")
    assert "$ym-01 09:00" in script
    assert "+1 month" in script
