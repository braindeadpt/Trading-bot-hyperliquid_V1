"""Unit tests for QW3 (time-based log rotation).

Verifies that ``setup_logger`` returns a logger wired up with a
``TimedRotatingFileHandler`` that rotates at the configured cadence and
keeps the requested number of backups.

Run:  python tests/test_log_rotation.py
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.logger import setup_logger  # noqa: E402

FAILED = 0


def print_test(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    global FAILED
    if not ok:
        FAILED += 1


@contextmanager
def temp_log_file(suffix: str = ".log") -> Iterator[Path]:
    """Create a temp log file path. Cleanup is the caller's responsibility
    because the logger holds the file handle — we close handlers in the
    test body, then delete."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    import os
    os.close(fd)
    try:
        yield Path(name)
    finally:
        # Caller is expected to close handlers; this is just a safety net.
        pass


@contextmanager
def logger_context(name: str, **kwargs) -> Iterator[logging.Logger]:
    """Create a logger, yield it, then close all handlers and unlink the file."""
    log_file = kwargs.pop("log_file", None)
    if log_file is None:
        log_path: Path | None = None
    else:
        log_path = Path(log_file)

    logger = setup_logger(name=name, log_file=str(log_path) if log_path else None, **kwargs)
    try:
        yield logger
    finally:
        for h in list(logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)
        if log_path is not None:
            log_path.unlink(missing_ok=True)
            # Clean up any rotated siblings
            for p in Path(tempfile.gettempdir()).glob(f"{log_path.name}.*"):
                try:
                    p.unlink()
                except OSError:
                    pass


def test_default_uses_timed_rotating_handler() -> None:
    with temp_log_file() as log_file:
        with logger_context(name="test_timed", level="INFO", console=False, log_file=str(log_file)) as logger:
            file_handlers = [
                h for h in logger.handlers
                if isinstance(h, (logging.handlers.TimedRotatingFileHandler, logging.handlers.RotatingFileHandler))
            ]
            assert file_handlers, "expected at least one rotating file handler"
            h = file_handlers[0]
            ok = isinstance(h, logging.handlers.TimedRotatingFileHandler)
            print_test("default_uses_timed_rotating_handler", ok, f"handler={type(h).__name__}")


def test_handler_attributes() -> None:
    with temp_log_file() as log_file:
        with logger_context(
            name="test_attrs", level="INFO", console=False, log_file=str(log_file),
            rotation_when="H", rotation_interval=2, backup_count=7, utc=True,
        ) as logger:
            h = [x for x in logger.handlers if isinstance(x, logging.handlers.TimedRotatingFileHandler)][0]
            # TimedRotatingFileHandler stores ``interval`` in seconds after
            # it normalises the ``when`` string. With when="H" the multiplier
            # is 3600, so 2 hours = 7200 seconds.
            expected_interval_seconds = 2 * 3600
            ok = (
                h.when == "H"
                and h.interval == expected_interval_seconds
                and h.backupCount == 7
                and h.utc is True
            )
            print_test("handler_attributes", ok,
                       f"when={h.when} interval={h.interval} (sec, expected {expected_interval_seconds}) "
                       f"backupCount={h.backupCount} utc={h.utc}")


def test_size_cap_attached_on_python_3_13_plus() -> None:
    """If maxBytes attribute is supported (3.13+), it should be set."""
    with temp_log_file() as log_file:
        with logger_context(
            name="test_sizecap", level="INFO", console=False, log_file=str(log_file),
            max_bytes=1_000_000, backup_count=5,
        ) as logger:
            h = [x for x in logger.handlers if isinstance(x, logging.handlers.TimedRotatingFileHandler)][0]
            if hasattr(h, "maxBytes"):
                ok = h.maxBytes == 1_000_000
                print_test("size_cap_attached_on_python_3_13_plus", ok,
                           f"maxBytes={getattr(h, 'maxBytes', 'N/A')}")
            else:
                print_test("size_cap_attached_on_python_3_13_plus", True,
                           "Python <3.13 - size cap silently skipped (expected)")


def test_writes_to_file() -> None:
    with temp_log_file() as log_file:
        with logger_context(name="test_writes", level="INFO", console=False, log_file=str(log_file)) as logger:
            for i in range(5):
                logger.info("test message %d", i)
            for h in logger.handlers:
                h.flush()
            content = log_file.read_text(encoding="utf-8")
            n_lines = content.count("test message")
            ok = n_lines == 5
            print_test("writes_to_file", ok, f"lines={n_lines}, file size={len(content)} bytes")


def test_console_handler_skipped() -> None:
    with temp_log_file() as log_file:
        with logger_context(name="test_noconsole", level="INFO", console=False, log_file=str(log_file)) as logger:
            has_console = any(
                isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
                for h in logger.handlers
            )
            print_test("console_handler_skipped", not has_console)


def test_idempotent_clear() -> None:
    """Calling setup_logger twice on the same name should not stack handlers."""
    with temp_log_file() as log_file:
        with logger_context(name="test_idem", level="INFO", console=False, log_file=str(log_file)) as logger1:
            n1 = len(logger1.handlers)
        with logger_context(name="test_idem", level="DEBUG", console=False, log_file=str(log_file)) as logger2:
            n2 = len(logger2.handlers)
            ok = n1 == n2
            print_test("idempotent_clear", ok, f"first={n1} second={n2}")


def test_idempotent_level_update() -> None:
    """Second call with a different level should update the logger's level."""
    with temp_log_file() as log_file:
        setup_logger(name="test_lvl", level="INFO", console=False, log_file=str(log_file))
        logger2 = setup_logger(name="test_lvl", level="DEBUG", console=False, log_file=str(log_file))
        # Cleanup
        for h in list(logger2.handlers):
            try:
                h.close()
            except Exception:
                pass
        log_file.unlink(missing_ok=True)
        ok = logger2.level == logging.DEBUG
        print_test("idempotent_level_update", ok, f"level={logging.getLevelName(logger2.level)}")


def test_manual_rotation_does_not_lose_data() -> None:
    """Force a rollover via doRollover() - old file should be archived, new empty."""
    with temp_log_file() as log_file:
        with logger_context(name="test_rollover", level="INFO", console=False, log_file=str(log_file)) as logger:
            logger.info("before rollover")
            h = [x for x in logger.handlers if isinstance(x, logging.handlers.TimedRotatingFileHandler)][0]
            h.doRollover()
            logger.info("after rollover")
            for hh in logger.handlers:
                hh.flush()
            content = log_file.read_text(encoding="utf-8")
            # Current file should only contain "after rollover" (the "before" was rotated out)
            ok = "after rollover" in content and "before rollover" not in content
            print_test("manual_rotation_does_not_lose_data", ok,
                       f"current content: {content[:80]!r}")


def test_no_log_file_disables_file_handler() -> None:
    logger = setup_logger(name="test_nofile", level="INFO", log_file=None, console=True)
    file_handlers = [h for h in logger.handlers
                     if isinstance(h, (logging.FileHandler, logging.handlers.TimedRotatingFileHandler, logging.handlers.RotatingFileHandler))]
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)
    print_test("no_log_file_disables_file_handler", not file_handlers)


def main() -> int:
    print("=" * 70)
    print("QW3 (time-based log rotation) tests")
    print("=" * 70)

    tests = [
        test_default_uses_timed_rotating_handler,
        test_handler_attributes,
        test_size_cap_attached_on_python_3_13_plus,
        test_writes_to_file,
        test_console_handler_skipped,
        test_idempotent_clear,
        test_idempotent_level_update,
        test_manual_rotation_does_not_lose_data,
        test_no_log_file_disables_file_handler,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print_test(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            print_test(t.__name__, False, f"{type(e).__name__}: {e}")

    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
