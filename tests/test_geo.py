"""Tests for spatial geometry and grid subdivision."""

from __future__ import annotations

import pytest

from gmaps_scraper.geo.geometry import Area, area_from_geojson
from gmaps_scraper.geo.grid import Cell, children_inside, cover


def test_build_area_from_point_circle():
    geojson = {"type": "Point", "coordinates": [13.4050, 52.5200], "radiusKm": 2.0}
    area = area_from_geojson(geojson, source="custom", label="Berlin Center")
    assert isinstance(area, Area)
    assert area.area_km2 > 10.0 and area.area_km2 < 15.0  # pi * 2^2 ~ 12.56 km2
    assert area.contains(52.5200, 13.4050) is True
    assert area.contains(52.7000, 13.4050) is False


def test_grid_generation_and_subdivide():
    geojson = {"type": "Point", "coordinates": [13.4050, 52.5200], "radiusKm": 1.0}
    area = area_from_geojson(geojson, source="custom", label="Test Circle")
    cells = cover(area, size_m=1000.0)
    assert len(cells) > 0

    first_cell = cells[0]
    children = children_inside(first_cell, area)
    assert len(children) > 0
    for child in children:
        assert child.level == first_cell.level + 1
        assert pytest.approx(child.size_m, rel=1e-3) == first_cell.size_m / 2.0

