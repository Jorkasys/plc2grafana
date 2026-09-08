"""Веб-приложение: HTTP API + статика интерфейса."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .context import ctx
from .db import DatabaseError
from .grafana.manager import GrafanaError
from .registry import RegistryError
from .settings import WEB_DIR

log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await ctx.startup()
    try:
        yield
    finally:
        await ctx.shutdown()


app = FastAPI(
    title="plc2grafana",
    description="Локальное приложение: опрос устройств по Modbus, хранение "
                "в PostgreSQL и автоматические дашборды Grafana",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.include_router(api_router)


@app.exception_handler(RegistryError)
async def _registry_error(_request: Request, exc: RegistryError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(DatabaseError)
async def _database_error(_request: Request, exc: DatabaseError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(GrafanaError)
async def _grafana_error(_request: Request, exc: GrafanaError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok", "version": __version__}


# Интерфейс отдаётся статикой. Монтируется последним, чтобы не перехватывать
# маршруты API: совпадения ищутся в порядке регистрации.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
