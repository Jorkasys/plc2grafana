"""Тонкий клиент HTTP API Grafana.

Нужен для того, что нельзя сделать через provisioning-файлы: узнать список
уже созданных дашбордов, проверить датасорс, дождаться готовности сервера.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("app.grafana.client")


class GrafanaClient:
    def __init__(self, url: str, user: str = "", password: str = "",
                 token: str = "", timeout: float = 8.0) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.token = token
        self.timeout = timeout

    def _auth(self) -> dict[str, Any]:
        if self.token:
            return {"headers": {"Authorization": f"Bearer {self.token}"}}
        if self.user:
            return {"auth": (self.user, self.password)}
        return {}

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, **self._auth()) as client:
            response = await client.get(f"{self.url}{path}")
            response.raise_for_status()
            return response.json()

    async def health(self) -> dict[str, Any] | None:
        try:
            return await self._get("/api/health")
        except Exception:  # noqa: BLE001
            return None

    async def dashboards(self, tag: str = "plc2grafana") -> list[dict[str, Any]]:
        """Список дашбордов, созданных приложением."""
        try:
            items = await self._get(f"/api/search?tag={tag}&type=dash-db&limit=200")
        except Exception as exc:  # noqa: BLE001
            log.debug("не удалось получить список дашбордов: %s", exc)
            return []
        return [
            {
                "uid": item.get("uid"),
                "title": item.get("title"),
                "url": f"{self.url}{item.get('url', '')}",
                "path": item.get("url", ""),
                "folder": item.get("folderTitle") or "",
                "tags": item.get("tags") or [],
            }
            for item in items
        ]

    async def datasource_ok(self, uid: str) -> bool:
        try:
            await self._get(f"/api/datasources/uid/{uid}")
            return True
        except Exception:  # noqa: BLE001
            return False

    def embed_url(self, path: str, *, kiosk: bool = True, theme: str = "dark",
                  refresh: str = "5s", time_from: str = "now-30m") -> str:
        """Ссылка на дашборд для вставки в iframe внутри нашего интерфейса."""
        params = ["orgId=1", f"theme={theme}", f"refresh={refresh}",
                  f"from={time_from}", "to=now"]
        if kiosk:
            params.append("kiosk")
        sep = "&" if "?" in path else "?"
        return f"{self.url}{path}{sep}{'&'.join(params)}"
