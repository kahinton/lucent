"""API package for Lucent admin dashboard."""

# Note: app and create_app are imported lazily to avoid circular imports
# with lucent.web.routes. Use: from lucent.api.app import create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in ("app", "create_app"):
        from lucent.api.app import app, create_app
        globals()["app"] = app
        globals()["create_app"] = create_app
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
