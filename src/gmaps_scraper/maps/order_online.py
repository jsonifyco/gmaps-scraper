"""Google's order widget (``googleFoodUrl``: ``/searchviewer/42?cvd=…``) -> pickup and delivery providers.

The widget is a server-rendered Google page; its data sits in inline ``(function(){var m={…};``
blocks (tested on Arepittas, The Coal Hole and Mr. Minsch):

* a header value ``[[…], "Order online", <place name>, 0]`` (the title is localised);
* the provider value ``[[[entry, …], [entry, …]], 2, […], [<?>, "<place CID>"]]`` -- two groups,
  the delivery tab first, then the pickup tab; ``[3][1]`` is the place's CID (the identity check);
* an entry ``[service, name, ?, ?, ?, logo, order URL, business website (1/0), ?,
  [time text, [[fee part], …], spoken time text], tracking]`` -- service 1 = delivery,
  2 = pickup, 0 = an ordering link of the business's own site ("May offer pickup / delivery",
  no time and no fee), whose service is that of its group.

Prices and times are kept as Google writes them (no address is entered: the widget shows its
estimate for Google's default location); nothing is computed.  A shape other than the above is
an :class:`OrderWidgetUnreadable` error, never an empty widget.

Every provider block of the requested CID is read (Rudy's has ``[null, [pickup entries]]``).  A
provider listed again with another time, fee, website flag or link is not a failure: the first
row stays and the other is kept in its ``variants`` (counted in :attr:`OrderWidget.conflicts`).
Blocks with other tab layouts are read with their own layout; an own-site link outside the
two-tab layout cannot be given a service and makes the answer unreadable.
"""

from __future__ import annotations

import json
import re
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Any

PARSER_VERSION = "order-online-2026.10.13"
SERVICE_CODES = {1: "delivery", 2: "pickup"}
#: group position -> service, for entries of code 0 (the delivery tab comes first)
GROUP_SERVICES = ("delivery", "pickup")

_BLOCK = re.compile(r"\(function\(\)\{var m=(\{.*?\});", re.S)


class OrderIdentityError(Exception):
    """The widget belongs to another place (its CID is not the expected one)."""


class OrderWidgetUnreadable(Exception):
    """The answer is not an order widget of the known shape."""


@dataclass
class OrderWidget:
    title: str | None
    place_cid: str | None
    pickups: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    #: provider rows Google lists more than once with other content: kept as ``variants`` of the
    #: first row, never a failure
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.pickups and not self.deliveries


def _blocks(html: str) -> list[dict]:
    out = []
    for m in _BLOCK.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _is_entry(e: Any) -> bool:
    return (isinstance(e, list) and len(e) >= 7 and isinstance(e[0], int) and isinstance(e[1], str)
            and (e[6] is None or isinstance(e[6], str)))


def _cid_of(value: list) -> str | None:
    tail = value[3] if len(value) > 3 else None
    if isinstance(tail, list) and len(tail) > 1 and isinstance(tail[1], str) and tail[1].isdigit():
        return tail[1]
    return None


def _provider_values(blocks: list[dict]) -> list[list]:
    """``[[group, …], 2, […], [<?>, "<CID>"]]``: groups of entries (an empty widget has empty groups)."""
    found = []
    for data in blocks:
        for value in data.values():
            if not (isinstance(value, list) and len(value) > 3 and isinstance(value[0], list) and value[0]
                    and _cid_of(value)):
                continue
            # Rudy's source uses [null, [pickup entries]] for an absent delivery tab.
            groups = [g if g is not None else [] for g in value[0]]
            if all(isinstance(g, list) for g in groups) and all(_is_entry(e) for g in groups for e in g):
                found.append([groups, *value[1:]])
    return found


def _header(blocks: list[dict]) -> str | None:
    """The place name of the header value ``[[…], "<widget title>", "<place name>", 0]``."""
    for data in blocks:
        for value in data.values():
            if (isinstance(value, list) and len(value) == 4 and isinstance(value[0], list)
                    and isinstance(value[1], str) and isinstance(value[2], str) and value[3] in (0, 1)):
                return value[2]
    return None


def _dedupe_key(name: str, url: str | None) -> tuple:
    """A provider once per service: the name with the link's host and path (the per-request
    ``rwg_token`` and tracking parameters differ between answers)."""
    if not url:
        return (name.casefold(), None)
    u = up.urlparse(url)
    return (name.casefold(), u.netloc.lower(), u.path)


#: what tells two rows of one provider apart (the link last: compared without tracking parameters)
VARIANT_KEYS = ("time", "feeParts", "businessWebsite", "url")


def _destination(url: str | None) -> tuple:
    parsed = up.urlparse(url or "")
    query = sorted((k, v) for k, v in up.parse_qsl(parsed.query) if k != "rwg_token" and not k.startswith("utm_"))
    return parsed.scheme, parsed.netloc.lower(), parsed.path, query


def parse_order_widget(html: str, *, cid: str | None = None) -> OrderWidget:
    """The widget's providers; with ``cid`` the widget must belong to that place
    (:class:`OrderIdentityError` otherwise)."""
    blocks = _blocks(html)
    if not blocks:
        raise OrderWidgetUnreadable("no data blocks in the answer (not an order widget page)")
    values = _provider_values(blocks)
    if not values:
        raise OrderWidgetUnreadable("no provider block with the place's CID in the answer")
    matches = [v for v in values if cid is None or _cid_of(v) == cid]
    if not matches:
        raise OrderIdentityError(f"the order widget belongs to CIDs {[_cid_of(v) for v in values]}, not {cid}")
    if cid is None and len({_cid_of(v) for v in matches}) != 1:
        raise OrderIdentityError("multiple place identities in the order widget")
    widget = OrderWidget(title=_header(blocks), place_cid=_cid_of(matches[0]))
    if cid is not None and widget.place_cid != cid:
        raise OrderIdentityError(f"the order widget belongs to CID {widget.place_cid}, not to this place ({cid})")
    # Every block of the confirmed place is read with its own tab layout: an entry names its service
    # (1 delivery, 2 pickup) whatever the layout; an own-site link (0) takes the service of its tab,
    # which only the two-tab layout gives -- elsewhere the answer is unreadable, never an empty widget
    entries = [(gi, len(v[0]), e) for v in matches for gi, group in enumerate(v[0]) for e in group]
    seen: dict[str, dict] = {"pickup": {}, "delivery": {}}
    for gi, n_groups, e in entries:
        code, name, url = e[0], e[1], e[6]
        if code in SERVICE_CODES:
            service, basis = SERVICE_CODES[code], "entry"
        elif code == 0 and n_groups == len(GROUP_SERVICES):
            service, basis = GROUP_SERVICES[gi], "tab"
        else:
            raise OrderWidgetUnreadable(f"entry {name!r} with service code {code!r} in group {gi} of {n_groups}")
        info = e[9] if len(e) > 9 and isinstance(e[9], list) else []
        time_text = info[0] if info and isinstance(info[0], str) else None
        parts = [p[0] for p in (info[1] if len(info) > 1 and isinstance(info[1], list) else [])
                 if isinstance(p, list) and p and isinstance(p[0], str)]
        key = _dedupe_key(name, url)
        row = {"service": service, "serviceFrom": basis, "name": name, "url": url,
               "time": time_text, "feeParts": parts, "fees": " · ".join(parts) if parts else None,
               "businessWebsite": bool(len(e) > 7 and e[7] == 1), "logo": e[5] if isinstance(e[5], str) else None}
        if key in seen[service]:
            old = seen[service][key]
            if (any(old[k] != row[k] for k in VARIANT_KEYS[:-1])
                    or _destination(old["url"]) != _destination(row["url"])):
                # the same provider with other content: the first row stays, the other is kept
                variant = {k: row[k] for k in VARIANT_KEYS}
                if variant not in old.setdefault("variants", []):
                    old["variants"].append(variant)
                    widget.conflicts.append({"reason": "variant", "service": service, "name": name})
            continue
        seen[service][key] = row
        (widget.pickups if service == "pickup" else widget.deliveries).append(row)
    return widget


def apify_view(widget_rows: dict[str, list[dict]] | None, links: list[dict] | None = None) -> dict[str, Any]:
    """Apify's ``orderOnline`` shape: ``pickUps`` keep the link in ``orderUrl`` (``url`` null),
    ``deliveries`` in ``url`` -- as compass/crawler-google-places 0.14.761 writes them.  Ordering
    links whose service Google does not state (no widget) go to ``links``, not into either list."""
    rows = widget_rows or {}
    out: dict[str, Any] = {
        "pickUps": [{"name": r["name"], "url": None, "orderUrl": r["url"], "pickUpTime": r["time"],
                     "pickUpFees": r["fees"]} for r in rows.get("pickups") or []],
        "deliveries": [{"name": r["name"], "url": r["url"], "deliveryTime": r["time"], "deliveryFees": r["fees"]}
                       for r in rows.get("deliveries") or []],
    }
    if links:
        out["links"] = [{"name": l["name"], "url": l["url"]} for l in links]
    return out
