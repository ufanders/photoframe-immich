"""Choose the next photo: weighted pick of a source, then a random fresh photo from it."""

from __future__ import annotations

import itertools
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, Source
from .immich import ImmichClient, ImmichError, asset_aspect

log = logging.getLogger(__name__)

BASE_FILTERS = {"type": "IMAGE", "visibility": "timeline", "withExif": True}


class ConfigError(ValueError):
    pass


class NoPhotoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    id: str
    aspect: float | None


@dataclass(frozen=True)
class Pick:
    asset_id: str
    source: str


class History:
    """Recently shown asset ids, persisted so restarts don't repeat photos."""

    def __init__(self, path: Path | None, size: int):
        self._path = path
        self._ids: deque[str] = deque(maxlen=max(size, 1) if size else 1)
        self._enabled = size > 0
        if path and path.exists():
            try:
                self._ids.extend(json.loads(path.read_text()))
            except (ValueError, OSError):
                log.warning("ignoring unreadable history file %s", path)

    def add(self, asset_id: str) -> None:
        if not self._enabled:
            return
        if asset_id in self._ids:
            self._ids.remove(asset_id)
        self._ids.append(asset_id)
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text(json.dumps(list(self._ids)))
            except OSError as e:
                log.warning("could not save history: %s", e)

    def recent(self, n: int) -> set[str]:
        if n <= 0 or not self._enabled:
            return set()
        return set(list(self._ids)[-n:])


class Selector:
    def __init__(self, client: ImmichClient, settings: Settings,
                 rng: random.Random | None = None):
        self.client = client
        self.settings = settings
        self.rng = rng or random.Random()
        self.history = History(settings.state_dir / "history.json", settings.history_size)
        self._cache: dict[str, tuple[float, list[Candidate]]] = {}
        self._names: dict[str, dict[str, str]] | None = None

    # ---- name resolution ----

    async def _lookup(self) -> dict[str, dict[str, str]]:
        if self._names is None:
            albums = await self.client.albums()
            people = await self.client.people()
            tags = await self.client.tags()
            names: dict[str, dict[str, str]] = {
                "album": {a["albumName"].casefold(): a["id"] for a in albums},
                "person": {p["name"].casefold(): p["id"] for p in people if p.get("name")},
                "tag": {},
            }
            for t in tags:
                for key in (t.get("value"), t.get("name")):
                    if key:
                        names["tag"].setdefault(key.casefold(), t["id"])
            self._names = names
        return self._names

    async def _ids(self, kind: str, wanted: list[str]) -> list[str]:
        table = (await self._lookup())[kind]
        missing = [w for w in wanted if w.casefold() not in table]
        if missing:
            raise ConfigError(f"unknown {kind}(s) {missing}; see /catalog for available names")
        return [table[w.casefold()] for w in wanted]

    def invalidate(self) -> None:
        self._cache.clear()
        self._names = None

    # ---- candidate gathering ----

    async def queries(self, src: Source) -> list[dict]:
        """Expand a source into Immich search bodies.

        Immich ANDs list filters (an asset must be in *every* album / contain
        *every* person), so "any of" lists are split into one query per value
        and the results unioned.
        """
        common: dict = dict(BASE_FILTERS)
        if src.favorites is not None:
            common["isFavorite"] = src.favorites
        if src.taken_after:
            common["takenAfter"] = f"{src.taken_after.isoformat()}T00:00:00.000Z"
        if src.taken_before:
            common["takenBefore"] = f"{src.taken_before.isoformat()}T23:59:59.999Z"
        for field in ("city", "state", "country"):
            if (v := getattr(src, field)) is not None:
                common[field] = v

        dims: list[list[dict]] = []
        if src.albums:
            dims.append([{"albumIds": [i]} for i in await self._ids("album", src.albums)])
        if src.people:
            ids = await self._ids("person", src.people)
            dims.append([{"personIds": ids}] if src.people_match == "all"
                        else [{"personIds": [i]} for i in ids])
        if src.tags:
            dims.append([{"tagIds": [i]} for i in await self._ids("tag", src.tags)])
        if src.min_rating:
            dims.append([{"rating": r} for r in range(src.min_rating, 6)])

        bodies = []
        for combo in itertools.product(*dims) if dims else [()]:
            body = dict(common)
            for part in combo:
                body.update(part)
            bodies.append(body)
        return bodies

    async def candidates(self, src: Source) -> list[Candidate]:
        ttl = self.settings.candidate_cache_minutes * 60
        hit = self._cache.get(src.name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]

        found: dict[str, Candidate] = {}
        for body in await self.queries(src):
            remaining = src.max_candidates - len(found)
            if remaining <= 0:
                break
            if src.smart_query:
                body = {k: v for k, v in body.items() if k != "withExif"}
                items = await self.client.search_smart(src.smart_query, body, remaining)
            else:
                items = await self.client.search_metadata(body, remaining)
            for a in items:
                found.setdefault(a["id"], Candidate(a["id"], asset_aspect(a)))

        result = list(found.values())
        self._cache[src.name] = (time.monotonic(), result)
        log.info("source %r: %d candidates", src.name, len(result))
        return result

    # ---- choosing ----

    def _choose(self, cands: list[Candidate]) -> Candidate | None:
        mode = self.settings.orientation

        def is_portrait(c: Candidate) -> bool:
            return c.aspect is not None and c.aspect < 1

        pool = [c for c in cands if not (mode == "landscape_only" and is_portrait(c))]
        if not pool:
            return None
        # Avoid recently shown photos; if the pool is small, only avoid the newest half.
        recent = self.history.recent(min(self.settings.history_size, len(pool) // 2))
        fresh = [c for c in pool if c.id not in recent] or pool
        weights = [self.settings.portrait_weight if (mode == "landscape_preferred" and is_portrait(c))
                   else 1.0 for c in fresh]
        if not any(weights):
            weights = [1.0] * len(fresh)
        return self.rng.choices(fresh, weights=weights)[0]

    async def pick(self) -> Pick:
        remaining = [s for s in self.settings.sources if s.weight > 0]
        problems: list[str] = []
        while remaining:
            src = self.rng.choices(remaining, weights=[s.weight for s in remaining])[0]
            remaining.remove(src)
            try:
                chosen = self._choose(await self.candidates(src))
            except (ImmichError, ConfigError) as e:
                log.warning("source %r failed: %s", src.name, e)
                problems.append(f"{src.name}: {e}")
                continue
            if chosen:
                self.history.add(chosen.id)
                return Pick(chosen.id, src.name)
            problems.append(f"{src.name}: no matching photos")
        raise NoPhotoError("; ".join(problems) or "no sources configured")
