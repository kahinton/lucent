"""Web routes package — assembles sub-routers into a single router.

Backward-compatible: ``from lucent.web.routes import router`` still works,
as does ``from lucent.web.routes import _check_csrf, start_impersonation``.
"""

from fastapi import APIRouter

from . import (
    admin,
    audit,
    auth,
    daemon,
    dashboard,
    definitions,
    groups,
    memories,
    requests_routes,
    sandboxes,
    schedules,
    settings,
)

# Re-export symbols that tests and other modules import directly
from ._shared import _check_csrf, get_user_context  # noqa: F401
from .admin import start_impersonation  # noqa: F401

router = APIRouter()

# Mount all sub-routers (order matters for route matching)
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(definitions.router)
router.include_router(daemon.router)
router.include_router(memories.router)
router.include_router(audit.router)
router.include_router(admin.router)
router.include_router(groups.router)
router.include_router(sandboxes.router)
router.include_router(settings.router)
router.include_router(requests_routes.router)
router.include_router(schedules.router)
