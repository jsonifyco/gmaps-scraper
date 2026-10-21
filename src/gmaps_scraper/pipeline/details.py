"""Place detail tasks (``/maps/preview/place``), fetched several per bro command."""

from __future__ import annotations

import logging
from typing import Any

from ..maps import pb
from ..maps.client import MapsClient
from ..maps.ids import normalize_fid
from ..maps.place import PARSER_VERSION as PLACE_PARSER, maps_url, parse_place
from ..maps.xssi import ResponseFormatError, dig, loads_guarded
from ..store import loads, now_iso
from .context import PRIORITY, RunContext
from .discovery import schedule_followups
from .filters import reject_reason

log = logging.getLogger(__name__)


class PlaceFetchError(RuntimeError):
    pass


def handle_places(ctx: RunContext, maps: MapsClient, tasks: list[Any]) -> dict[int, tuple[str, dict | str]]:
    """Fetch and store a batch; returns ``{task_id: (status, result_or_error)}``."""
    store = ctx.store
    fids = [loads(t["payload"])["fid"] for t in tasks]
    urls = [pb.place_url(f, hl=ctx.hl, gl=ctx.gl) for f in fids]
    responses = maps.fetch_many(urls)
    outcome: dict[int, tuple[str, dict | str]] = {}
    for task, fid, url, resp in zip(tasks, fids, urls, responses):
        if resp.status != 200 or resp.error:
            ctx.evidence(kind="place", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                         note=resp.error, failed=True)
            outcome[task["id"]] = ("retry", f"HTTP {resp.status} {resp.error or ''}"[:300])
            continue
        try:
            data = loads_guarded(resp.text)
        except ResponseFormatError as exc:
            ctx.evidence(kind="place", url=url, status=resp.status, text=resp.text, task_id=task["id"],
                         note=str(exc), failed=True)
            outcome[task["id"]] = ("retry", f"unreadable place response: {exc}")
            continue
        ctx.evidence(kind="place", url=url, status=resp.status, text=resp.text, task_id=task["id"])
        place = dig(data, 6)
        if not isinstance(place, list) or not dig(place, 10):
            with store.tx() as db:
                store.set_section(fid, "details", "absent", reason="the place endpoint returned no place", db=db)
                db.execute("UPDATE results SET accepted = 0, filter_reason = 'place_not_found' "
                           "WHERE fid = ? AND accepted IS NULL", (fid,))
                store.finish_task(task["id"], "done", result={"found": False}, db=db)
            outcome[task["id"]] = ("done", {"found": False})
            continue
        fields, errors = parse_place(place, language=ctx.hl, scraped_at=now_iso(), source="details")
        real = fields["fid"]
        with store.tx() as db:
            if real != normalize_fid(fid):
                _remap(db, normalize_fid(fid), real)
            store.upsert_place(db, real, fields, source="details", language=ctx.hl, parser_version=PLACE_PARSER,
                               errors=errors)
            store.set_section(real, "details", "done", detail={"parseErrors": errors} if errors else None, db=db)
            accepted = _decide(ctx, db, real, fields)
            if accepted:
                schedule_followups(ctx, db, real, fields)
                if ctx.run.apify["scrapeDirectories"]:
                    _directory(ctx, db, real, fields)
            store.finish_task(task["id"], "done", result={"fid": real, "accepted": accepted,
                                                          "parseErrors": len(errors)}, db=db)
        outcome[task["id"]] = ("done", {"fid": real, "accepted": accepted})
    return outcome


def _remap(db, old: str, new: str) -> None:
    """A CID-form id (``0x0:..``) turned out to be ``new``; move its rows."""
    for table in ("results", "discoveries", "sections"):
        db.execute(f"UPDATE OR IGNORE {table} SET fid = ? WHERE fid = ?", (new, old))
        db.execute(f"DELETE FROM {table} WHERE fid = ?", (old,))
    db.execute("INSERT OR IGNORE INTO place_aliases (alias, kind, fid) VALUES (?, 'fid', ?)", (old, new))


def _decide(ctx: RunContext, db, fid: str, fields: dict) -> bool:
    """Apply filters to rows still undecided (requested places); keep decisions made by search."""
    rows = db.execute("SELECT search_key, accepted, search_term FROM results WHERE fid = ?", (fid,)).fetchall()
    accepted_any = False
    total_cap = ctx.run.legacy.get("maxCrawledPlaces")
    for row in rows:
        if row["accepted"] is None:
            reason = reject_reason(fields, ctx.run, search_term=None, from_search=False)
            if reason is None and total_cap and str(row["search_key"]).startswith("dir:"):
                # a place found in a directory is a discovered place: the overall cap holds for it too
                # (checked in this BEGIN IMMEDIATE transaction, so concurrent workers cannot pass it)
                known = db.execute("SELECT 1 FROM results WHERE fid = ? AND accepted = 1 LIMIT 1", (fid,)).fetchone()
                if known is None and ctx.store.total_accepted(db) >= total_cap:
                    reason = "total_limit_reached"
            db.execute("UPDATE results SET accepted = ?, filter_reason = ? WHERE fid = ? AND search_key = ?",
                       (0 if reason else 1, reason, fid, row["search_key"]))
            accepted_any |= reason is None
        else:
            accepted_any |= bool(row["accepted"])
    return accepted_any


def _directory(ctx: RunContext, db, parent: str, fields: dict) -> None:
    """Places listed "at this place" (malls, airports) become requested places of their own."""
    store = ctx.store
    entries = fields.get("directory") or []
    if not entries:
        store.set_section(parent, "directory", "absent", reason="no directory at this place", db=db)
        return
    parent_key = db.execute("SELECT search_key FROM results WHERE fid = ? AND accepted = 1 ORDER BY created_at LIMIT 1",
                            (parent,)).fetchone()
    for entry in entries:
        child = entry["fid"]
        key = f"dir:{parent}"
        store.add_discovery(db, fid=child, source="directory", search_term=entry.get("category"), task_id=None,
                            parent_fid=parent, page=0, position=0, rank=None)
        store.add_result(db, fid=child, search_key=key, search_term=parent_key["search_key"] if parent_key else None,
                         rank=None, accepted=None, parent_fid=parent)
        store.add_task("place", child, {"fid": child}, priority=PRIORITY["place"], db=db)
    store.set_section(parent, "directory", "done", count=len(entries), db=db)
