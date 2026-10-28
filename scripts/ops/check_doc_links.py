#!/usr/bin/env python3
"""check_doc_links.py — verify relative links in tracked markdown files.

Scans every tracked *.md for ](target) links that are relative paths
(no scheme, no http). Each target is resolved against the file's
directory; missing files are reported. Exits 1 if any broken links.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)
SKIP = ("http://", "https://", "mailto:", "javascript:", "#", "tel:")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return [ROOT / f for f in out]


def main() -> int:
    broken: list[str] = []
    for f in tracked_markdown():
        try:
            text = FENCE_RE.sub("", f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for m in LINK_RE.finditer(text):
            target = m.group(1).split("#")[0]
            if not target or target.startswith(SKIP):
                continue
            if not (f.parent / target).resolve().exists():
                rel = f.relative_to(ROOT)
                broken.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1} -> {m.group(1)}")
    if broken:
        print(f"check_doc_links: {len(broken)} broken link(s):")
        print("\n".join("  " + b for b in broken))
        return 1
    print("check_doc_links: all links resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
