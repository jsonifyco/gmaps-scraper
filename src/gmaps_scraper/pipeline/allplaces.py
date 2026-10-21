"""``allPlacesNoSearchAction``: places visible on the map, without a search term.

The Maps web client draws pins on a canvas and has no "list everything here"
request (an empty or ``*`` query returns nothing; probed 2026-10-01).  Like the
Apify OCR mode, this mode reads the labels: each cell is opened at
``bro.allPlacesZoom`` and bro's ``extract`` (vision) lists the visible place
labels.  A label is only a lead -- every place stored here comes from a
search response for that label inside the cell, so the stored values are
Google's, and the discovery keeps the label and the extract command id as
its provenance.  The mode costs LLM/vision credits and is opt-in by input.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from shapely.geometry import Point

from ..bro.transport import command
from ..geo import grid
from ..maps import pb
from ..maps.client import MapsClient
from ..maps.place import PARSER_VERSION as PLACE_PARSER, parse_place
from ..maps.search import parse_search
from ..store import loads, now_iso
from .context import PRIORITY, RunContext
from .discovery import TaskInterrupted, schedule_followups, search_page_url
from .filters import reject_reason

log = logging.getLogger(__name__)

LABEL_SCHEMA = [{"name": "<exact text of one place label printed on the map (business, shop, restaurant, "
                         "hotel, landmark); not street names, not district names>"}]


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^\w]+", " ", text).strip()


def handle_allplaces(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    run = ctx.run
    payload = loads(task["payload"])
    cell = grid.Cell.from_json(payload["cell"])
    search_key = payload["searchKey"]
    maps.ensure_maps()
    view = f"https://www.google.com/maps/@{cell.lat:.7f},{cell.lng:.7f},{run.bro.allPlacesZoom:.1f}z?hl={ctx.hl}&gl={ctx.gl}"
    res = maps.browser.execute([command("open_url", url=view), command("sleep", wait_time=3.0),
                                command("extract", data_instruction="List every place label visible on the map.",
                                        json_schema=LABEL_SCHEMA, model_size="small")], timeout=240)
    maps.browser.current_url = None
    step = res.steps[-1] if res.steps else None
    if step is None or not step.success:
        raise RuntimeError(f"map label extraction failed: {step.error_name if step else res.status}")
    labels = []
    for row in ((step.data or {}).get("extracted_json") or []):
        name = (row or {}).get("name") if isinstance(row, dict) else None
        if name and len(name.strip()) > 1 and name.strip() not in labels:
            labels.append(name.strip())
    found = accepted = 0
    alt = pb.altitude_for_height(cell.size_m)
    for i in range(0, len(labels), 8):
        if ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        batch = labels[i:i + 8]
        urls = [pb.search_url(label, cell.lat, cell.lng, alt, hl=ctx.hl, gl=ctx.gl) for label in batch]
        for label, url, resp in zip(batch, urls, maps.fetch_many(urls)):
            if resp.status != 200:
                continue
            try:
                page = parse_search(resp.text)
            except ValueError:
                continue
            with store.tx() as db:
                for item in page.items:
                    fields, errors = parse_place(item.place, language=ctx.hl, scraped_at=now_iso(), source="search")
                    loc = fields.get("location") or {}
                    if loc.get("lat") is None or _fold(fields.get("title")) != _fold(label):
                        continue
                    if not cell.polygon(margin=0.1).covers(Point(loc["lng"], loc["lat"])):
                        continue
                    fid = fields["fid"]
                    found += 1
                    store.upsert_place(db, fid, fields, source="search", language=ctx.hl, parser_version=PLACE_PARSER,
                                       errors=errors)
                    store.add_discovery(db, fid=fid, source="allplaces", search_term=label, task_id=task["id"],
                                        cell_key=cell.key, cell_pass=cell.pass_name, zoom=run.bro.allPlacesZoom, page=0,
                                        position=item.position, rank=None,
                                        search_page_url=search_page_url(label, cell.lat, cell.lng, cell.zoom, ctx.hl))
                    inside = ctx.inside(loc.get("lat"), loc.get("lng"))
                    reason = "outside_area" if inside is False else reject_reason(fields, run, search_term=None,
                                                                                    from_search=False)
                    if db.execute("SELECT 1 FROM results WHERE fid = ? AND search_key = ?", (fid, search_key)).fetchone():
                        continue
                    rank = db.execute("SELECT COUNT(*) FROM results WHERE search_key = ?", (search_key,)).fetchone()[0] + 1
                    store.add_result(db, fid=fid, search_key=search_key, search_term=None, rank=rank,
                                     accepted=0 if reason else 1, filter_reason=reason,
                                     inside_area=None if inside is None else int(inside),
                                     input_start_url=payload.get("inputStartUrl"))
                    if reason is None:
                        accepted += 1
                        if run.details_enabled:
                            store.add_task("place", fid, {"fid": fid}, priority=PRIORITY["place"], db=db)
                        else:
                            schedule_followups(ctx, db, fid, fields)
                    break
    return {"labels": len(labels), "matched": found, "accepted": accepted, "extractCommand": res.command_id,
            "cell": cell.to_json()}
