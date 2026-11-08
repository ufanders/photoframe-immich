"""Minimal async client for the parts of the Immich API this app uses."""

from __future__ import annotations

from typing import Any

import httpx


class ImmichError(RuntimeError):
    pass


class ImmichClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ImmichError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise ImmichError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def ping(self) -> bool:
        r = await self._request("GET", "/server/ping")
        return r.json().get("res") == "pong"

    # ---- catalog lookups (names -> ids) ----

    async def albums(self) -> list[dict[str, Any]]:
        """Owned and shared albums, de-duplicated."""
        seen: dict[str, dict] = {}
        for params in ({}, {"shared": "true"}):
            for a in (await self._request("GET", "/albums", params=params)).json():
                seen.setdefault(a["id"], a)
        return list(seen.values())

    async def people(self) -> list[dict[str, Any]]:
        out: list[dict] = []
        page = 1
        while True:
            data = (await self._request(
                "GET", "/people", params={"page": page, "size": 500, "withHidden": "false"}
            )).json()
            out.extend(data.get("people", []))
            if not data.get("hasNextPage"):
                return out
            page += 1

    async def tags(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/tags")).json()

    # ---- search ----

    async def _paged_search(self, path: str, body: dict, max_items: int) -> list[dict]:
        items: list[dict] = []
        page: int | None = 1
        while page and len(items) < max_items:
            size = min(1000, max_items - len(items))
            data = (await self._request(
                "POST", path, json={**body, "page": page, "size": size}
            )).json()
            assets = data.get("assets", {})
            items.extend(assets.get("items", []))
            nxt = assets.get("nextPage")
            page = int(nxt) if nxt else None
        return items[:max_items]

    async def search_metadata(self, filters: dict, max_items: int) -> list[dict]:
        return await self._paged_search("/search/metadata", filters, max_items)

    async def search_smart(self, query: str, filters: dict, max_items: int) -> list[dict]:
        # Smart search is ranked by relevance, so the first N are the best matches.
        return await self._paged_search("/search/smart", {**filters, "query": query}, max_items)

    # ---- assets ----

    async def asset(self, asset_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/assets/{asset_id}")).json()

    async def preview(self, asset_id: str) -> bytes:
        """~1440px JPEG, already orientation-corrected; avoids decoding HEIC/RAW."""
        r = await self._request(
            "GET", f"/assets/{asset_id}/thumbnail", params={"size": "preview"},
            headers={"Accept": "image/*"},
        )
        return r.content


def asset_aspect(asset: dict) -> float | None:
    """Displayed width/height of an asset, or None if unknown."""
    w, h = asset.get("width"), asset.get("height")
    exif = asset.get("exifInfo") or {}
    if not (w and h):
        w, h = exif.get("exifImageWidth"), exif.get("exifImageHeight")
        if str(exif.get("orientation") or "") in {"5", "6", "7", "8"}:
            w, h = h, w
    if w and h:
        return float(w) / float(h)
    return None


def asset_faces(asset: dict) -> list[tuple[float, float, float, float]]:
    """Face boxes as (x1, y1, x2, y2) fractions of the displayed image."""
    boxes = []
    faces = [f for p in asset.get("people") or [] for f in p.get("faces") or []]
    faces += asset.get("unassignedFaces") or []
    for f in faces:
        iw, ih = f.get("imageWidth"), f.get("imageHeight")
        if not (iw and ih):
            continue
        boxes.append((f["boundingBoxX1"] / iw, f["boundingBoxY1"] / ih,
                      f["boundingBoxX2"] / iw, f["boundingBoxY2"] / ih))
    return boxes
