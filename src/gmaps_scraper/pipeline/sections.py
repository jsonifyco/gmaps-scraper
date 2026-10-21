"""Reviews and images of accepted places."""

from __future__ import annotations

import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from ..maps import pb
from ..maps.client import MapsClient
from ..maps.photos import PARSER_VERSION as PHOTOS_PARSER, gallery_body, parse_gallery, parse_photometa, photometa_url, photometa_dates
from ..maps.xssi import ResponseFormatError
from ..store import loads, now_iso
from .context import RunContext
from .discovery import NeedFreshSession, TaskInterrupted

#: fresh sessions tried before falling back to Google's default review order
GATED_SESSION_RETRIES = 2
#: consecutive newest-order pages older than reviewsStartDate (by edit time) that end the list
#: default of bro.reviewsDateCutoffPages
DATE_CUTOFF_PAGES = 2
#: a list that ends below this share of Google's review count is reported as limited, not done
SHORT_END_SHARE = 0.95
#: pager stops that are retried on a fresh session, and how often
STALL_REASONS = {"no_new_pages", "refused"}
STALL_RETRIES = 2

log = logging.getLogger(__name__)


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _list_count(store, fid: str, sort: str, query: str | None, db=None) -> int:
    """Reviews of this place in the requested list (the limit counts these, not every stored review)."""
    return (db or store.db).execute("SELECT COUNT(*) FROM reviews WHERE fid = ? AND sort = ? AND query = ?",
                                    (fid, sort, query or "")).fetchone()[0]


# The review list is collected through Google Search with exact dates from Maps single-review
# pages: pipeline/review_list.py and pipeline/review_dates.py.  The Maps list pagers
# (maps/reviews.py, maps/reviews_native.py) are kept for their parsers and diagnostics only.
from .review_list import handle_reviews  # noqa: E402,F401  (kept importable from here)


GALLERY_JS = r"""(() => {
  const path = %s; const fid = %s; const bodies = []; const out = []; let token = %s; const max = %d;
  const build = %s;
  for (let i = 0; i < max; i++) {
    const body = build(token);
    const x = new XMLHttpRequest(); x.open('POST', path, false);
    x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8');
    x.setRequestHeader('X-Same-Domain', '1'); x.send(body);
    out.push({s: x.status, t: x.responseText, token: token});
    if (x.status !== 200) break;
    let next = null;
    try {
      const line = x.responseText.split('\n').find(l => l.startsWith('[["wrb.fr","hspqX"'));
      const env = JSON.parse(line); const payload = JSON.parse(env[0][2]); next = payload[5] || null;
    } catch (e) { next = null; }
    if (!next) break;
    token = next;
  }
  return out;
})()"""


def _gallery_builder_js(fid: str, page_size: int) -> str:
    """JS function token -> request body, built from the Python inner template."""
    template = json.dumps(json.loads(json.dumps(pb.photos_inner(fid, token="__TOKEN__", page_size=page_size))))
    return ("(function (token) { const inner = %s; inner[4][2][2] = token; "
            "const outer = [[['hspqX', JSON.stringify(inner), null, 'generic']]]; "
            "return 'f.req=' + encodeURIComponent(JSON.stringify(outer)); })") % template


def handle_images(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    p = loads(task["payload"])
    progress = loads(task["progress"]) or {}
    fid, limit, authors = p["fid"], p.get("limit"), p.get("authors")
    token = progress.get("token")
    count = int(progress.get("count", 0))
    done = bool(progress.get("done"))
    stale = 0
    store.set_section(fid, "images", "running")
    maps.ensure_maps()
    path = pb.batchexecute_path("hspqX", source_path="/maps/place/data=!4m2!3m1!1s" + fid, hl=ctx.hl, gl=ctx.gl,
                                reqid=maps.reqid)
    reason = None
    while not done:
        if ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        if limit is not None and count >= limit:
            reason = f"maxImages={limit}"
            break
        remaining = None if limit is None else max(limit - count, 1)
        page_size = 100 if remaining is None else min(100, max(20, remaining))
        max_pages = 5 if remaining is None else max(1, min(5, -(-remaining // page_size)))
        js = GALLERY_JS % (json.dumps(path), json.dumps(fid), json.dumps(token), max_pages,
                           _gallery_builder_js(fid, page_size))
        rows = maps.js(js, out_type="list", timeout=170) or []
        if not rows:
            raise RuntimeError("gallery request returned nothing")
        for row in rows:
            if int(row.get("s") or 0) != 200:
                raise RuntimeError(f"gallery HTTP {row.get('s')}")
            try:
                page = parse_gallery(row.get("t") or "")
            except ResponseFormatError as exc:
                ctx.evidence(kind="images", url=path, status=200, text=row.get("t") or "", task_id=task["id"],
                             note=str(exc), failed=True)
                raise RuntimeError(f"unreadable gallery page: {exc}") from exc
            ctx.evidence(kind="images", url=path, status=200, text=row.get("t") or "", task_id=task["id"])
            body = row.get("t")
            evidence_sha = hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else None
            for photo in page.photos:
                photo.setdefault("dateEvidence", {"meaning": "unknown", "precision": "unknown"}).update(observedAt=now_iso(), sourceUrl=path,
                    evidenceSha256=evidence_sha)
            if page.refused:
                raise RuntimeError("gallery request refused")
            known = {r[0] for r in store.db.execute("SELECT photo_id FROM images WHERE fid = ?", (fid,))}
            fresh = [ph for ph in page.photos if ph["photoId"] not in known]
            photos_page = fresh if limit is None else fresh[:max(limit - count, 0)]
            with store.tx() as db:
                new = store.add_images(db, fid, photos_page, count)
                count += new
                stale = stale + 1 if new == 0 and page.photos else 0
                token = page.next_token
                # an empty page ends the gallery even when Google still sends a token
                done = not token or not page.photos
                if limit is not None and count >= limit and token and page.photos:
                    done = False
                store.save_progress(task["id"], {"token": token, "count": count, "done": done}, db)
                store.set_section(fid, "images", "running", count=count, expected=page.total, db=db)
            if done or (limit is not None and count >= limit):
                break
            if stale >= 2:
                break
        if stale >= 2 and not done:
            reason = "the gallery kept returning photos already stored"
            break
    if authors:
        _authors(ctx, maps, fid, limit)
    status = "done" if done else "limited"
    store.set_section(fid, "images", status, reason="source_end" if done else (reason or "stopped"), count=count)
    return {"fid": fid, "images": count, "status": status}


def _authors(ctx: RunContext, maps: MapsClient, fid: str, limit: int | None) -> None:
    store = ctx.store
    photos = store.images_for(fid)
    if limit is not None:
        photos = photos[:limit]
    todo = [ph for ph in photos if not ph.get("authorName") and not ph.get("authorChecked")]
    for i in range(0, len(todo), 25):
        if ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        batch = todo[i:i + 25]
        urls = [photometa_url(ph["photoId"], fid, hl=ctx.hl, gl=ctx.gl) for ph in batch]
        for ph, resp in zip(batch, maps.fetch_many(urls)):
            ph["authorChecked"] = True
            if resp.status == 200:
                try:
                    observations = photometa_dates(resp.text, ph["photoId"])
                    sha = hashlib.sha256(resp.text.encode("utf-8")).hexdigest()
                    for observation in observations:
                        observation.update(observedAt=now_iso(), sourceUrl=resp.url, evidenceSha256=sha)
                    ph.setdefault("dateEvidence", {}).setdefault("observations", []).extend(observations)
                    ph.update(parse_photometa(resp.text))
                    ctx.evidence(kind="photometa", url=resp.url, status=resp.status, text=resp.text, task_id=None)
                except ResponseFormatError:
                    ctx.evidence(kind="photometa", url=resp.url, status=resp.status, text=resp.text,
                                 task_id=None, failed=True, note="unreadable or different publication")
            store.update_image(fid, ph["photoId"], ph)
