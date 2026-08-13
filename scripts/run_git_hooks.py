"""Git hook runner: fast pre-commit path + full pre-push gate.

Two modes (install via scripts/install_git_hooks.py):

  --hook pre-commit  Fast path — runs on every commit, must stay in seconds.
                     Only the STAGED files are checked, plus the frozen
                     config hash:
                       1. Syntax-check every staged ``.py`` file (in-process
                          ``compile()``; no .pyc written, imports unresolved).
                       2. Scoped security audit over ONLY the staged ``.py``
                          files under ``src/`` (CRITICAL blocks;
                          ``--fail-on-high`` also blocks HIGH) — the same
                          rules the full audit runs, restricted to the diff.
                       3. config_hash vs the Fase 10 frozen manifest (always
                          — catches drift even from a ``DEFAULT_CONFIG``
                          change in ``src/utils/config.py``).
                     The slow checks (full pytest battery, full-tree audit)
                     stay in the pre-push hook.

  --hook pre-push    Full gate — delegates to ``scripts/run_pre_push_gate.py``
                     (pytest battery + full security audit + config_hash).
                     Exit code passes through.

Exit codes: 0 pass · 1 a check failed · 2 infrastructure error (git
unavailable / missing frozen manifest).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Bare-import convention shared with main.py: repo root AND src/ on sys.path.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _staged_files(root: Path) -> list:
    """Return the paths staged for commit (added/copied/modified), relative to *root*."""
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=str(root), capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        print(f"[FAIL] cannot read staged files from git: {r.stderr.strip()}")
        raise SystemExit(2)
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _syntax_check(root: Path, py_files: list) -> int:
    """In-process compile() of each staged .py file; no .pyc written."""
    bad = 0
    for rel in py_files:
        p = root / rel
        if not p.exists():
            continue  # renamed/deleted side of a rename — nothing to check
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as exc:
            print(f"  [FAIL] {rel}:{exc.lineno}: {exc.msg}")
            bad += 1
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  [FAIL] {rel}: cannot read: {exc}")
            bad += 1
    return 1 if bad else 0


def _scoped_audit(root: Path, py_files: list, *, fail_on_high: bool) -> int:
    """Run the security rules over ONLY the staged .py files under src/."""
    from security.audit import SecurityAuditor, Severity

    auditor = SecurityAuditor(src_dir=root / "src")
    auditor.run(targets=[root / f for f in py_files])

    blocking = [
        f for f in auditor.findings
        if f.severity >= Severity.CRITICAL
        or (fail_on_high and f.severity >= Severity.HIGH)
    ]
    for finding in blocking:
        print(
            f"  [FAIL] {finding.rule_id} {finding.file}:{finding.line} "
            f"({finding.severity.name}) {finding.message}"
        )
    if blocking:
        return 1
    print(
        f"  [PASS] scoped audit: {auditor.files_scanned} file(s) under src/, "
        f"{len(auditor.findings)} finding(s)"
    )
    return 0


def _config_hash_check(root: Path) -> int:
    """Effective settings.yaml hash must equal the frozen Fase 10 manifest.

    Mirrors the light snippet in run_pre_push_gate.py / run_ci_tests.py
    (reads the manifest JSON directly — no pandas through src.utils.helpers).
    """
    from src.utils.config import load_config, compute_config_hash

    manifest_path = root / "data" / "research" / "phase10" / "phase10_preregister.json"
    if not manifest_path.exists():
        print("[FAIL] frozen Fase 10 manifest missing — the bot would refuse to start.")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    cfg = load_config(root / "config" / "settings.yaml")
    if compute_config_hash(cfg) == manifest.get("config_hash"):
        print("  [PASS] config_hash matches the frozen Fase 10 manifest")
        return 0
    print("[FAIL] config_hash drifted from the frozen Fase 10 window")
    return 1


def _pre_commit(root: Path, *, fail_on_high: bool) -> int:
    staged = _staged_files(root)
    if not staged:
        print("No staged files — nothing to check.")
        return 0
    py_files = [f for f in staged if f.endswith(".py")]
    print(f">>> pre-commit fast path: {len(staged)} staged file(s), {len(py_files)} .py")

    if py_files:
        rc = _syntax_check(root, py_files)
        if rc != 0:
            print("\n[BLOCKED] syntax errors in staged files. Fix before commit.")
            return rc
        rc = _scoped_audit(root, py_files, fail_on_high=fail_on_high)
        if rc != 0:
            print("\n[BLOCKED] scoped security audit failed. Fix before commit.")
            return rc
    else:
        print("  (no staged .py files — skipping syntax + scoped audit)")

    rc = _config_hash_check(root)
    if rc != 0:
        print(
            "\n[BLOCKED] config_hash drifted from the frozen window. "
            "Restore it or re-freeze before commit."
        )
        return rc

    print("\n[PASS] pre-commit fast path OK.")
    return 0


def _pre_push(root: Path) -> int:
    print(">>> pre-push: full gate (CI battery + security audit + config_hash)")
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "run_pre_push_gate.py")],
        cwd=str(root), check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hook", required=True, choices=["pre-commit", "pre-push"],
        help="which hook to run",
    )
    parser.add_argument(
        "--fail-on-high",
        action="store_true",
        help="pre-commit scoped audit also blocks HIGH findings",
    )
    args = parser.parse_args()

    # Resolve the repo root from git (cwd), not from __file__: the installed
    # hooks cd to the toplevel before invoking, and tests drive the script
    # against a temp git repo. scripts/run_pre_push_gate.py and src/ are then
    # resolved relative to that root.
    root = _git_root()
    if args.hook == "pre-commit":
        return _pre_commit(root, fail_on_high=args.fail_on_high)
    return _pre_push(root)


def _git_root() -> Path:
    """Fallback: resolve the repo root from git when the script is copied out."""
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        print(f"[FAIL] not inside a git repository: {r.stderr.strip()}")
        raise SystemExit(2)
    return Path(r.stdout.strip())


if __name__ == "__main__":
    raise SystemExit(main())
