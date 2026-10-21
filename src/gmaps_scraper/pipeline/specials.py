"""Order online and table reservation providers: tasks of their own after the place card.

* ``order_online`` (``scrapeOrderOnline``) reads Google's order widget of the card's order action
  (maps/order_online.py): pickup and delivery providers with Google's time and fee texts.  A card
  whose order action has only direct links (no widget) keeps the links, without a service.
* ``reservation`` (``scrapeTableReservationProvider``) reads Google's reservation page of the card's
  "Reserve a table" action (maps/reservation.py): the booking provider behind it.  Providers that
  the card names but the page does not (a second booking link) are kept with the card as source.

Both are read-only: nothing is booked or ordered.  The page is fetched with a GET from a
www.google.com page of the session; a failed or unreadable answer is retried once with a fresh link
from a new read of the card, then fails the task (the worker's retries apply).  An answer of another place
(identity check inside the parse, before the read counts as a success) raises at once: the task is
retried by the worker and, once its attempts are spent, ends ``failed`` with its section, so the job
ends ``completed_with_errors`` and ``--retry-failed`` picks it up.  The raw answer is kept as
evidence (also with ``evidence=errors`` when the read failed); the card's own links stay in the
export whatever the read gives.

Section statuses (``orderOnline`` / ``reservation``): ``absent`` -- the card has no such action;
``empty`` -- the widget was read and lists no provider; ``done`` -- read; ``failed`` -- the read or
its identity check failed (reason); a page of another place is never used.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..maps import pb
from ..maps.client import MapsClient
from ..maps.ids import cid_from_fid
from ..maps.order_online import (PARSER_VERSION as ORDER_PARSER, OrderIdentityError, OrderWidgetUnreadable,
                                 apify_view, parse_order_widget)
from ..maps.place import parse_place
from ..maps.reservation import (PARSER_VERSION as RESERVE_PARSER, ReserveIdentityError, ReservePageUnreadable,
                                parse_reserve_page, provider_entry)
from ..maps.xssi import dig, loads_guarded
from ..store import loads, now_iso
from .context import PRIORITY, RunContext

log = logging.getLogger(__name__)

#: why a delivery fee may be missing or approximate (no address is ever entered)
DELIVERY_CONDITIONS = ("no delivery address entered: delivery times and fees are the estimates Google's widget shows "
                       "for its default location at the time of reading")


# --------------------------------------------------------------------------- scheduling
def schedule_specials(ctx: RunContext, db, fid: str, fields: dict) -> None:
    """Queue the order and reservation reads an accepted place needs (called with its card)."""
    a, store = ctx.run.apify, ctx.store
    if a["scrapeOrderOnline"]:
        url, links = fields.get("googleFoodUrl"), fields.get("orderOnlineLinks") or []
        if url:
            store.set_section(fid, "orderOnline", "pending", db=db)
            store.add_task("order_online", fid, {"fid": fid, "url": url}, priority=PRIORITY["order_online"], db=db)
        elif links:
            store.put_extra(db, fid, "orderOnline", {"pickups": [], "deliveries": [], "links": links,
                                                     "source": "place_card"},
                            source_url=None, parser_version=ORDER_PARSER, evidence_sha=None)
            store.set_section(fid, "orderOnline", "done", count=len(links),
                              reason="direct ordering links without Google's order widget: the card does not say "
                                     "whether they are for pickup or delivery", db=db)
        else:
            store.set_section(fid, "orderOnline", "absent", reason="the place card has no order-online action", db=db)
    if a["scrapeTableReservationProvider"]:
        url = fields.get("reserveTableUrl")
        card = fields.get("tableReservationProviders") or []
        if url:
            store.set_section(fid, "reservation", "pending", db=db)
            store.add_task("reservation", fid, {"fid": fid, "url": url}, priority=PRIORITY["reservation"], db=db)
        elif card or fields.get("tableReservationLinks"):
            store.put_extra(db, fid, "reservation", {"providers": [_card_provider(p) for p in card], "source": "place_card"},
                            source_url=None, parser_version=RESERVE_PARSER, evidence_sha=None)
            store.set_section(fid, "reservation", "done", count=len(card),
                              reason="no Google reservation page: providers as named by the card's booking links",
                              db=db)
        else:
            store.set_section(fid, "reservation", "absent", reason="the place card has no table reservation", db=db)


def _card_provider(p: dict) -> dict:
    return {"name": p.get("name"), "email": None, "phone": None, "address": None,
            "reserveTableUrl": p.get("providerUrl") or p.get("reserveTableUrl"),
            "sources": {"name": "place_card", "reserveTableUrl": "place_card", "contacts": "not_in_source"}}


# --------------------------------------------------------------------------- fetching
def _fetch(maps: MapsClient, url: str):
    return maps.fetch_many([url])[0]


def _fresh_card(ctx: RunContext, maps: MapsClient, fid: str, *, task_id: int, kind: str) -> dict | None:
    """A new read of the place card, for fresh action links (the stored ones carry request tokens).
    A card of another place raises the identity error of ``kind`` (its answer is kept as evidence)."""
    url = pb.place_url(fid, hl=ctx.hl, gl=ctx.gl)
    resp = _fetch(maps, url)
    if resp.status != 200 or resp.error:
        return None
    try:
        place = dig(loads_guarded(resp.text), 6)
    except Exception:  # noqa: BLE001 - an unreadable card gives no fresh link
        return None
    if not isinstance(place, list):
        return None
    fields, _ = parse_place(place, language=ctx.hl, scraped_at=now_iso(), source="details")
    if fields.get("fid") != fid:
        note = f"fresh card identity mismatch: expected {fid}, got {fields.get('fid')}"
        ctx.evidence(kind="place", url=url, status=resp.status, text=resp.text, task_id=task_id, note=note,
                     failed=True)
        raise (ReserveIdentityError if kind == "reservation" else OrderIdentityError)(note)
    return fields


def _read(ctx: RunContext, maps: MapsClient, task: Any, *, kind: str, url: str, link_key: str, parse):
    """GET the page and parse it; on failure once more with a fresh link from the card.
    Returns (parsed, url, raw text, sha) or raises the last error."""
    fid = loads(task["payload"])["fid"]
    last: Exception | None = None
    for attempt in range(2):
        resp = _fetch(maps, url)
        text = resp.text or ""
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        try:
            if resp.status != 200 or resp.error:
                raise RuntimeError(f"{kind} page answered HTTP {resp.status} {resp.error or ''}".strip())
            parsed = parse(text)
            conflicts = getattr(parsed, "conflicts", None)
            # an answer whose conflicting provider rows are kept in the data keeps its source too
            ctx.evidence(kind=kind, url=url, status=resp.status, text=text, task_id=task["id"], keep=bool(conflicts),
                         note=f"{len(conflicts)} provider conflict(s) kept" if conflicts else None)
            return parsed, url, sha
        except (ReserveIdentityError, OrderIdentityError) as exc:
            ctx.evidence(kind=kind, url=url, status=resp.status, text=text, task_id=task["id"],
                         note=f"identity mismatch: {exc}"[:300], failed=True)
            raise
        except (RuntimeError, OrderWidgetUnreadable, ReservePageUnreadable) as exc:
            last = exc
            ctx.evidence(kind=kind, url=url, status=resp.status, text=text, task_id=task["id"],
                         note=str(exc)[:300], failed=True)
        if attempt == 0:
            fields = _fresh_card(ctx, maps, fid, task_id=task["id"], kind=kind)
            fresh = (fields or {}).get(link_key)
            if not fresh:
                break
            url = fresh
    raise last or RuntimeError(f"{kind} page could not be read")


def _conditions(ctx: RunContext) -> dict[str, Any]:
    p = ctx.run.bro.proxy
    return {"observedAt": now_iso(), "hl": ctx.hl, "gl": ctx.gl, "deliveryAddress": None,
            "sessionExit": (f"bro {p.tier} proxy, country {p.country or 'any'}" if p.enabled else "bro default exit"),
            "note": DELIVERY_CONDITIONS}


# --------------------------------------------------------------------------- handlers
def handle_order_online(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    p = loads(task["payload"])
    fid = p["fid"]
    want = cid_from_fid(fid)
    # another place's widget raises OrderIdentityError: a task failure, retried by the worker
    widget, url, sha = _read(ctx, maps, task, kind="order_online", url=p["url"], link_key="googleFoodUrl",
                             parse=lambda text: parse_order_widget(text, cid=want))
    row = store.place(fid)
    links = ((loads(row["details"]) or {}).get("orderOnlineLinks") or []) if row else []
    data = {"pickups": widget.pickups, "deliveries": widget.deliveries, "links": links, "source": "google_order_widget",
            "widgetUrl": url, "widgetTitle": widget.title, "placeCid": widget.place_cid, "conditions": _conditions(ctx),
            "conflicts": widget.conflicts}
    n = len(widget.pickups) + len(widget.deliveries)
    with store.tx() as db:
        store.put_extra(db, fid, "orderOnline", data, source_url=url, parser_version=ORDER_PARSER, evidence_sha=sha)
        if widget.empty:
            store.set_section(fid, "orderOnline", "empty", count=0, db=db,
                              reason="Google's order widget was read and lists no provider",
                              detail={"conflicts": len(widget.conflicts)})
        else:
            store.set_section(fid, "orderOnline", "done", count=n, db=db,
                              detail={"pickups": len(widget.pickups), "deliveries": len(widget.deliveries),
                                      "conflicts": len(widget.conflicts), "conditions": data["conditions"]})
    return {"fid": fid, "status": "empty" if widget.empty else "done", "pickups": len(widget.pickups),
            "deliveries": len(widget.deliveries)}


def handle_reservation(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    p = loads(task["payload"])
    fid = p["fid"]
    # another place's page (or one that proves no place) raises ReserveIdentityError: a task failure
    providers, url, sha = _read(ctx, maps, task, kind="reservation", url=p["url"], link_key="reserveTableUrl",
                                parse=lambda text: parse_reserve_page(text, fid=fid))
    row = store.place(fid)
    card = ((loads(row["details"]) or {}).get("tableReservationProviders") or []) if row else []
    out = [provider_entry(x, reserve_url=url) for x in providers]
    named = {e["name"].casefold() for e in out}
    # a provider the card names (a second booking link) but the reservation page does not
    out += [_card_provider(c) for c in card if c.get("name") and c["name"].casefold() not in named]
    with store.tx() as db:
        store.put_extra(db, fid, "reservation", {"providers": out, "source": "google_reserve_page", "pageUrl": url},
                        source_url=url, parser_version=RESERVE_PARSER, evidence_sha=sha)
        store.set_section(fid, "reservation", "done", count=len(out), db=db,
                          detail={"fromReservePage": len(providers), "fromCard": len(out) - len(providers),
                                  "contacts": "the reservation page names no email, phone or address of the "
                                              "provider (its address and phone are the restaurant's)"})
    return {"fid": fid, "status": "done", "providers": [e["name"] for e in out]}


# --------------------------------------------------------------------------- export views
def order_online_export(store, fid: str) -> dict | None:
    """Apify's ``orderOnline`` for a place: the read widget, empty lists when the card has no order
    action, None (left out) when the read failed or was not made."""
    sec = store.section(fid, "orderOnline")
    if sec is None:
        return None
    if sec["status"] == "absent":
        return {"pickUps": [], "deliveries": []}
    if sec["status"] not in ("done", "empty"):
        return None
    row = store.extra(fid, "orderOnline")
    data = loads(row["data"]) if row else None
    if data is None:
        return None
    return apify_view(data, data.get("links"))


def reservation_export(store, fid: str) -> list[dict] | None:
    """Apify's ``tableReservationProviders`` from the reservation read (None: use the card's)."""
    sec = store.section(fid, "reservation")
    if sec is None or sec["status"] != "done":
        return None
    row = store.extra(fid, "reservation")
    data = loads(row["data"]) if row else None
    return None if data is None else data.get("providers") or []
