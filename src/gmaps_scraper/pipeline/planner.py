"""Initial plan of a job: territories, search cells and direct-place tasks.

Planning runs once, before any browser starts.  Every task key is unique, so
re-planning on resume adds nothing twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..geo import grid
from ..geo.geometry import Area, GeometryError, area_from_geojson, viewport_area
from ..geo.nominatim import LocationError, Nominatim, resolve_area
from ..maps import pb
from ..input_model import ADAPTIVE
from ..maps.ids import fid_from_place_id, normalize_fid
from ..store import dumps, now_iso
from .context import PRIORITY, RunContext, Territory

log = logging.getLogger(__name__)

#: viewport used for a search term without any location (Google geocodes the text)
WORLD_VIEW = {"lat": 30.0, "lng": 0.0, "zoom": 3.0}


@dataclass
class PlanSummary:
    territories: list[dict[str, Any]] = field(default_factory=list)
    tasks: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def resolve_territory(ctx: RunContext, nominatim: Nominatim | None = None) -> list[Area]:
    run = ctx.run
    a = run.apify
    mode = run.location_mode
    if mode == "none":
        return []
    if mode == "custom":
        try:
            return [area_from_geojson(a["customGeolocation"], label="customGeolocation")]
        except GeometryError as exc:
            raise LocationError(f"customGeolocation: {exc}") from exc
    nominatim = nominatim or Nominatim(run.bro.nominatimUrl, contact=run.bro.nominatimContact)
    if mode == "query":
        return [resolve_area(nominatim, query=a["locationQuery"], osm_id=run.bro.osmId, label=a["locationQuery"])]
    structured = {"country": a.get("countryCode"), "state": a.get("state"), "county": a.get("county"),
                  "city": a.get("city"), "postalcode": a.get("postalCode")}
    structured = {k: str(v) for k, v in structured.items() if v}
    label = ", ".join(f"{k}={v}" for k, v in structured.items())
    return [resolve_area(nominatim, structured=structured, osm_id=run.bro.osmId, label=label)]


def initial_cell_size(ctx: RunContext, area: Area) -> float:
    zoom = ctx.run.legacy.get("zoom")
    if zoom:
        c = area.geometry.representative_point()
        return grid.size_for_zoom(float(zoom), c.y)
    minimum = ctx.run.bro.minCellKm * 1000 if ctx.run.bro.discoveryAlgorithm in ADAPTIVE else 1500.0
    return grid.default_cell_size(area, minimum)


def plan(ctx: RunContext, nominatim: Nominatim | None = None) -> PlanSummary:
    run = ctx.run
    store = ctx.store
    summary = PlanSummary(warnings=list(run.warnings))
    areas = resolve_territory(ctx, nominatim)
    with store.tx() as db:
        for area in areas:
            cur = db.execute("INSERT INTO territories (label, source, geojson, area_km2, provenance, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (area.label, area.source, dumps(area.to_json()["geometry"]),
                                                            area.area_km2, dumps(area.provenance), now_iso()))
            ctx.territories.append(Territory(cur.lastrowid, area))
            summary.territories.append({"id": cur.lastrowid, **area.to_json(), "geometry": None})

        deeper = run.legacy.get("deeperCityScrape", True)
        counts: dict[str, int] = {}

        def add(kind: str, key: str, payload: dict) -> None:
            if store.add_task(kind, key, payload, priority=PRIORITY[kind], db=db):
                counts[kind] = counts.get(kind, 0) + 1

        coverage = run.bro.effective_coverage
        for term in run.search_terms:
            search_key = f"term:{term}"
            if ctx.territories:
                for terr in ctx.territories:
                    cells = grid.cover_parts(terr.area, lambda a: initial_cell_size(ctx, a))
                    if len(cells) > 1:
                        for cell in grid.overview_cells(terr.area):
                            if store.add_task("search", f"{search_key}|t{terr.id}|{cell.key}",
                                              {"term": term, "searchKey": search_key, "territoryId": terr.id,
                                               "cell": cell.to_json(), "subdivide": False},
                                              priority=PRIORITY["overview"], db=db):
                                counts["overview"] = counts.get("overview", 0) + 1
                    for cell in cells:
                        add("search", f"{search_key}|t{terr.id}|{cell.key}",
                            {"term": term, "searchKey": search_key, "territoryId": terr.id, "cell": cell.to_json(),
                             "subdivide": bool(deeper)})
                    if coverage in ("deep", "extended"):
                        add("controlpass", f"{search_key}|t{terr.id}",
                            {"term": term, "searchKey": search_key, "territoryId": terr.id, "coverage": coverage})
            else:
                add("search", f"{search_key}|world",
                    {"term": term, "searchKey": search_key, "viewport": WORLD_VIEW, "subdivide": False})

        for su in run.start_urls:
            key = f"url:{su.url}"
            if su.kind in ("place", "cid", "reviews") and su.fid:
                _direct(db, store, su.fid, key, input_start_url=su.url)
                add("place", su.fid, {"fid": su.fid})
            elif su.kind == "place" and not su.fid:
                add("resolve", key, {"kind": "url", "url": su.url, "searchKey": key, "placeId": su.place_id})
            elif su.kind in ("short", "list"):
                add("resolve", key, {"kind": su.kind, "url": su.url, "searchKey": key})
            elif su.kind == "search":
                view = ({"lat": su.lat, "lng": su.lng, "zoom": su.zoom or 13.0} if su.lat is not None
                        else (WORLD_VIEW if not ctx.territories else None))
                if view is None:
                    for terr in ctx.territories:
                        for cell in grid.cover_parts(terr.area, lambda a: initial_cell_size(ctx, a)):
                            add("search", f"{key}|t{terr.id}|{cell.key}",
                                {"term": su.query or "", "searchKey": key, "territoryId": terr.id,
                                 "cell": cell.to_json(), "subdivide": bool(deeper), "inputStartUrl": su.url})
                else:
                    add("search", key, {"term": su.query or "", "searchKey": key, "viewport": view,
                                        "subdivide": False, "inputStartUrl": su.url})
            elif su.kind == "map":
                if run.all_places_mode:
                    lat, lng, zoom = su.lat, su.lng, su.zoom or 15.0
                    h = pb.height_for_altitude(pb.altitude_for_zoom(zoom, lat))
                    area = viewport_area(lat, lng, h, h * pb.SCREEN_W / pb.SCREEN_H, label=su.url)
                    for cell in grid.cover(area, grid.size_for_zoom(run.bro.allPlacesZoom, lat)):
                        add("allplaces", f"{key}|{cell.key}", {"searchKey": key, "cell": cell.to_json(),
                                                                "inputStartUrl": su.url})
                else:
                    summary.warnings.append(f"map URL without a search term is skipped: {su.url}")

        for place_id in run.direct_place_ids:
            key = f"placeId:{place_id}"
            fid = fid_from_place_id(place_id)
            if fid:
                _direct(db, store, fid, key, input_place_id=place_id)
                add("place", fid, {"fid": fid})
            else:
                add("resolve", key, {"kind": "placeId", "placeId": place_id, "searchKey": key})

        if run.all_places_mode and ctx.territories:
            for terr in ctx.territories:
                for cell in grid.cover_parts(terr.area, lambda a: grid.size_for_zoom(
                        run.bro.allPlacesZoom, a.geometry.representative_point().y)):
                    add("allplaces", f"all|t{terr.id}|{cell.key}",
                        {"searchKey": "allPlaces", "territoryId": terr.id, "cell": cell.to_json()})
        summary.tasks = counts
    return summary


def _direct(db, store, fid: str, search_key: str, *, input_start_url: str | None = None,
            input_place_id: str | None = None) -> None:
    """A requested place: its output row exists from the start, acceptance is decided by details."""
    fid = normalize_fid(fid)
    store.add_result(db, fid=fid, search_key=search_key, search_term=None, rank=None, accepted=None,
                     input_start_url=input_start_url, input_place_id=input_place_id)
