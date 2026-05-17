"""FastAPI entrypoint that mounts scillm exec routes before the legacy catch-all.

The existing ``scillm.proxy.app`` module owns the main proxy app and registers a
catch-all route at the end.  Importing that app and then including a router would
append exec routes after the catch-all, making them unreachable.  This entrypoint
includes the exec router, then moves those routes in front of the catch-all while
preserving the existing app lifespan and all existing endpoints.
"""

from __future__ import annotations

from scillm.proxy.app import _check_auth, app
from scillm.proxy.exec_api import create_exec_router

_EXEC_ROUTE_PREFIX = "/v1/scillm/exec"
_CATCH_ALL_PATH = "/{path:path}"


def _mount_exec_routes_before_catch_all() -> None:
    before_count = len(app.router.routes)
    app.include_router(create_exec_router(_check_auth), prefix="/v1/scillm")
    new_routes = app.router.routes[before_count:]
    old_routes = app.router.routes[:before_count]

    catch_index = next(
        (idx for idx, route in enumerate(old_routes) if getattr(route, "path", None) == _CATCH_ALL_PATH),
        len(old_routes),
    )
    app.router.routes = old_routes[:catch_index] + new_routes + old_routes[catch_index:]


if not any(str(getattr(route, "path", "")).startswith(_EXEC_ROUTE_PREFIX) for route in app.router.routes):
    _mount_exec_routes_before_catch_all()
