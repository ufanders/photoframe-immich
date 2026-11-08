import random
from collections import Counter

import pytest

from app.config import Settings, Source
from app.immich import asset_aspect, asset_faces
from app.selector import Candidate, ConfigError, NoPhotoError, Selector


class FakeClient:
    def __init__(self, metadata=None, smart=None):
        self.metadata = metadata or {}
        self.smart = smart or []
        self.bodies = []

    async def albums(self):
        return [{"id": "a1", "albumName": "Family"}, {"id": "a2", "albumName": "Trips"}]

    async def people(self):
        return [{"id": "p1", "name": "Alice"}, {"id": "p2", "name": "Bob"}, {"id": "p3", "name": ""}]

    async def tags(self):
        return [{"id": "t1", "name": "Hawaii", "value": "Travel/Hawaii"}]

    async def search_metadata(self, body, max_items):
        self.bodies.append(body)
        key = tuple(body.get("albumIds", [])) + tuple(body.get("personIds", []))
        return self.metadata.get(key, [])[:max_items]

    async def search_smart(self, query, body, max_items):
        self.bodies.append({**body, "query": query})
        return self.smart[:max_items]


def _asset(i, w=4000, h=3000):
    return {"id": i, "exifInfo": {"exifImageWidth": w, "exifImageHeight": h}}


def _sel(client, tmp_path, **kw):
    s = Settings(immich_url="x", immich_api_key="k", state_dir=tmp_path, **kw)
    return Selector(client, s, rng=random.Random(1))


async def test_any_of_albums_is_union_of_queries(tmp_path):
    c = FakeClient({("a1",): [_asset("x")], ("a2",): [_asset("y"), _asset("x")]})
    sel = _sel(c, tmp_path)
    cands = await sel.candidates(Source(name="s", albums=["family", "Trips"]))
    assert {c.id for c in cands} == {"x", "y"}
    assert all(b["type"] == "IMAGE" and b["visibility"] == "timeline" for b in c.bodies)


async def test_people_all_is_single_query(tmp_path):
    c = FakeClient()
    sel = _sel(c, tmp_path)
    bodies = await sel.queries(Source(name="s", people=["Alice", "Bob"], people_match="all"))
    assert [b["personIds"] for b in bodies] == [["p1", "p2"]]
    bodies = await sel.queries(Source(name="s", people=["Alice", "Bob"]))
    assert [b["personIds"] for b in bodies] == [["p1"], ["p2"]]


async def test_filters_and_rating_expansion(tmp_path):
    from datetime import date
    sel = _sel(FakeClient(), tmp_path)
    bodies = await sel.queries(Source(name="s", favorites=True, tags=["travel/hawaii"],
                                      min_rating=4, taken_after=date(2020, 1, 2), country="Norway"))
    assert sorted(b["rating"] for b in bodies) == [4, 5]
    b = bodies[0]
    assert b["isFavorite"] is True and b["tagIds"] == ["t1"] and b["country"] == "Norway"
    assert b["takenAfter"].startswith("2020-01-02")


async def test_unknown_name_is_reported(tmp_path):
    sel = _sel(FakeClient(), tmp_path)
    with pytest.raises(ConfigError, match="Nope"):
        await sel.queries(Source(name="s", albums=["Nope"]))


async def test_smart_query_uses_smart_search(tmp_path):
    c = FakeClient(smart=[_asset("s1")])
    sel = _sel(c, tmp_path)
    cands = await sel.candidates(Source(name="s", smart_query="beach"))
    assert [x.id for x in cands] == ["s1"]
    assert c.bodies[-1]["query"] == "beach" and "withExif" not in c.bodies[-1]


async def test_weighting_and_no_repeat(tmp_path):
    c = FakeClient({("a1",): [_asset(f"f{i}") for i in range(50)],
                    ("a2",): [_asset(f"t{i}") for i in range(50)]})
    sel = _sel(c, tmp_path, history_size=40, sources=[
        Source(name="fam", weight=3, albums=["Family"]),
        Source(name="trip", weight=1, albums=["Trips"]),
    ])
    picks = [await sel.pick() for _ in range(400)]
    counts = Counter(p.source for p in picks)
    assert 2.0 < counts["fam"] / counts["trip"] < 4.5
    ids = [p.asset_id for p in picks]
    for i in range(1, len(ids)):  # history window is min(40, pool//2)=25 picks
        assert ids[i] not in ids[max(0, i - 20):i]


async def test_landscape_only_and_failing_sources(tmp_path):
    c = FakeClient({("a1",): [_asset("portrait", 3000, 4000)], ("a2",): [_asset("land")]})
    sel = _sel(c, tmp_path, orientation="landscape_only", sources=[
        Source(name="bad", albums=["Missing"]),
        Source(name="portraits", albums=["Family"]),
        Source(name="ok", albums=["Trips"]),
    ])
    for _ in range(10):
        assert (await sel.pick()).asset_id == "land"
    sel.settings.sources = sel.settings.sources[:2]
    with pytest.raises(NoPhotoError):
        await sel.pick()


def test_history_persists(tmp_path):
    sel = _sel(FakeClient(), tmp_path)
    sel.history.add("abc")
    assert "abc" in _sel(FakeClient(), tmp_path).history.recent(5)


def test_asset_helpers():
    assert asset_aspect({"exifInfo": {"exifImageWidth": 4000, "exifImageHeight": 3000, "orientation": "6"}}) == 0.75
    assert asset_aspect({}) is None
    faces = asset_faces({"people": [{"faces": [{"imageWidth": 100, "imageHeight": 50,
                                                "boundingBoxX1": 10, "boundingBoxY1": 5,
                                                "boundingBoxX2": 20, "boundingBoxY2": 25}]}]})
    assert faces == [(0.1, 0.1, 0.2, 0.5)]
    assert Candidate("a", None).aspect is None
