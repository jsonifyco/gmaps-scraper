"""Tests for SQLite Store (WAL)."""

from __future__ import annotations

from pathlib import Path

from gmaps_scraper.store import Store


def test_store_initialization(tmp_path: Path):
    db_path = tmp_path / "state.sqlite"
    store = Store(db_path)
    assert db_path.exists()

    with store.tx() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "tasks" in tables
        assert "places" in tables
        assert "job" in tables


def test_store_task_lifecycle(tmp_path: Path):
    db_path = tmp_path / "state.sqlite"
    store = Store(db_path)

    store.init_job(input_json={"test": True}, normalized={"test": True}, versions={})
    store.set_job(status="running")
    job = store.job()
    assert job["status"] == "running"

    store.add_task(kind="discovery", key="cell_1", payload={"lat": 52.5, "lng": 13.4}, priority=10)
    counts = store.task_counts()
    assert counts.get("discovery", {}).get("pending") == 1

    claimed = store.claim(owner="worker-1", lease=60.0)
    assert claimed and len(claimed) > 0
    task = claimed[0]
    assert task["key"] == "cell_1"

    with store.tx() as db:
        store.save_progress(task["id"], {"done": True, "places": 5}, db)
    store.finish_task(task["id"], status="done", result={"placesCount": 5})

    counts_after = store.task_counts()
    assert counts_after.get("discovery", {}).get("done") == 1
