"""Dashboard token resolution and validation."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DashboardAuthConfig:
    """Resolved dashboard authentication settings."""

    enabled: bool
    token: Optional[str]


def resolve_dashboard_auth(config: Dict[str, Any]) -> DashboardAuthConfig:
    """Resolve token and whether auth is required.

    Token sources (first match wins):
      1. ``BOT_DASHBOARD_TOKEN`` / ``DASHBOARD_TOKEN`` env
      2. ``dashboard.token`` / ``dashboard.password`` from bot config
      3. ``dashboard_token`` / ``dashboard_password`` passed in dashboard cfg dict
    """
    token = (
        os.environ.get("BOT_DASHBOARD_TOKEN", "").strip()
        or os.environ.get("DASHBOARD_TOKEN", "").strip()
        or str(config.get("token") or "").strip()
        or str(config.get("dashboard_token") or "").strip()
        or str(config.get("password") or "").strip()
        or str(config.get("dashboard_password") or "").strip()
    ) or None

    # Hash-neutral deployment switch: DASHBOARD_AUTH_ENABLED is read directly
    # from the environment (NOT ``BOT_``-prefixed, so it never enters the
    # effective config and cannot trip the Fase 10 frozen ``config_hash``
    # assert — ``auth_enabled`` is part of the hash, the token is not).
    # Precedence: env var > ``dashboard.auth_enabled`` > token presence.
    env_auth = os.environ.get("DASHBOARD_AUTH_ENABLED", "").strip().lower()
    if env_auth in ("1", "true", "yes"):
        explicit = True
    elif env_auth in ("0", "false", "no"):
        explicit = False
    else:
        explicit = config.get("auth_enabled")
    if explicit is None:
        enabled = bool(token)
    else:
        enabled = bool(explicit)

    if enabled and not token:
        token = secrets.token_urlsafe(24)
        logger.info(
            "Dashboard auth ON — ephemeral token generated (set BOT_DASHBOARD_TOKEN "
            "or dashboard.password in config for persistence): %s",
            token,
        )
    elif enabled and token:
        logger.info("Dashboard auth ON — token configured, paste at login prompt")

    return DashboardAuthConfig(enabled=enabled, token=token)


def validate_dashboard_token(provided: Optional[str], expected: Optional[str]) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)
