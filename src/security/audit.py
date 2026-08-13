"""
Security audit runner for the Hyperliquid trading bot.

Scans the entire ``src/`` tree for dangerous patterns, hardcoded secrets,
unsafe imports, and suspicious network / filesystem calls.  Produces a
structured report with severity levels and returns a non-zero exit code
when critical issues are detected.

Usage::

    python -m src.security.audit
    python -m src.security.audit --src-dir ./custom_src
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SRC_DIR: Path = Path(__file__).resolve().parents[1]  # src/
LOG_DIR: Path = Path(__file__).resolve().parents[2] / "logs"
SEVERITY_ORDER: List[str] = ["critical", "high", "medium", "low", "info"]

# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------


class Severity(IntEnum):
    CRITICAL = 4
    HIGH = 3
    MEDIUM = 2
    LOW = 1
    INFO = 0


SEVERITY_LABEL: Dict[Severity, str] = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "MEDIUM",
    Severity.LOW: "LOW",
    Severity.INFO: "INFO",
}

# ---------------------------------------------------------------------------
# Audit finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A single security finding."""

    file: Path
    line: int
    severity: Severity
    rule_id: str
    message: str
    snippet: str = ""


# ---------------------------------------------------------------------------
# Audit rules — ReDoS-safe static regexes (no nested quantifiers / backrefs)
# ---------------------------------------------------------------------------

# Hardcoded API keys / secrets — only exact prefixes, bounded to single line
_SECRET_PREFIXES: Tuple[str, ...] = (
    "api_key",
    "apikey",
    "api_secret",
    "apisecret",
    "secret_key",
    "secretkey",
    "private_key",
    "privkey",
    "password",
    "passwd",
    "token",
    "auth_token",
    "bearer",
    "aws_access_key_id",
    "aws_secret_access_key",
)

# Looks like a high-entropy secret assignment on a single line.
# We anchor to the prefix (case-insensitive) followed by optional spaces/
# punctuation, then a quoted or raw string of ≥20 chars.
_SECRET_RE: re.Pattern[str] = re.compile(
    r"(?im)^[^#\n]*?\b(?:"
    + "|".join(re.escape(p) for p in _SECRET_PREFIXES)
    + r")\s*[=:]\s*['\"]\s*([A-Za-z0-9+/=_\-]{20,})\s*['\"]"
)

# Dangerous builtins
_EVAL_EXEC_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(eval|exec|compile)\s*\("
)

# Dangerous deserialization
_PICKLE_RE: re.Pattern[str] = re.compile(
    r"(?i)\bpickle\.loads?\s*\("
)

# Dynamic __import__
_DYNAMIC_IMPORT_RE: re.Pattern[str] = re.compile(
    r"(?i)\b__import__\s*\("
)

# Subprocess / os.system
_SUBPROCESS_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:os\.system|subprocess\.(?:call|run|Popen|check_output|check_call))\s*\("
)

# urllib / requests to unknown domains
_HTTP_CLIENT_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:urllib\.request\.urlopen|urllib\.request\.Request|requests\.(?:get|post|put|delete|patch|request))\s*\("
)

# File-system calls that may escape project directory
# We flag open(path, ...) with a mode containing 'w' or 'a',
# os.remove, os.unlink, shutil.rmtree, pathlib.Path().write_text etc.
_FILE_WRITE_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:os\.(?:remove|unlink|rmdir)|shutil\.rmtree|\.write_text\s*\(|\.write_bytes\s*\(|open\s*\([^,]*,\s*['\"][wa])"
)

# Inline acceptance marker: ``# audit-ok: AUDIT-00X — justification`` on the
# flagged line (or the line directly above) documents a reviewed-and-accepted
# finding. The finding is moved to ``suppressed_findings`` and excluded from
# the blocking counts. The marker is self-validating: it only suppresses a
# rule that actually fired at that exact location — a stray marker on a line
# with no match does nothing.
_SUPPRESSION_RE: re.Pattern[str] = re.compile(r"#\s*audit-ok:\s*([A-Z]+-\d+)\b")

# Accepted HIGH findings baseline — the source of truth for "deliberately
# accepted HIGH findings" (decisions in docs/SECURITY.md §2.4), tracked by
# ``(rule_id, file)`` so line-number edits don't churn the baseline. The CLI
# flag ``--enforce-baseline`` (always passed by run_pre_push_gate.py and
# run_ci_tests.py) fails on any HIGH/CRITICAL finding whose pair is NOT here
# — a new HIGH must be remediated or added here with justification — and on
# any suppressed HIGH that is NOT mirrored here (acceptance must be
# documented in the baseline, not only via an in-code marker).
ACCEPTED_HIGH_BASELINE: FrozenSet[Tuple[str, str]] = frozenset({
    ("AUDIT-005", "utils/crash_recovery.py"),
})


def _norm_file(path: Path) -> str:
    """Normalize a finding's relative path to forward slashes (baseline keys)."""
    return str(path).replace("\\", "/")

# Module-level imports of urllib or requests (we flag to verify domain allowlist)
_IMPORT_HTTP_RE: re.Pattern[str] = re.compile(
    r"(?i)^\s*import\s+urllib|^\s*from\s+urllib|^\s*import\s+requests|^\s*from\s+requests"
)

# Commented-out code can hide backdoors — we flag suspicious commented patterns
_SUSPICIOUS_COMMENT_RE: re.Pattern[str] = re.compile(
    r"(?i)#\s*(?:eval|exec|os\.system|subprocess|urllib|requests|pickle\.loads|__import__)"
)

# Known Hyperliquid / Binance / allowed domains (everything else is flagged for review)
_ALLOWED_DOMAINS: Tuple[str, ...] = (
    "api.hyperliquid.xyz",
    "api.hyperliquid-testnet.xyz",
    "stats-data.hyperliquid.xyz",
    "api.binance.com",
    "fapi.binance.com",
    "testnet.binance.vision",
    "api.bybit.com",
    "www.okx.com",
    "127.0.0.1",
    "localhost",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_source(path: Path) -> str:
    """Read a Python source file safely (UTF-8, errors replaced)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _find_line_number(text: str, pos: int) -> int:
    """Return the 1-based line number for character position *pos* in *text*."""
    return text[:pos].count("\n") + 1


def _extract_snippet(text: str, pos: int, radius: int = 40) -> str:
    """Return a short snippet around character position *pos*."""
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    snippet = text[start:end]
    # Collapse newlines for compact display
    snippet = snippet.replace("\n", " ").replace("\r", "")
    snippet = snippet.strip()
    return snippet


def _hash_file(path: Path) -> str:
    """Return SHA-256 hex digest of file contents."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Audit engine
# ---------------------------------------------------------------------------

class SecurityAuditor:
    """
    Walks the source tree, applies static rules, and collects findings.
    """

    def __init__(
        self,
        src_dir: Path,
        allowed_domains: Optional[Tuple[str, ...]] = None,
        skip_dirs: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self.src_dir: Path = src_dir.resolve()
        self.allowed_domains: Tuple[str, ...] = allowed_domains or _ALLOWED_DOMAINS
        self.skip_dirs: Tuple[str, ...] = skip_dirs or (
            "__pycache__",
            ".git",
            ".venv",
            "venv",
            "env",
            ".pytest_cache",
            "node_modules",
        )
        self.findings: List[Finding] = []
        # Findings accepted via an inline ``# audit-ok: RULE_ID`` marker —
        # reported in the audit output but excluded from blocking counts.
        self.suppressed_findings: List[Finding] = []
        self.files_scanned: int = 0
        self.lines_scanned: int = 0

    # -- Scanning helpers -----------------------------------------------------

    def _iter_py_files(self) -> Iterator[Path]:
        """Yield all ``.py`` files under *src_dir*, skipping hidden dirs."""
        for root, dirs, files in os.walk(self.src_dir):
            # Remove skip_dirs in-place so os.walk doesn't descend into them
            dirs[:] = [d for d in dirs if d not in self.skip_dirs and not d.startswith(".")]
            for f in files:
                if f.endswith(".py"):
                    yield Path(root) / f

    def _is_suppressed(self, text: str, match: re.Match[str], rule_id: str) -> bool:
        """True when the match line (or the line directly above) carries an
        ``# audit-ok: <rule_id>`` marker — the finding is accepted and
        documented in-code instead of blocking."""
        line_start = text.rfind("\n", 0, match.start()) + 1
        candidates = [line_start]
        if line_start > 0:
            candidates.append(text.rfind("\n", 0, line_start - 1) + 1)
        for start in candidates:
            end = text.find("\n", start)
            line_text = text[start:end if end != -1 else len(text)]
            m = _SUPPRESSION_RE.search(line_text)
            if m and m.group(1) == rule_id:
                return True
        return False

    def _add_finding(
        self,
        path: Path,
        text: str,
        match: re.Match[str],
        severity: Severity,
        rule_id: str,
        message: str,
    ) -> None:
        """Append a Finding derived from a regex match.

        A finding whose location carries an ``# audit-ok: <rule_id>`` marker
        is recorded as suppressed (accepted + documented) instead of being
        added to the blocking ``findings`` list.
        """
        line = _find_line_number(text, match.start())
        snippet = _extract_snippet(text, match.start())
        finding = Finding(
            file=path.relative_to(self.src_dir),
            line=line,
            severity=severity,
            rule_id=rule_id,
            message=message,
            snippet=snippet,
        )
        if self._is_suppressed(text, match, rule_id):
            self.suppressed_findings.append(finding)
            return
        self.findings.append(finding)

    # -- Rule engines --------------------------------------------------------

    def _rule_eval_exec(self, path: Path, text: str) -> None:
        """Flag any use of eval, exec, or compile (excluding re.compile)."""
        # Skip the audit module itself — its docstrings contain the keywords for detection
        if path.name == "audit.py" and "security" in str(path):
            return
        for m in _EVAL_EXEC_RE.finditer(text):
            keyword = m.group(1).lower()
            # Skip re.compile — it's the safe regex compilation function
            if keyword == "compile":
                prefix = text[max(0, m.start() - 10):m.start()]
                if "re." in prefix or "Pattern[" in prefix:
                    continue
            self._add_finding(
                path,
                text,
                m,
                Severity.CRITICAL,
                "AUDIT-001",
                "Dangerous builtin call (eval/exec/compile) detected",
            )

    def _rule_hardcoded_secrets(self, path: Path, text: str) -> None:
        """Flag assignments that look like hardcoded secrets."""
        # Skip the vault module itself — it legitimately contains patterns like these
        if path.name == "vault.py" and "security" in str(path):
            return
        for m in _SECRET_RE.finditer(text):
            secret_val = m.group(1)
            # Skip obviously dummy / placeholder values
            if re.fullmatch(r"(?:your_?|example|test|dummy|placeholder|changeme|secret)[A-Za-z0-9_\-]*", secret_val, re.I):
                continue
            self._add_finding(
                path,
                text,
                m,
                Severity.CRITICAL,
                "AUDIT-002",
                "Possible hardcoded secret / API key",
            )

    def _rule_http_clients(self, path: Path, text: str) -> None:
        """Flag urllib / requests calls; reviewers must verify target domain."""
        for m in _HTTP_CLIENT_RE.finditer(text):
            # Heuristic: check whether the same line contains an allowed domain
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line_end = line_end if line_end != -1 else len(text)
            line_text = text[line_start:line_end]
            if any(domain in line_text for domain in self.allowed_domains):
                # Allowed domain present — downgrade to INFO for traceability
                sev = Severity.INFO
                msg = "HTTP client call to allowed domain"
            else:
                sev = Severity.HIGH
                msg = "HTTP client call to unverified domain — review required"
            self._add_finding(path, text, m, sev, "AUDIT-003", msg)

    def _rule_file_write_outside_project(self, path: Path, text: str) -> None:
        """
        Flag filesystem write/delete operations.

        If the call is inside the project directory or uses a relative path,
        it is downgraded to MEDIUM.  Absolute paths outside the project are
        flagged HIGH.
        """
        # Skip the audit module itself — it legitimately writes reports
        if path.name == "audit.py" and "security" in str(path):
            return
        for m in _FILE_WRITE_RE.finditer(text):
            snippet = _extract_snippet(text, m.start(), radius=80)
            # Simple heuristic: absolute path outside project tree
            if re.search(r"['\"]/[^'\"]*['\"]", snippet) and self.src_dir.as_posix() not in snippet:
                sev = Severity.HIGH
                msg = "File operation using absolute path outside project — review required"
            else:
                sev = Severity.MEDIUM
                msg = "File write/delete operation detected — confirm path is safe"
            self._add_finding(path, text, m, sev, "AUDIT-004", msg)

    def _rule_subprocess(self, path: Path, text: str) -> None:
        """Flag os.system and subprocess calls."""
        for m in _SUBPROCESS_RE.finditer(text):
            self._add_finding(
                path,
                text,
                m,
                Severity.HIGH,
                "AUDIT-005",
                "Subprocess / os.system call detected — verify necessity",
            )

    def _rule_pickle(self, path: Path, text: str) -> None:
        """Flag pickle.loads (dangerous deserialization)."""
        for m in _PICKLE_RE.finditer(text):
            # Skip docstrings and comments that merely mention the pattern
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_text = text[line_start:m.start()]
            if '"""' in line_text or "'''" in line_text or line_text.strip().startswith("#"):
                continue
            self._add_finding(
                path,
                text,
                m,
                Severity.HIGH,
                "AUDIT-006",
                "Unsafe pickle.loads usage — arbitrary code execution risk",
            )

    def _rule_dynamic_import(self, path: Path, text: str) -> None:
        """Flag dynamic __import__ with string arguments."""
        for m in _DYNAMIC_IMPORT_RE.finditer(text):
            # Skip the audit module itself (it contains the pattern for detection)
            if path.name == "audit.py" and "security" in str(path):
                continue
            self._add_finding(
                path,
                text,
                m,
                Severity.HIGH,
                "AUDIT-007",
                "Dynamic __import__ call — possible code injection vector",
            )

    def _rule_suspicious_comments(self, path: Path, text: str) -> None:
        """Flag comments that contain dangerous keywords (possible hidden backdoors)."""
        # Skip the audit module itself — its docstrings explain the rules
        if path.name == "audit.py" and "security" in str(path):
            return
        for m in _SUSPICIOUS_COMMENT_RE.finditer(text):
            self._add_finding(
                path,
                text,
                m,
                Severity.LOW,
                "AUDIT-008",
                "Comment contains suspicious keyword — verify not hidden code",
            )

    def _rule_import_http(self, path: Path, text: str) -> None:
        """Log urllib / requests imports at INFO level for inventory."""
        for m in _IMPORT_HTTP_RE.finditer(text):
            self._add_finding(
                path,
                text,
                m,
                Severity.INFO,
                "AUDIT-009",
                "HTTP client library imported — ensure domain allowlist is enforced",
            )

    # -- Public API ----------------------------------------------------------

    def _resolve_target(self, target: Union[Path, str]) -> Optional[Path]:
        """Resolve a caller-supplied target to an absolute ``.py`` file under
        ``src_dir``; return ``None`` for non-``.py`` paths, missing files, or
        paths outside ``src_dir``."""
        if isinstance(target, str):
            target = Path(target)
        p = target if target.is_absolute() else self.src_dir / target
        p = p.resolve()
        try:
            p.relative_to(self.src_dir)
        except ValueError:
            return None
        if p.suffix != ".py" or not p.is_file():
            return None
        return p

    def run(self, targets: Optional[Sequence[Path]] = None) -> None:
        """Execute the audit scan.

        With *targets* (a list of ``.py`` paths, resolved relative to
        ``src_dir``), only those files are scanned — the fast path used by the
        pre-commit hook (scripts/run_git_hooks.py). The default (``None``)
        scans the whole tree, exactly as before.
        """
        self.findings.clear()
        self.suppressed_findings.clear()
        self.files_scanned = 0
        self.lines_scanned = 0

        if targets is None:
            files = self._iter_py_files()
        else:
            files = [p for p in (self._resolve_target(t) for t in targets) if p is not None]

        for py_file in files:
            self.files_scanned += 1
            try:
                text = _read_source(py_file)
            except OSError as exc:
                logging.warning("Cannot read %s: %s", py_file, exc)
                continue

            self.lines_scanned += text.count("\n") + 1

            # Apply every rule
            self._rule_eval_exec(py_file, text)
            self._rule_hardcoded_secrets(py_file, text)
            self._rule_http_clients(py_file, text)
            self._rule_file_write_outside_project(py_file, text)
            self._rule_subprocess(py_file, text)
            self._rule_pickle(py_file, text)
            self._rule_dynamic_import(py_file, text)
            self._rule_suspicious_comments(py_file, text)
            self._rule_import_http(py_file, text)

        # Sort findings by severity descending, then file, then line
        self.findings.sort(key=lambda f: (-f.severity.value, str(f.file), f.line))

    def _render_suppressed(self) -> str:
        """Render the accepted (suppressed) findings block."""
        lines: List[str] = [f"{'─' * 72}"]
        lines.append(
            f"  [ACCEPTED] — {len(self.suppressed_findings)} finding(s) suppressed "
            "via # audit-ok markers (documented in-code)"
        )
        lines.append("─" * 72)
        for f in sorted(self.suppressed_findings, key=lambda x: (-x.severity.value, str(x.file), x.line)):
            lines.append(f"  {f.rule_id}  {f.file}:{f.line} ({f.severity.name})")
            lines.append(f"       → {f.message}")
            lines.append("")
        return "\n".join(lines)

    def generate_report(self) -> str:
        """Return a human-readable multi-line report string."""
        lines: List[str] = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append("=" * 72)
        lines.append("  SECURITY AUDIT REPORT")
        lines.append(f"  Generated : {now}")
        lines.append(f"  Source    : {self.src_dir}")
        lines.append(f"  Files     : {self.files_scanned}")
        lines.append(f"  Lines     : {self.lines_scanned}")
        lines.append(f"  Findings  : {len(self.findings)}")
        lines.append(f"  Suppressed: {len(self.suppressed_findings)} (accepted via # audit-ok)")
        lines.append("=" * 72)
        lines.append("")

        if not self.findings:
            lines.append("✅ No security findings detected.")
            lines.append("")
            if self.suppressed_findings:
                lines.append(self._render_suppressed())
            return "\n".join(lines)

        # Group by severity
        grouped: Dict[str, List[Finding]] = {s: [] for s in SEVERITY_ORDER}
        for finding in self.findings:
            grouped[SEVERITY_LABEL[finding.severity].lower()].append(finding)

        for sev_label in SEVERITY_ORDER:
            findings_in_group = grouped.get(sev_label, [])
            if not findings_in_group:
                continue
            lines.append(f"{'─' * 72}")
            lines.append(f"  [{sev_label.upper()}] — {len(findings_in_group)} finding(s)")
            lines.append(f"{'─' * 72}")
            for f in findings_in_group:
                lines.append(f"  {f.rule_id}  {f.file}:{f.line}")
                lines.append(f"       → {f.message}")
                if f.snippet:
                    lines.append(f"       snippet: {f.snippet}")
                lines.append("")

        # Accepted (suppressed) findings — reviewed + documented in-code.
        if self.suppressed_findings:
            lines.append(self._render_suppressed())

        # Summary footer
        critical_count = sum(1 for f in self.findings if f.severity == Severity.CRITICAL)
        high_count = sum(1 for f in self.findings if f.severity == Severity.HIGH)
        lines.append("=" * 72)
        lines.append(f"  Critical : {critical_count}")
        lines.append(f"  High     : {high_count}")
        lines.append(f"  Medium   : {sum(1 for f in self.findings if f.severity == Severity.MEDIUM)}")
        lines.append(f"  Low      : {sum(1 for f in self.findings if f.severity == Severity.LOW)}")
        lines.append(f"  Info     : {sum(1 for f in self.findings if f.severity == Severity.INFO)}")
        lines.append(f"  Suppressed : {len(self.suppressed_findings)}")
        lines.append("=" * 72)

        if critical_count > 0:
            lines.append("")
            lines.append("❌ CRITICAL findings present. Audit FAILED.")
        elif high_count > 0:
            lines.append("")
            lines.append("⚠️  HIGH findings present. Review recommended before deployment.")
        else:
            lines.append("")
            lines.append("✅ No critical/high findings. Audit PASSED.")

        lines.append("")
        return "\n".join(lines)

    def save_report(self, path: Optional[Path] = None) -> Path:
        """Save the report to disk and return the file path."""
        if path is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            date_stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = LOG_DIR / f"security_audit_{date_stamp}.log"
        path.write_text(self.generate_report(), encoding="utf-8")
        return path

    def has_critical(self) -> bool:
        """Return ``True`` if any CRITICAL finding was recorded."""
        return any(f.severity == Severity.CRITICAL for f in self.findings)

    def has_high_or_above(self) -> bool:
        """Return ``True`` if any HIGH or CRITICAL finding was recorded."""
        return any(f.severity >= Severity.HIGH for f in self.findings)

    def unaccepted_high(
        self,
        baseline: FrozenSet[Tuple[str, str]] = ACCEPTED_HIGH_BASELINE,
    ) -> List[Finding]:
        """HIGH/CRITICAL findings whose ``(rule_id, file)`` is not in the
        accepted baseline — the "new HIGH" set the pre-push gate blocks on."""
        return [
            f for f in self.findings
            if f.severity >= Severity.HIGH
            and (f.rule_id, _norm_file(f.file)) not in baseline
        ]

    def undocumented_acceptance(
        self,
        baseline: FrozenSet[Tuple[str, str]] = ACCEPTED_HIGH_BASELINE,
    ) -> List[Finding]:
        """Suppressed HIGH/CRITICAL findings NOT mirrored in the baseline —
        an in-code ``# audit-ok`` marker without the documented baseline
        entry (acceptance must be recorded in both places)."""
        return [
            f for f in self.suppressed_findings
            if f.severity >= Severity.HIGH
            and (f.rule_id, _norm_file(f.file)) not in baseline
        ]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns exit code 1 if critical findings exist."""
    parser = argparse.ArgumentParser(
        prog="security_audit",
        description="Static security audit for the Hyperliquid trading bot.",
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        default=DEFAULT_SRC_DIR,
        help="Root directory to scan (default: src/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override report output path",
    )
    parser.add_argument(
        "--fail-on-high",
        action="store_true",
        help="Return non-zero exit code for HIGH severity as well as CRITICAL",
    )
    parser.add_argument(
        "--enforce-baseline",
        action="store_true",
        help="fail on HIGH/CRITICAL findings not in the accepted baseline "
             "(ACCEPTED_HIGH_BASELINE, docs/SECURITY.md §2.4) and on "
             "suppressed HIGHs not mirrored there — the pre-push gate and "
             "the CI trio always pass this",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print report to stdout",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.src_dir.exists():
        print(f"ERROR: Source directory does not exist: {args.src_dir}", file=sys.stderr)
        return 2

    auditor = SecurityAuditor(src_dir=args.src_dir)
    auditor.run()
    report = auditor.generate_report()

    if args.verbose:
        print(report)

    report_path = auditor.save_report(path=args.output)
    logging.info("Report saved to %s", report_path)

    if auditor.has_critical():
        logging.error("Critical security findings detected — pipeline blocked.")
        return 1

    if args.fail_on_high and auditor.has_high_or_above():
        logging.error("High/Critical findings detected — pipeline blocked.")
        return 1

    if args.enforce_baseline:
        unaccepted = auditor.unaccepted_high()
        if unaccepted:
            for f in unaccepted:
                logging.error(
                    "UNACCEPTED HIGH %s %s:%d (%s) — not in ACCEPTED_HIGH_BASELINE; "
                    "remediate, or add with justification to docs/SECURITY.md §2.4",
                    f.rule_id, f.file, f.line, f.message,
                )
            logging.error(
                "Pipeline blocked: new HIGH finding(s) beyond the accepted baseline."
            )
            return 1
        undocumented = auditor.undocumented_acceptance()
        if undocumented:
            for f in undocumented:
                logging.error(
                    "UNDOCUMENTED ACCEPTANCE %s %s:%d — # audit-ok marker without "
                    "a matching ACCEPTED_HIGH_BASELINE entry",
                    f.rule_id, f.file, f.line,
                )
            logging.error(
                "Pipeline blocked: acceptance not documented in the baseline."
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
