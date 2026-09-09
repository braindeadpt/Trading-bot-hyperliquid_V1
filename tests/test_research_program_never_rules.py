"""Markup guard: research_program.md's NEVER rules must keep covering the invariant surfaces.

The overnight research agent's one invariant ("the agent never touches
anything that affects execution") is enforced *socially* — by the text the
agent reads before every session. Nothing in CI stops an agent from editing
that text, so this guard pins it:

  * the invariant section exists, is non-empty, and precedes the loop spec,
  * it names every pinned surface (``config/settings.yaml``, ``src/``,
    ``main.py``, ``.env`` and the ``docs/RESEARCH_BACKLOG.md`` stop-rule),
  * every path-bearing rule line is an absolute prohibition (contains
    "never") and carries no hedging ("avoid", "when possible", "unless", ...),
  * the frozen-window guards (``assert_config_matches_preregister``,
    ``tests/test_config_hash_frozen.py``) and the "never opens a PR"
    ledger boundary are still stated.

It fails in both directions: a rule line deleted (never-absent) and a rule
softened into a suggestion or given an exception clause (never-retracted).
Softening the invariant is a reviewable act, like any gate-key drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "research_program.md"

SECTION_HEADER = "## The one invariant"
NEXT_SECTION = "## The experiment unit"

# Surfaces the NEVER rules must name explicitly, one prohibition line each.
PINNED_PATHS = (
    "config/settings.yaml",
    "src/",
    "main.py",
    ".env",
    "docs/RESEARCH_BACKLOG.md",
)

# Absolute-prohibition statements may not carry softeners. "unless" is
# included deliberately: a self-service exception ("never edit X unless the
# experiment needs it") is exactly the weakening this guard exists to catch.
HEDGE_WORDS = (
    "avoid",
    "when possible",
    "preferably",
    "prefer",
    "optionally",
    "if needed",
    "if necessary",
    "unless",
    "feel free",
    "try to",
    "best effort",
)

# Frozen-window machinery whose protection must stay spelled out.
FROZEN_WINDOW_PINS = (
    "frozen",
    "assert_config_matches_preregister",
    "test_config_hash_frozen",
)

# Gate-discipline section: the keep-list the program inherited from the
# bot's standing protections, and the two autoresearch behaviours it must
# NOT copy. Defined up front (like DOCUMENTED_GATE_KEYS) so mutation tests
# can strike them out one at a time.
KEEP_LIST_PINS = (
    "frozen window",
    "non-overlapping",
    "FDR",
    "n≥30",
    "shadow",
    "≤20 runs/night",
    "sign-flip",  # the paired window-null noise gate: KEEP = beyond luck
)
DONT_COPY_PINS = (
    "winner's curse",
    "holdout",
)
GATE_SECTION_HEADER = "## Gate discipline vs autoresearch"
GATE_SECTION_NEXT = "## Ledger and morning report"


def _program_text() -> str:
    return PROGRAM.read_text(encoding="utf-8")


def _invariant_section(text: str) -> str:
    """Slice the invariant section between its header and the next section."""
    return _section_slice(text, SECTION_HEADER, NEXT_SECTION)


def _gate_section(text: str) -> str:
    """Slice the gate-discipline section between its header and the next section."""
    return _section_slice(text, GATE_SECTION_HEADER, GATE_SECTION_NEXT)


def _section_slice(text: str, header: str, nxt_header: str) -> str:
    """Slice between two section headers; ValueError if the header is gone."""
    start = text.index(header)  # ValueError if the header is gone: that IS a failure
    nxt = text.find("\n## ", start + len(header))
    return text[start : nxt if nxt != -1 else len(text)]


def _rule_items(section: str) -> list[str]:
    """Invariant-section bullets joined into logical rule statements.

    Markdown wraps long bullets across physical lines (the NEVER and its
    target path can land on different lines), so the semantic unit is the
    whole bullet, not the line.
    """
    items: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            if current:
                items.append(" ".join(current))
            current = [line]
        elif current and line.startswith("  "):
            current.append(line.strip())
        elif current:
            items.append(" ".join(current))
            current = []
    if current:
        items.append(" ".join(current))
    return items


def _path_rule_items(section: str) -> dict[str, list[str]]:
    """Group invariant-section rule bullets by which pinned path they name."""
    grouped: dict[str, list[str]] = {p: [] for p in PINNED_PATHS}
    for item in _rule_items(section):
        for p in PINNED_PATHS:
            if p in item:
                grouped[p].append(item)
    return grouped


class NeverAbsent(AssertionError):
    """A pinned surface is no longer named by the NEVER rules."""


class NeverRetracted(AssertionError):
    """A rule line lost its absolute form (softened or exception-claused)."""


class KeepListDropped(AssertionError):
    """A keep-list protection was removed from the gate-discipline section."""


class DontCopyDropped(AssertionError):
    """A don't-copy warning was removed from the gate-discipline section."""


def _assert_gate_section_intact(text: str) -> None:
    """Pin the keep-list and the two don't-copies in the gate section."""
    section = _gate_section(text).lower()
    dropped_keep = [p for p in KEEP_LIST_PINS if p.lower() not in section]
    if dropped_keep:
        raise KeepListDropped(
            f"{PROGRAM.name}: gate-discipline keep-list lost {dropped_keep} — "
            "these are the bot's standing protections that automation must retain"
        )
    dropped_dont = [p for p in DONT_COPY_PINS if p.lower() not in section]
    if dropped_dont:
        raise DontCopyDropped(
            f"{PROGRAM.name}: don't-copy warnings lost {dropped_dont} — "
            "the section must keep warning against keep-on-any-improvement "
            "and holdout-free evaluation"
        )


def _assert_invariant_intact(text: str) -> None:
    """Pin the NEVER rules in the invariant section (surfaces + absolute form)."""
    section = _invariant_section(text)
    assert section.strip(), f"{PROGRAM.name}: invariant section is empty"

    rule_items = _path_rule_items(section)
    missing = [p for p in PINNED_PATHS if not rule_items[p]]
    if missing:
        raise NeverAbsent(
            f"{PROGRAM.name}: NEVER rules no longer name {missing}. "
            "The overnight agent's only guard is this text — restore the "
            "explicit prohibition or land the change as a reviewed human edit."
        )

    retracted: list[str] = []
    for p, items in rule_items.items():
        for item in items:
            lowered = item.lower()
            if "never" not in lowered:
                retracted.append(f"[no absolute form] {p}: {item.strip()}")
            hedges = [w for w in HEDGE_WORDS if w in lowered]
            if hedges:
                retracted.append(f"[hedged: {hedges}] {p}: {item.strip()}")
    if retracted:
        raise NeverRetracted(
            f"{PROGRAM.name}: NEVER rule lines softened — "
            + " | ".join(retracted)
        )

    absent_pins = [pin for pin in FROZEN_WINDOW_PINS if pin not in section]
    if absent_pins:
        raise NeverAbsent(
            f"{PROGRAM.name}: frozen-window protection no longer stated: {absent_pins}. "
            "The invariant must keep naming assert_config_matches_preregister and "
            "tests/test_config_hash_frozen.py by name."
        )


def test_program_declares_invariant_section_before_loop() -> None:
    text = _program_text()
    assert SECTION_HEADER in text, f"{PROGRAM.name}: '{SECTION_HEADER}' heading missing"
    assert text.index(SECTION_HEADER) < text.index(NEXT_SECTION), (
        f"{PROGRAM.name}: invariant section must precede '{NEXT_SECTION}' — "
        "the agent reads the rules before the loop spec"
    )
    _assert_invariant_intact(text)


def test_never_rules_name_every_pinned_path_with_absolute_prohibition() -> None:
    text = _program_text()
    section = _invariant_section(text)

    for path in PINNED_PATHS:
        items = _rule_items(section)
        assert any(path in item and "never" in item.lower() for item in items), (
            f"{PROGRAM.name}: no absolute NEVER rule bullet names {path!r}"
        )


def test_frozen_window_guards_are_pinned_in_the_invariant() -> None:
    section = _invariant_section(_program_text())
    for pin in FROZEN_WINDOW_PINS:
        assert pin in section, f"{PROGRAM.name}: frozen-window pin {pin!r} missing"


def test_ledger_boundary_never_opens_a_pr() -> None:
    assert "never opens a PR" in _program_text(), (
        f"{PROGRAM.name}: 'never opens a PR' boundary sentence missing — "
        "the loop must end at the ledger"
    )


def test_gate_discipline_section_keeps_full_protection_list() -> None:
    text = _program_text()
    assert GATE_SECTION_HEADER in text, (
        f"{PROGRAM.name}: '{GATE_SECTION_HEADER}' section missing — the "
        "keep-list and don't-copies must stay documented"
    )
    assert text.index(GATE_SECTION_HEADER) < text.index(GATE_SECTION_NEXT), (
        f"{PROGRAM.name}: gate-discipline section must precede '{GATE_SECTION_NEXT}'"
    )
    _assert_gate_section_intact(text)


@pytest.mark.parametrize(
    "pin",
    KEEP_LIST_PINS,
    ids=lambda p: f"keep:{p}",
)
def test_dropping_a_keep_list_pin_fails(pin: str) -> None:
    text = _program_text()
    # Strike the pin's phrase from the whole gate section, not just once —
    # the same protection can legitimately be mentioned twice.
    section = _gate_section(text)
    # Case-insensitive strike: the same protection legitimately appears in
    # both prose (lowercase) and table rows (Title Case) in the section.
    pattern = re.compile(re.escape(pin), re.IGNORECASE)
    struck = pattern.sub("(dropped-for-test)", section)
    assert struck != section, f"test bug: pin {pin!r} not found in gate section"
    with pytest.raises(KeepListDropped):
        _assert_gate_section_intact(text.replace(section, struck))


@pytest.mark.parametrize(
    "pin",
    DONT_COPY_PINS,
    ids=lambda p: f"dontcopy:{p}",
)
def test_dropping_a_dont_copy_pin_fails(pin: str) -> None:
    text = _program_text()
    section = _gate_section(text)
    pattern = re.compile(re.escape(pin), re.IGNORECASE)
    struck = pattern.sub("(dropped-for-test)", section)
    assert struck != section, f"test bug: pin {pin!r} not found in gate section"
    with pytest.raises(DontCopyDropped):
        _assert_gate_section_intact(text.replace(section, struck))


@pytest.mark.parametrize(
    "pinned",
    PINNED_PATHS,
    ids=lambda p: f"remove:{p}",
)
def test_removing_a_pinned_path_fails_never_absent(pinned: str) -> None:
    text = _program_text()
    softened = text.replace(f"`{pinned}`", "`(surface-withheld-for-test)`")
    assert softened != text, f"test bug: {pinned!r} not found backticked in {PROGRAM.name}"
    with pytest.raises(NeverAbsent):
        _assert_invariant_intact(softened)


def test_hedged_rule_fails_never_retracted() -> None:
    text = _program_text()
    softened = text.replace(
        "NEVER edit `config/settings.yaml`, `src/`, or `main.py`",
        "Avoid editing `config/settings.yaml`, `src/`, or `main.py` when possible",
    )
    assert softened != text
    with pytest.raises(NeverRetracted, match="hedged"):
        _assert_invariant_intact(softened)


def test_exception_clause_fails_never_retracted() -> None:
    text = _program_text()
    softened = text.replace(
        "NEVER touch `.env`",
        "NEVER touch `.env` unless the experiment requires it",
    )
    assert softened != text
    with pytest.raises(NeverRetracted, match="hedged"):
        _assert_invariant_intact(softened)


def test_deleted_never_form_fails_never_retracted() -> None:
    text = _program_text()
    softened = text.replace("NEVER edit `config/settings.yaml`", "Edit `config/settings.yaml`")
    assert softened != text
    with pytest.raises(NeverRetracted, match="no absolute form"):
        _assert_invariant_intact(softened)


def test_missing_section_header_fails_loudly() -> None:
    text = _program_text()
    beheaded = text.replace(SECTION_HEADER, "## (invariant removed)", 1)
    with pytest.raises((ValueError, NeverAbsent, NeverRetracted, AssertionError)):
        _assert_invariant_intact(beheaded)


def test_gutted_invariant_section_fails_never_absent() -> None:
    """Header kept but rules replaced by vague advice still trips the guard."""
    text = _program_text()
    stubbed = text.replace(
        _invariant_section(text),
        SECTION_HEADER + "\n\nThe agent should generally be careful with execution files.\n",
    )
    assert stubbed != text
    with pytest.raises(NeverAbsent):
        _assert_invariant_intact(stubbed)
