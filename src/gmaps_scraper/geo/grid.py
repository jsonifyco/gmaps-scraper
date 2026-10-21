"""Covering an area with overlapping search cells.

A cell is a square of ``size_m`` metres centred on ``(lat, lng)``.  Its search
viewport is the 1024x768 screen whose height equals the cell size, so the
4:3 viewport covers the square plus a margin on the sides.  Cells overlap
their neighbours by ``OVERLAP`` so that pins near a border are seen twice.

Subdivision: a cell whose search ended saturated (Google stopped near its
per-viewport cap while places were still new) is split into four children
of half the size, down to ``min_size_m``.  A cell's end of results proves
nothing about the whole area; the coverage report counts processed,
saturated and unfinished cells and the gain of unique places per pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from shapely.geometry import box

from ..maps import pb
from .geometry import Area

OVERLAP = 0.10
M_PER_DEG_LAT = 111_320.0


def _dlat(m: float) -> float:
    return m / M_PER_DEG_LAT


def _dlng(m: float, lat: float) -> float:
    return m / (M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))


@dataclass(frozen=True)
class Cell:
    lat: float
    lng: float
    size_m: float
    level: int = 0
    pass_name: str = "main"

    @property
    def key(self) -> str:
        return f"{self.pass_name}:{self.level}:{self.lat:.6f}:{self.lng:.6f}:{int(round(self.size_m))}"

    def polygon(self, margin: float = 0.0):
        half = self.size_m * (0.5 + margin)
        return box(self.lng - _dlng(half, self.lat), self.lat - _dlat(half), self.lng + _dlng(half, self.lat),
                   self.lat + _dlat(half))

    @property
    def altitude(self) -> float:
        return pb.altitude_for_height(self.size_m)

    @property
    def zoom(self) -> float:
        return pb.zoom_for_altitude(self.altitude, self.lat)

    def children(self) -> list["Cell"]:
        q = self.size_m / 4
        half = self.size_m / 2
        return [Cell(self.lat + _dlat(dy), self.lng + _dlng(dx, self.lat), half, self.level + 1, self.pass_name)
                for dy in (-q, q) for dx in (-q, q)]

    def shifted(self, pass_name: str = "offset") -> "Cell":
        """The same-size cell moved by half a cell diagonally (control pass)."""
        half = self.size_m / 2
        return Cell(self.lat + _dlat(half), self.lng + _dlng(half, self.lat), self.size_m, self.level, pass_name)

    def to_json(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "sizeM": self.size_m, "level": self.level, "pass": self.pass_name}

    @staticmethod
    def from_json(data: dict) -> "Cell":
        return Cell(float(data["lat"]), float(data["lng"]), float(data["sizeM"]), int(data.get("level", 0)),
                    str(data.get("pass", "main")))


def size_for_zoom(zoom: float, lat: float) -> float:
    return pb.height_for_altitude(pb.altitude_for_zoom(zoom, lat))


def default_cell_size(area: Area, min_size_m: float = 1500.0) -> float:
    """Initial cell size: about 16 cells for the area, between 1.5 km and 60 km."""
    km2 = max(area.area_km2, 0.01)
    size_km = math.sqrt(km2 / 16.0)
    return max(min_size_m, min(60_000.0, size_km * 1000.0))


def parts(area: Area) -> list[Area]:
    """Separate polygons of a multi-part area (each is covered on its own)."""
    geom = area.geometry
    if geom.geom_type == "MultiPolygon":
        return [Area(geometry=g, source=area.source, label=f"{area.label} #{i + 1}", geojson={}) for i, g in
                enumerate(geom.geoms)]
    return [area]


def cover_parts(area: Area, size_for, *, pass_name: str = "main") -> list[Cell]:
    """Cover every part of ``area`` with cells sized by ``size_for(part)``."""
    cells: list[Cell] = []
    seen: set[str] = set()
    for part in parts(area):
        for cell in cover(part, size_for(part), pass_name=pass_name):
            if cell.key not in seen:
                seen.add(cell.key)
                cells.append(cell)
    return cells


def overview_cells(area: Area) -> list[Cell]:
    """One viewport per part, sized to the whole part (pass ``overview``).

    Searched first, it lists the places Google ranks highest for the territory,
    so the first N results of a limited search follow Google's own order."""
    out = []
    for part in parts(area):
        minx, miny, maxx, maxy = part.bounds
        lat, lng = (miny + maxy) / 2, (minx + maxx) / 2
        height = (maxy - miny) * M_PER_DEG_LAT
        width = (maxx - minx) * M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6)
        out.append(Cell(lat, lng, max(height, width * 3 / 4, 500.0), 0, "overview"))
    return out


def cover(area: Area, size_m: float, *, pass_name: str = "main", level: int = 0) -> list[Cell]:
    """Cells of ``size_m`` whose squares intersect ``area`` (with OVERLAP between neighbours).

    Callers with multi-part areas use :func:`cover_parts`: the lattice spans the
    bounding box, which for distant parts would be enormous."""
    minx, miny, maxx, maxy = area.bounds
    step = size_m * (1 - OVERLAP)
    cells: list[Cell] = []
    lat = miny + _dlat(step / 2)
    geom = area.geometry
    while True:
        lng = minx + _dlng(step / 2, lat)
        while True:
            cell = Cell(lat, lng, size_m, level, pass_name)
            if cell.polygon().intersects(geom):
                cells.append(cell)
            if lng + _dlng(step / 2, lat) >= maxx:
                break
            lng += _dlng(step, lat)
        if lat + _dlat(step / 2) >= maxy:
            break
        lat += _dlat(step)
    if not cells:
        c = geom.representative_point()
        cells.append(Cell(c.y, c.x, size_m, level, pass_name))
    return cells


def children_inside(cell: Cell, area: Area) -> list[Cell]:
    return [c for c in cell.children() if c.polygon().intersects(area.geometry)]


def offset_pass(leaves: Iterable[Cell], area: Area) -> list[Cell]:
    seen: set[str] = set()
    out: list[Cell] = []
    for leaf in leaves:
        s = leaf.shifted()
        if s.key not in seen and s.polygon().intersects(area.geometry):
            seen.add(s.key)
            out.append(s)
    return out
