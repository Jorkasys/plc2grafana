"""HTTP API веб-интерфейса."""

from fastapi import APIRouter

from . import data, devices, grafana_api, system, twin

router = APIRouter(prefix="/api")
router.include_router(system.router)
router.include_router(devices.router)
router.include_router(data.router)
router.include_router(twin.router)
router.include_router(grafana_api.router)

__all__ = ["router"]
