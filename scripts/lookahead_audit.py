"""Static look-ahead / future-data auditor.

Scans the strategies and backtest code for patterns that would
introduce look-ahead bias in backtests or live runs.

Heuristics (line-based, conservative — false positives possible;
review every match):

  * Future-indexed slices:   data[i+1]  /  data[:-N] negative shift
  * Pandas negative shifts:  df.shift(-N)  /  .shift(-1, freq=)
  * Future variable names:   next_*,  future_*,  forward_*
  * Synthetic timestamps:    time.time() + N>0  where N>=60s
  * .iloc[-N]  on a freshly fetched DataFrame (often OK, flagged
    for manual review)
  * Direct access to "next_candle" / "tomorrow" / "ahead" identifiers

Exits 0 if no findings, 1 if any HIGH-severity pattern is detected.
Use --ci to fail on any non-LOW finding.

Usage:
    python scripts/lookahead_audit.py
    python scripts/lookahead_audit.py --ci
    python scripts/lookahead_audit.py --paths src/strategies src/backtest
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Pattern


ROOT = Path(__file__).resolve().parents[1]
# v3.1.19: scan the full src tree — the timestamp-convention bug fixed
# in v3.1.16 (close_time vs open_time) lived in src/data/, which was
# previously outside the audit scope.
DEFAULT_PATHS = (
    "src/strategies",
    "src/backtest",
    "src/data",
    "src/core",
    "src/exchanges",
)

# ────────────────────────────────────────────────────────────────────
# Pattern catalogue
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    code: str
    severity: str  # HIGH | MEDIUM | LOW
    description: str
    pattern: Pattern[str]
    allow_substrings: tuple = ()  # substrings that suppress a match


RULES: List[Rule] = [
    Rule(
        "LOOKAHEAD-001", "HIGH",
        "pandas/Series negative shift (uses future data)",
        re.compile(r"\.(shift|tshift)\(\s*-\s*\d+"),
    ),
    Rule(
        "LOOKAHEAD-002", "HIGH",
        "future index access (data[i + N] with N>=1, or [i + offset])",
        # Matches `[expr + N]` where N>=1 (positive offset = future bar).
        re.compile(r"\[\s*[A-Za-z_][\w\.]*\s*\+\s*[123456789]\d*\s*\]"),
    ),
    Rule(
        "LOOKAHEAD-002b", "HIGH",
        "future index access with variable offset ([i + offset])",
        # v3.1.19: catch variable offsets too — ``candles[i + offset]`` is
        # still a look-ahead if offset > 0.
        re.compile(r"\[\s*\w+\s*\+\s*\w+\s*\]"),
    ),
    Rule(
        "LOOKAHEAD-003", "HIGH",
        "future-indexed variable (next_*, future_*, forward_*)",
        re.compile(r"\b(next_[a-z_][\w]*|future_[a-z_][\w]*|forward_[a-z_][\w]*)"),
        # v3.1.19: the following identifiers are legitimate uses of
        # ``next_*`` that do not represent look-ahead bias:
        #   * next_candle_complete — DataBus topic name
        #   * next_funding_ts, next_funding_time_ms, next_ms — fields
        #     holding the *next* scheduled funding settlement time
        #     (a forward timestamp we are walking toward, not peeking at)
        #   * nextFundingTime — Binance/JSON field name
        #   * forward_binance_prices, forward_binance_perp_prices — these
        #     are *publisher* functions that push Binance prices onto
        #     the DataBus; they don't peek at future data.
        allow_substrings=(
            "next_candle_complete",
            "next_funding_ts",
            "next_funding_time_ms",
            "next_ms",
            "nextFundingTime",
            "next_reset_remaining_sec",
            "forward_binance_prices",
            "forward_binance_perp_prices",
        ),
    ),
    Rule(
        "LOOKAHEAD-004", "MEDIUM",
        "synthetic future timestamp (time.time() + N>=60)",
        re.compile(r"time\.time\(\)\s*\+\s*(\d{3,})"),
    ),
    Rule(
        "LOOKAHEAD-005", "MEDIUM",
        ".iloc[-N] on a variable (verify N is last-bar, not future)",
        # v3.1.19: .iloc uses square brackets, not parens. .iloc[-1] on
        # an equity curve is the *last* bar, not a future peek — that
        # pattern is the common case and should not fail CI. The regex
        # excludes N=1.
        re.compile(r"\.iloc\[\s*-((?:[2-9]|[1-9]\d+))"),
    ),
    Rule(
        "LOOKAHEAD-006", "LOW",
        "comment with 'forward' / 'future' (informational)",
        re.compile(r"#.*\b(forward[-_ ]fill|future[-_ ]data|look[-_ ]ahead)\b", re.IGNORECASE),
    ),
    Rule(
        "LOOKAHEAD-007", "MEDIUM",
        "candle timestamp stored as Binance kline open_time (timestamp_ms = int(k[0]))",
        # v3.1.19: catches the v3.1.16 bug. Live candles use close_time
        # (candle_builder convention), so any historical fetcher writing
        # k[0] to timestamp_ms creates a duplicate row + look-ahead bias.
        re.compile(r"timestamp_ms\s*=\s*int\(\s*k\s*\[\s*0\s*\]\s*\)"),
    ),
]

SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


@dataclass(frozen=True)
class Finding:
    file: Path
    line: int
    code: str
    severity: str
    snippet: str


def iter_python_files(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            yield from sorted(p.rglob("*.py"))


def scan_file(path: Path) -> List[Finding]:
    findings: List[Finding] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings

    for lineno, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            m = rule.pattern.search(line)
            if not m:
                continue
            if any(s in line for s in rule.allow_substrings):
                continue
            findings.append(Finding(
                file=path,
                line=lineno,
                code=rule.code,
                severity=rule.severity,
                snippet=line.strip()[:160],
            ))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--paths",
        nargs="+",
        default=list(DEFAULT_PATHS),
        help="Files or directories to scan (default: src/strategies src/backtest)",
    )
    ap.add_argument(
        "--ci",
        action="store_true",
        help="Fail on any non-LOW finding (default: only HIGH fails)",
    )
    args = ap.parse_args()

    paths = [Path(p) for p in args.paths]
    if not paths:
        print("ERROR: no paths provided", file=sys.stderr)
        return 2

    all_findings: List[Finding] = []
    files = list(iter_python_files(paths))
    for f in files:
        all_findings.extend(scan_file(f))

    print("=" * 72)
    print("  LOOK-AHEAD / FUTURE-DATA AUDIT")
    print("=" * 72)
    print(f"  Files scanned : {len(files)}")
    print(f"  Findings      : {len(all_findings)}")
    print("=" * 72)

    if not all_findings:
        print("[OK] no look-ahead patterns detected")
        return 0

    by_code: dict[str, List[Finding]] = {}
    for f in all_findings:
        by_code.setdefault(f.code, []).append(f)

    exit_code = 0
    for code, items in sorted(by_code.items()):
        rule = next(r for r in RULES if r.code == code)
        print(f"\n[{rule.severity}] {code}  ({len(items)} match(es))  — {rule.description}")
        for it in items[:25]:
            rel = it.file.relative_to(ROOT) if it.file.is_absolute() else it.file
            print(f"   {rel}:{it.line}  {it.snippet}")
        if len(items) > 25:
            print(f"   …and {len(items) - 25} more")
        if SEVERITY_RANK[rule.severity] >= 3:
            exit_code = 1
        if args.ci and SEVERITY_RANK[rule.severity] >= 2:
            exit_code = 1

    print("\n" + "=" * 72)
    if exit_code == 0:
        print("[OK] no HIGH-severity look-ahead issues (CI mode: %s)" % ("strict" if args.ci else "lenient"))
    else:
        print("[FAIL] look-ahead audit blocked — see findings above")
    print("=" * 72)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
