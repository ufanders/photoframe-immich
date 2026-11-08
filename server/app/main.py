"""HTTP API consumed by the ESPHome frame, plus a small preview page for tuning."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import Settings, load_settings
from .immich import ImmichClient, ImmichError, asset_faces
from .render import Rendered, render
from .selector import ConfigError, NoPhotoError, Selector

log = logging.getLogger("photoframe")
NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class Frame:
    asset_id: str
    source: str
    rendered: Rendered = field(repr=False)
    original: bytes = field(repr=False)
    info: dict
    created: float

    def meta(self) -> dict:
        return {"asset_id": self.asset_id, "source": self.source,
                "created": self.created, **self.info}


class FrameService:
    """Keeps the current frame and pre-renders the next one in the background."""

    def __init__(self, settings: Settings, client: ImmichClient, selector: Selector):
        self.settings = settings
        self.client = client
        self.selector = selector
        self.current: Frame | None = None
        self.upcoming: Frame | None = None
        self.last_error: str | None = None
        self.battery: dict | None = None
        self._lock = asyncio.Lock()
        self._prefetch: asyncio.Task | None = None
        self._dir = settings.state_dir
        self._load_saved()

    # Persist the last frame so a restart can still serve something if Immich is down.
    def _load_saved(self) -> None:
        png, meta = self._dir / "current.png", self._dir / "current.json"
        if png.exists() and meta.exists():
            try:
                m = json.loads(meta.read_text())
                self.current = Frame(m["asset_id"], m["source"], Rendered(png.read_bytes(), png.read_bytes()),
                                     b"", m.get("info", {}), m.get("created", 0))
            except (OSError, ValueError, KeyError):
                log.warning("ignoring unreadable saved frame")

    def _save(self, f: Frame) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / "current.png").write_bytes(f.rendered.frame_png)
            (self._dir / "current.json").write_text(json.dumps(
                {"asset_id": f.asset_id, "source": f.source, "info": f.info, "created": f.created}))
        except OSError as e:
            log.warning("could not save frame: %s", e)

    async def build(self) -> Frame:
        pick = await self.selector.pick()
        asset = await self.client.asset(pick.asset_id)
        original = await self.client.preview(pick.asset_id)
        rendered = await asyncio.to_thread(render, original, self.settings.render, asset_faces(asset))
        exif = asset.get("exifInfo") or {}
        info = {
            "filename": asset.get("originalFileName"),
            "taken": exif.get("dateTimeOriginal") or asset.get("localDateTime"),
            "place": ", ".join(p for p in (exif.get("city"), exif.get("country")) if p) or None,
        }
        log.info("rendered %s from %r", pick.asset_id, pick.source)
        return Frame(pick.asset_id, pick.source, rendered, original, info, time.time())

    async def _prefetch_next(self) -> None:
        try:
            self.upcoming = await self.build()
        except (ImmichError, NoPhotoError, ConfigError) as e:
            log.warning("prefetch failed: %s", e)
        except Exception:  # never let a background task die silently
            log.exception("prefetch crashed")

    def start_prefetch(self) -> None:
        if self._prefetch is None or self._prefetch.done():
            self._prefetch = asyncio.create_task(self._prefetch_next())

    async def advance(self) -> Frame:
        async with self._lock:
            if self._prefetch and not self._prefetch.done():
                await asyncio.shield(self._prefetch)
            frame, self.upcoming = self.upcoming, None
            if frame is None:
                frame = await self.build()
            self.current = frame
            self.last_error = None
            self._save(frame)
        self.start_prefetch()
        return frame


def create_app(settings: Settings | None = None, client: ImmichClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or load_settings()
        c = client or ImmichClient(s.immich_url, s.immich_api_key)
        app.state.settings = s
        app.state.client = c
        app.state.selector = Selector(c, s)
        app.state.frames = FrameService(s, c, app.state.selector)
        app.state.frames.start_prefetch()
        yield
        await c.aclose()

    app = FastAPI(title="photoframe-immich", lifespan=lifespan)

    def svc(request: Request) -> FrameService:
        return request.app.state.frames

    def png(data: bytes, extra: dict | None = None) -> Response:
        return Response(data, media_type="image/png", headers={**NO_STORE, **(extra or {})})

    @app.get("/frame.png")
    async def frame_png(request: Request, advance: bool = True,
                        battery: float | None = Query(None, description="battery % reported by the device")):
        """The device calls this on every wake. Advances to the next photo by default."""
        fs = svc(request)
        if battery is not None:
            fs.battery = {"percent": battery, "at": time.time()}
        if advance or fs.current is None:
            try:
                await fs.advance()
            except (ImmichError, NoPhotoError, ConfigError) as e:
                fs.last_error = str(e)
                log.error("could not advance: %s", e)
                if fs.current is None:
                    raise HTTPException(503, f"no photo available: {e}")
                return png(fs.current.rendered.frame_png, {"X-Photoframe-Error": str(e)[:200]})
        return png(fs.current.rendered.frame_png)

    @app.get("/config.json")
    async def device_config(request: Request,
                            battery: float | None = Query(None, description="battery % reported by the device")):
        """Fetched by the device at the start of every wake."""
        if battery is not None:
            svc(request).battery = {"percent": battery, "at": time.time()}
        return JSONResponse(request.app.state.settings.device.model_dump(), headers=NO_STORE)

    @app.post("/next")
    async def next_photo(request: Request):
        try:
            frame = await svc(request).advance()
        except (ImmichError, NoPhotoError, ConfigError) as e:
            raise HTTPException(503, str(e))
        return frame.meta()

    @app.get("/current/{kind}")
    async def current_image(request: Request, kind: str):
        f = svc(request).current
        if f is None:
            raise HTTPException(404, "nothing shown yet")
        if kind == "frame.png":
            return png(f.rendered.frame_png)
        if kind == "simulated.png":
            return png(f.rendered.simulated_png)
        if kind == "original.jpg" and f.original:
            return Response(f.original, media_type="image/jpeg", headers=NO_STORE)
        raise HTTPException(404)

    @app.get("/status")
    async def status(request: Request):
        fs = svc(request)
        sel: Selector = request.app.state.selector
        return {
            "current": fs.current.meta() if fs.current else None,
            "upcoming": fs.upcoming.meta() if fs.upcoming else None,
            "last_error": fs.last_error,
            "battery": fs.battery,
            "sources": [{"name": s.name, "weight": s.weight,
                         "candidates": len(sel._cache[s.name][1]) if s.name in sel._cache else None}
                        for s in request.app.state.settings.sources],
        }

    @app.get("/catalog")
    async def catalog(request: Request):
        """Names you can use in config.yaml: albums, people, tags."""
        c: ImmichClient = request.app.state.client
        try:
            albums, people, tags = await asyncio.gather(c.albums(), c.people(), c.tags())
        except ImmichError as e:
            raise HTTPException(502, str(e))
        return {
            "albums": sorted(({"name": a["albumName"], "photos": a.get("assetCount")} for a in albums),
                             key=lambda a: a["name"].casefold()),
            "people": sorted((p["name"] for p in people if p.get("name")), key=str.casefold),
            "tags": sorted((t.get("value") or t.get("name") for t in tags), key=str.casefold),
        }

    @app.post("/refresh-sources")
    async def refresh_sources(request: Request):
        """Drop cached search results and name lookups (e.g. after adding photos to an album)."""
        request.app.state.selector.invalidate()
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def preview_page():
        return PREVIEW_HTML

    return app


PREVIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photoframe preview</title>
<style>
 body{font:14px system-ui,sans-serif;margin:0;padding:16px;background:#f4f4f2;color:#222}
 h1{font-size:18px;margin:0 0 12px}
 .row{display:flex;flex-wrap:wrap;gap:16px}
 figure{margin:0;flex:1 1 380px;max-width:800px}
 figure img{width:100%;display:block;background:#ddd;border:1px solid #ccc}
 figcaption{color:#666;margin-top:4px}
 button{font:inherit;padding:6px 14px;margin-right:8px}
 pre{background:#fff;border:1px solid #ddd;padding:8px;overflow:auto;max-height:300px}
</style></head><body>
<h1>Photoframe preview</h1>
<p><button id="next">Next photo</button><button id="refresh">Re-query Immich</button><span id="msg"></span></p>
<div class="row">
 <figure><img id="sim"><figcaption>Simulated panel (measured ink colours)</figcaption></figure>
 <figure><img id="orig"><figcaption>Original from Immich</figcaption></figure>
</div>
<h2>Status</h2><pre id="status"></pre>
<h2>Names available for config.yaml</h2><pre id="catalog">loading…</pre>
<script>
const $ = id => document.getElementById(id);
async function load(){
  const t = Date.now();
  $('sim').src = 'current/simulated.png?t=' + t;
  $('orig').src = 'current/original.jpg?t=' + t;
  $('status').textContent = JSON.stringify(await (await fetch('status')).json(), null, 2);
}
$('next').onclick = async () => {
  $('msg').textContent = ' working…';
  const r = await fetch('next', {method:'POST'});
  $('msg').textContent = r.ok ? '' : ' ' + (await r.json()).detail;
  load();
};
$('refresh').onclick = async () => { await fetch('refresh-sources', {method:'POST'}); $('msg').textContent=' cache cleared'; };
fetch('catalog').then(r => r.json()).then(j => $('catalog').textContent = JSON.stringify(j, null, 2))
  .catch(e => $('catalog').textContent = String(e));
load();
</script></body></html>"""


def _make_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


app = _make_default_app()
