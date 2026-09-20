"""Application-facing facade for daemon identity database resolution."""

from __future__ import annotations

from lucent.db.daemon_identity import (
    ensure_daemon_service_user,
    resolve_daemon_service_user,
)


__all__ = ["ensure_daemon_service_user", "resolve_daemon_service_user"]
