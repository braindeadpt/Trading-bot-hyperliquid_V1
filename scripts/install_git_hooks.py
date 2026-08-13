"""Install the managed pre-commit / pre-push hooks into .git/hooks/.

The pre-commit hook runs the FAST path (scripts/run_git_hooks.py --hook
pre-commit): syntax check + scoped security audit + config_hash, only over
the staged files — seconds per commit. The pre-push hook runs the FULL gate
(--hook pre-push): pytest battery + security audit + config_hash.

Idempotent: re-running replaces only hooks this installer wrote (identified
by a marker header). A pre-existing hook written by someone else is left
alone unless --force is given, in which case it is backed up as <name>.bak.

Usage:
  python scripts/install_git_hooks.py               # install both hooks
  python scripts/install_git_hooks.py --force       # replace foreign hooks (backup .bak)
  python scripts/install_git_hooks.py --uninstall   # remove managed hooks only
  python scripts/install_git_hooks.py --list        # show current state
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_git_hooks.py"
MARKER = "# Managed by scripts/install_git_hooks.py"
HOOKS = {
    "pre-commit": "--hook pre-commit",
    "pre-push": "--hook pre-push",
}


def _hooks_dir() -> Path:
    """Resolve .git/hooks from the current repository (created if missing).

    ``git rev-parse --git-dir`` returns ".git" relative to cwd (hooks live
    under the git dir, not beside it).
    """
    r = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        print(f"[FAIL] not inside a git repository: {r.stderr.strip()}")
        raise SystemExit(2)
    git_dir = Path(r.stdout.strip())
    git_dir = git_dir if git_dir.is_absolute() else (Path.cwd() / git_dir)
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    return hooks


def _hook_body(hook: str) -> str:
    """The managed hook script (POSIX sh — works on Windows via Git Bash)."""
    python = sys.executable.replace("\\", "/")
    runner = RUNNER.as_posix()
    return (
        "#!/usr/bin/env sh\n"
        f"{MARKER} — do not edit (re-run the installer to refresh).\n"
        'cd "$(git rev-parse --show-toplevel)" || exit 1\n'
        f'exec "{python}" "{runner}" {HOOKS[hook]}\n'
    )


def _is_managed(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _install(hooks_dir: Path, *, force: bool) -> int:
    rc = 0
    for hook in HOOKS:
        target = hooks_dir / hook
        if target.exists() and not _is_managed(target):
            if not force:
                print(
                    f"[SKIP] {hook}: foreign hook exists ({target.name}). "
                    f"Re-run with --force to back it up and replace."
                )
                rc = 1
                continue
            backup = target.with_name(f"{hook}.bak")
            shutil.copy2(target, backup)
            print(f"[BACKUP] {hook} -> {backup.name}")
        target.write_text(_hook_body(hook), encoding="utf-8")
        target.chmod(0o755)
        print(f"[INSTALL] .git/hooks/{hook} -> run_git_hooks.py {HOOKS[hook]}")
    return rc


def _uninstall(hooks_dir: Path) -> None:
    for hook in HOOKS:
        target = hooks_dir / hook
        if _is_managed(target):
            target.unlink()
            print(f"[REMOVE] .git/hooks/{hook}")
        else:
            print(f"[SKIP] {hook}: not managed (left in place)")


def _list(hooks_dir: Path) -> None:
    for hook in HOOKS:
        target = hooks_dir / hook
        if _is_managed(target):
            print(f"{hook}: managed hook installed")
        elif target.exists():
            print(f"{hook}: FOREIGN hook present (not managed)")
        else:
            print(f"{hook}: not installed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace foreign hooks (backup .bak)")
    parser.add_argument("--uninstall", action="store_true", help="remove managed hooks only")
    parser.add_argument("--list", action="store_true", help="show current hook state")
    args = parser.parse_args()

    hooks_dir = _hooks_dir()
    if args.list:
        _list(hooks_dir)
        return 0
    if args.uninstall:
        _uninstall(hooks_dir)
        return 0
    return _install(hooks_dir, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
