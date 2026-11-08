import io

import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from app.config import PURE_COLORS, Settings, Source
from app.immich import ImmichClient, ImmichError
from app.main import create_app

BASE = "http://immich.test/api"


def _jpeg():
    buf = io.BytesIO()
    Image.fromarray(np.random.default_rng(0).integers(0, 255, (300, 400, 3), np.uint8)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def immich():
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        r.get("/albums").respond(json=[{"id": "a1", "albumName": "Family", "assetCount": 2}])
        r.get("/people").respond(json={"people": [{"id": "p1", "name": "Alice"}], "hasNextPage": False})
        r.get("/tags").respond(json=[])
        r.post("/search/metadata").respond(json={"assets": {"items": [
            {"id": "x1", "exifInfo": {"exifImageWidth": 400, "exifImageHeight": 300}},
            {"id": "x2", "exifInfo": {"exifImageWidth": 400, "exifImageHeight": 300}},
        ], "nextPage": None}})
        for i in ("x1", "x2"):
            r.get(f"/assets/{i}").respond(json={"id": i, "originalFileName": f"{i}.jpg",
                                                 "exifInfo": {"city": "Oslo", "country": "Norway"}})
            r.get(f"/assets/{i}/thumbnail").respond(content=_jpeg(), headers={"content-type": "image/jpeg"})
        yield r


def _app(tmp_path):
    s = Settings(immich_url="http://immich.test", immich_api_key="k", state_dir=tmp_path,
                 sources=[Source(name="fam", albums=["Family"])])
    return create_app(s, ImmichClient(s.immich_url, s.immich_api_key))


def test_frame_flow(immich, tmp_path):
    with TestClient(_app(tmp_path)) as c:
        c.get("/config.json?battery=80")
        r = c.get("/frame.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        assert im.size == (800, 480)
        assert {tuple(p) for p in np.asarray(im).reshape(-1, 3)} <= set(PURE_COLORS.values())

        first = c.get("/status").json()["current"]["asset_id"]
        assert c.get("/frame.png?advance=false").content == r.content
        assert c.get("/status").json()["current"]["asset_id"] == first
        c.get("/frame.png")
        st = c.get("/status").json()
        assert st["current"]["asset_id"] != first and st["battery"]["percent"] == 80
        assert st["current"]["place"] == "Oslo, Norway"

        assert c.get("/config.json").json()["sleep_minutes"] == 60
        assert c.get("/catalog").json()["albums"] == [{"name": "Family", "photos": 2}]
        assert c.get("/current/original.jpg").status_code == 200
        assert c.post("/next").status_code == 200
    assert (tmp_path / "current.png").exists()


def test_serves_last_frame_when_immich_down(immich, tmp_path):
    with TestClient(_app(tmp_path)) as c:
        good = c.get("/frame.png").content
    immich.post("/search/metadata").mock(side_effect=httpx.ConnectError("down"))
    immich.get("/assets/x1/thumbnail").mock(side_effect=httpx.ConnectError("down"))
    immich.get("/assets/x2/thumbnail").mock(side_effect=httpx.ConnectError("down"))
    with TestClient(_app(tmp_path)) as c:  # new process: only the saved frame survives
        r = c.get("/frame.png")
        assert r.status_code == 200 and r.content == good
        assert "X-Photoframe-Error" in r.headers


def test_no_photo_at_all_is_503(tmp_path):
    with respx.mock(base_url=BASE) as r:
        r.route().mock(side_effect=httpx.ConnectError("down"))
        with TestClient(_app(tmp_path)) as c:
            assert c.get("/frame.png").status_code == 503


async def test_client_pages_search():
    with respx.mock(base_url=BASE) as r:
        route = r.post("/search/metadata")
        route.side_effect = [
            httpx.Response(200, json={"assets": {"items": [{"id": "1"}], "nextPage": "2"}}),
            httpx.Response(200, json={"assets": {"items": [{"id": "2"}], "nextPage": None}}),
        ]
        c = ImmichClient("http://immich.test/", "k")
        items = await c.search_metadata({"type": "IMAGE"}, 10)
        assert [i["id"] for i in items] == ["1", "2"]
        assert route.calls[0].request.headers["x-api-key"] == "k"
        r.get("/server/ping").respond(401, text="nope")
        with pytest.raises(ImmichError, match="401"):
            await c.ping()
