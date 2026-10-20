"""Routers package export."""

from server.routers.auth import router as auth_router
from server.routers.tasks import router as tasks_router
from server.routers.settings import router as settings_router

__all__ = ["auth_router", "tasks_router", "settings_router"]
