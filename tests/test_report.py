"""Tests for job reporting and status generation."""

from __future__ import annotations

from pathlib import Path

from gmaps_scraper.report import render_text, status, to_json
from gmaps_scraper.store import Store


def test_empty_store_report(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    rep = status(store)

    assert rep.get("job") is None
    json_str = to_json(rep)
    assert isinstance(json_str, str)

    store.init_job(input_json={"search": "coffee"}, normalized={"search": "coffee"}, versions={})
    rep2 = status(store)
    assert rep2["job"]["status"] == "created"
    assert "job: created" in render_text(rep2)



