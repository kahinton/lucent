"""Route-order regression tests for the sandbox template API.

``GET /api/sandboxes/templates`` is a literal path that shares its shape with
``GET /api/sandboxes/{sandbox_id}``. Starlette matches routes in registration
order, so the ``/templates`` block MUST be registered before the parameterised
sandbox route. When it was not, every ``GET /api/sandboxes/templates`` request
was swallowed by ``get_sandbox("templates")`` and 404'd — the endpoint was
documented as working but unreachable.
"""

from lucent.api.routers import sandboxes


def _template_route_index() -> int:
    paths = [getattr(route, "path", "") for route in sandboxes.router.routes]
    return paths.index("/templates")


def _sandbox_id_route_index() -> int:
    paths = [getattr(route, "path", "") for route in sandboxes.router.routes]
    return paths.index("/{sandbox_id}")


def test_template_list_route_is_registered_before_sandbox_id_route():
    """The literal /templates path must not be shadowed by /{sandbox_id}."""
    assert _template_route_index() < _sandbox_id_route_index()


def test_every_template_route_precedes_the_sandbox_id_route():
    """All /templates* paths must win over the catch-all parameterised route."""
    sandbox_id_index = _sandbox_id_route_index()
    template_indices = [
        index
        for index, route in enumerate(sandboxes.router.routes)
        if getattr(route, "path", "").startswith("/templates")
    ]
    assert template_indices, "expected template routes to be registered"
    assert max(template_indices) < sandbox_id_index


def test_template_list_route_has_a_get_handler():
    """GET /templates must resolve to a real handler, not the sandbox lookup."""
    for route in sandboxes.router.routes:
        if getattr(route, "path", "") != "/templates":
            continue
        if "GET" not in (getattr(route, "methods", set()) or set()):
            continue
        assert route.endpoint.__name__ == "list_templates"
        return
    raise AssertionError("GET /templates route not found")
