"""Place array (``preview/place`` ``[6]`` or search item ``[1]``) -> Apify-named fields.

Every index below was located against the stage-1 fixtures and the
reference Apify run ``l5M4tnBCDxQUuK8MU`` (``devtools/locate_fields.py``);
``docs/ARCHITECTURE_AND_FIELD_MAP.md`` lists them with their evidence.  Values that are not
present in the source stay ``None`` / empty -- nothing is guessed.

The parser is defensive: any malformed section produces a field-level error
note instead of failing the whole place, so a broken gallery block cannot
destroy the address, reviews counters and the rest.
"""

from __future__ import annotations

import re
import urllib.parse as up
from datetime import datetime, timezone
from typing import Any, Callable

from .ids import cid_from_fid, normalize_fid, is_fid
from .travel_reviews import partner_sources
from .xssi import dig

PARSER_VERSION = "place-2026.10.14b"  # partner review sources of the card (p[175][12])

DAY_NAMES = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}
HISTOGRAM_DAYS = {7: "Su", 1: "Mo", 2: "Tu", 3: "We", 4: "Th", 5: "Fr", 6: "Sa"}

#: action button kinds in ``[75]`` (verified on Coal Hole, Monterey, Zahnarzt, Arepittas)
ACTION_RESERVE, ACTION_BOOK, ACTION_ORDER = 1, 3, 4

_SPACES = re.compile(r"[   ]")


def clean_text(value: Any) -> Any:
    """Text is kept exactly as Google sends it.

    Google writes times with narrow no-break spaces ("10 AM") and the
    Apify output keeps them, so no normalisation happens here; consumers that
    want plain spaces can use :func:`plain_spaces`.
    """
    return value


def plain_spaces(value: Any) -> Any:
    if isinstance(value, str):
        return _SPACES.sub(" ", value)
    return value


def _strip_image_size(url: str | None) -> str | None:
    """Google image URLs end with a size directive (``=w203-h100-k-no``); keep the base."""
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    return url


def maps_url(title: str | None, place_id: str | None) -> str | None:
    """Apify's canonical place URL (``/maps/search/?api=1&query=...&query_place_id=...``)."""
    if not place_id:
        return None
    query = up.quote(title or "", safe="")
    return f"https://www.google.com/maps/search/?api=1&query={query}&query_place_id={place_id}"


#: array positions that must hold lists when present (a different type means the format changed)
STRUCTURED_SECTIONS = {4: "rating", 7: "website", 9: "location", 13: "categories", 32: "description",
                       37: "photos", 64: "hotel", 75: "actions", 84: "popularTimes", 86: "gasPrices",
                       99: "peopleAlsoSearch", 100: "additionalInfo", 118: "additionalOpeningHours",
                       122: "ownerUpdates", 153: "reviewsTags", 175: "reviewsSummary", 178: "phone",
                       183: "addressParts", 186: "directory", 203: "openingHours"}


class FieldErrors(dict):
    """field name -> error text, for sections that could not be parsed."""


def _safe(errors: FieldErrors, name: str, fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - recorded per field, the place survives
        errors[name] = f"{type(exc).__name__}: {exc}"[:300]
        return default


# ---------------------------------------------------------------- sections

def _hours_list(days: list | None, *, apify_style: bool) -> list[dict[str, str]]:
    """``[[day, idx, date, [[text, ...], ...], ...], ...]`` -> Monday-first list."""
    out = []
    for day in sorted(days or [], key=lambda d: (d[1] if isinstance(d, list) and len(d) > 1 else 9)):
        if not isinstance(day, list) or len(day) < 4:
            continue
        name = DAY_NAMES.get(day[1], day[0])
        ranges = [clean_text(r[0]) for r in (day[3] or []) if isinstance(r, list) and r and r[0]]
        text = ", ".join(ranges) if ranges else None
        if text is None:
            continue
        if apify_style:
            text = text.replace("–", " to ")
        out.append({"day": name, "hours": text})
    return out


def opening_hours(p: list) -> list[dict[str, str]]:
    return _hours_list(dig(p, 203, 0), apify_style=True)


def opening_hours_google_order(p: list) -> list[dict[str, str]]:
    """The hours as Google lists them: from today, with the day names of the request's language (Apify's
    form for a language other than English, core-parity acceptance 2026-10-14)."""
    out = []
    for day in dig(p, 203, 0, default=[]) or []:
        if not isinstance(day, list) or len(day) < 4 or not isinstance(day[0], str):
            continue
        ranges = [clean_text(r[0]) for r in (day[3] or []) if isinstance(r, list) and r and r[0]]
        if ranges:
            out.append({"day": day[0], "hours": ", ".join(ranges).replace("–", " to ")})
    return out


def additional_opening_hours(p: list) -> dict[str, list[dict[str, str]]] | None:
    out: dict[str, list] = {}
    for block in dig(p, 118, default=[]) or []:
        label = dig(block, 0)
        days = dig(block, 3, 0)
        if label and days:
            out[clean_text(label)] = _hours_list(days, apify_style=False)
    return out or None


def open_now(p: list) -> bool | None:
    """Google's own "Open"/"Closed" status for the moment of the request."""
    status = dig(p, 203, 1, 8) or dig(p, 203, 1, 4)
    if not status:
        return None
    code = dig(status, 1, 0, 1)
    if code in (4, 5):
        return True
    if code in (6, 7):
        return False
    text = str(dig(status, 0) or "").lower()
    if text.startswith("open"):
        return True
    if text.startswith("closed"):
        return False
    return None


def popular_times(p: list) -> tuple[dict | None, str | None, int | None]:
    hist: dict[str, list] = {}
    for day in dig(p, 84, 0, default=[]) or []:
        key = HISTOGRAM_DAYS.get(dig(day, 0))
        if not key:
            continue
        hours = dig(day, 1, default=[]) or []
        hist[key] = [{"hour": h[0], "occupancyPercent": h[1]} for h in hours if isinstance(h, list) and len(h) > 1]
    live_text = clean_text(dig(p, 84, 6))
    live_pct = dig(p, 84, 7, 1)
    return (hist or None), live_text, live_pct


def _feature_items(feature: list) -> list[dict[str, bool]]:
    """One attribute: boolean (kind 1), valued (kind 2, e.g. "Free Wi-Fi") or a list (kind 3, languages)."""
    kind = dig(feature, 2, 0)
    if kind == 2:
        label = dig(feature, 2, 3, 2) or dig(feature, 1)
        return [{label: True}] if label else []
    if kind == 3:
        # a list-valued attribute (card brands, spoken languages): Apify reports the
        # attribute itself; the values are kept by attribute_values() for native export
        name = dig(feature, 1)
        return [{name: True}] if name else []
    name = dig(feature, 1)
    if name is None:
        return []
    flag = dig(feature, 2, 1, 0, 0)
    return [{name: bool(flag) if flag is not None else True}]


def attribute_values(p: list) -> dict[str, list[str]]:
    """List-valued attributes with their values, e.g. ``{"Credit cards": ["VISA", ...]}``."""
    out: dict[str, list[str]] = {}
    for group in dig(p, 100, 1, default=[]) or []:
        for feature in dig(group, 2, default=[]) or []:
            if dig(feature, 2, 0) == 3 and dig(feature, 1):
                values = dig(feature, 2, 4, 1, 0, 0, default=[]) or []
                out[dig(feature, 1)] = [dig(v, 2) for v in values if dig(v, 2)]
    return out


def additional_info(p: list) -> dict[str, list[dict[str, bool]]] | None:
    out: dict[str, list] = {}
    for group in dig(p, 100, 1, default=[]) or []:
        title = dig(group, 1)
        items: list[dict[str, bool]] = []
        for feature in dig(group, 2, default=[]) or []:
            items.extend(_feature_items(feature))
        if title and items:
            out[title] = items
    amenities = [{dig(a, 2): bool(dig(a, 3))} for a in dig(p, 64, 2, default=[]) or [] if dig(a, 2)]
    if amenities:
        out.setdefault("Amenities", [])
        known = {next(iter(x)) for x in out["Amenities"]}
        out["Amenities"].extend(a for a in amenities if next(iter(a)) not in known)
    return out or None


def reviews_distribution(p: list) -> dict[str, int] | None:
    counts = dig(p, 175, 3)
    if not isinstance(counts, list) or len(counts) != 5:
        return None
    names = ["oneStar", "twoStar", "threeStar", "fourStar", "fiveStar"]
    return {n: int(c or 0) for n, c in zip(names, counts)}


def reviews_tags(p: list) -> list[dict]:
    out = []
    for tag in dig(p, 153, 0, default=[]) or []:
        title = dig(tag, 1)
        if title:
            out.append({"title": clean_text(title), "count": dig(tag, 3, 4)})
    return out


def places_tags(p: list) -> list[dict]:
    """"At this place" categories with the number of places listed under each."""
    out = []
    for group in dig(p, 186, 2, default=[]) or []:
        title = dig(group, 0)
        members = dig(group, 1, default=[]) or []
        if title:
            out.append({"title": clean_text(title), "count": len(members)})
    return out


def directory_fids(p: list) -> list[dict[str, str]]:
    """Places inside this one (malls, airports, hotels): ``[{"category", "fid"}]``."""
    out = []
    for group in dig(p, 186, 2, default=[]) or []:
        title = dig(group, 0)
        for member in dig(group, 1, default=[]) or []:
            fid = dig(member, 0)
            if is_fid(fid):
                out.append({"category": clean_text(title), "fid": normalize_fid(fid)})
    return out


def people_also_search(p: list) -> list[dict]:
    out = []
    for section in dig(p, 99, default=[]) or []:
        for block in section or []:
            category = clean_text(dig(block, 0))
            for entry in dig(block, 1, default=[]) or []:
                q = dig(entry, 1)
                if not q:
                    continue
                out.append({"category": category, "title": clean_text(dig(q, 11)),
                            "reviewsCount": dig(q, 4, 8), "totalScore": dig(q, 4, 7)})
    return out


def image_categories(p: list) -> list[str]:
    return [clean_text(c[2]) for c in dig(p, 171, 0, default=[]) or [] if isinstance(c, list) and len(c) > 2 and c[2]]


def gas_prices(p: list) -> list[dict]:
    out = []
    for g in dig(p, 86, 0, default=[]) or []:
        if not isinstance(g, list) or len(g) < 8:
            continue
        updated = datetime.fromtimestamp(g[2], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if g[2] else None
        out.append({"priceTag": g[0], "updatedAt": updated, "unit": g[3], "currency": g[5], "price": g[6],
                    "gasType": g[7]})
    return out


def _action(p: list, kind: int) -> list | None:
    for entry in dig(p, 75, 0, default=[]) or []:
        if dig(entry, 0) == kind:
            return entry
    return None


def _action_links(entry: list | None) -> list[dict]:
    out = []
    for link in dig(entry, 2, default=[]) or []:
        url = dig(link, 1, 2, 0)
        if url:
            out.append({"name": dig(link, 0, 0) or up.urlparse(url).netloc, "url": url})
    return out


def reservation(p: list) -> dict[str, Any]:
    entry = _action(p, ACTION_RESERVE)
    links = _action_links(entry)
    for link in dig(p, 46, default=[]) or []:
        url = dig(link, 0)
        if url and all(l["url"] != url for l in links):
            links.append({"name": dig(link, 1) or up.urlparse(url).netloc, "url": url})
    reserve_url = dig(entry, 5, 1, 2, 0)
    providers = []
    for link in dig(entry, 2, default=[]) or []:
        name = dig(link, 0, 1, 1) or dig(link, 0, 2, 1)
        if name:
            # providerUrl: the provider's own booking link on the card (native export only)
            providers.append({"name": name, "email": None, "phone": None, "address": None,
                              "reserveTableUrl": reserve_url, "providerUrl": dig(link, 1, 2, 0)})
    return {"tableReservationLinks": links, "reserveTableUrl": reserve_url, "tableReservationProviders": providers}


def booking(p: list) -> tuple[list[dict], str | None]:
    entry = _action(p, ACTION_BOOK)
    return _action_links(entry), dig(entry, 5, 1, 2, 0)


def order_online_url(p: list) -> str | None:
    return dig(_action(p, ACTION_ORDER), 5, 1, 2, 0)


def order_links(p: list) -> list[dict]:
    """Direct ordering links of the order action (GetGo: DoorDash), without Google's widget: the
    action names the provider and the link, not whether it is for pickup or delivery."""
    out = []
    for link in dig(_action(p, ACTION_ORDER), 2, default=[]) or []:
        url = dig(link, 1, 2, 0)
        if url:
            domain = dig(link, 0, 0) or up.urlparse(url).netloc
            out.append({"name": dig(link, 0, 2, 1) or dig(link, 0, 1, 1) or domain, "domain": domain, "url": url})
    return out


def owner_updates(p: list) -> list[dict]:
    out = []
    for post in dig(p, 122, 1, default=[]) or []:
        if not isinstance(post, list):
            continue
        ts = dig(post, 2, 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") if ts else None
        out.append({"text": clean_text(dig(post, 1, 0, 0, 0)), "buttonText": dig(post, 4, 2),
                    "buttonLink": dig(post, 4, 1), "date": date, "imageUrl": dig(post, 5, 0, 0)})
    return out


def updates_from_customers(p: list) -> dict | None:
    post = dig(p, 175, 7, 0, 0, 0)
    if not post:
        return None
    media = []
    for photo in dig(post, 2, 2, default=[]) or []:
        link = dig(photo, 1, 6, 0)
        if link:
            media.append({"link": _strip_image_size(link), "postDate": dig(photo, 1, 21, 6, 8, 8, 0) or dig(post, 1, 6)})
    author = dig(post, 1, 4, 5) or []
    return {"text": clean_text(dig(post, 2, 15, 0, 0)), "language": dig(post, 2, 14, 0),
            "postDate": dig(post, 1, 6),
            "postedBy": {"name": dig(author, 0), "url": dig(author, 2, 0),
                         "title": "Local Guide" if str(dig(author, 10, 0) or "").startswith("Local Guide") else None,
                         "totalReviews": dig(author, 5)},
            "media": media}


def hours_confirmation(p: list) -> str | None:
    """"Updated by this business 12 weeks ago" under the opening hours (``p[203][5][0]``)."""
    text = dig(p, 203, 5, 0)
    return text if isinstance(text, str) and text else None


def reviews_removed_notice(p: list) -> dict[str, str] | None:
    """The card's notice of reviews removed after complaints: in ``p[241][0]`` the entry of type 3
    (type 1 entries are Google's posting policies); Apify reports its short text (``[1][3]``)."""
    for notice in dig(p, 241, 0, default=[]) or []:
        if dig(notice, 0) == 3:
            text = dig(notice, 1, 3) or dig(notice, 1, 0)
            if isinstance(text, str) and text:
                return {"text": text}
    return None


def hotel(p: list) -> dict[str, Any]:
    out: dict[str, Any] = {"hotelStars": None, "hotelDescription": None, "checkInDate": None, "checkOutDate": None,
                           "similarHotelsNearby": [], "hotelAds": [], "hotelReviewSummary": None}
    stars = dig(p, 64, 3)
    if stars:
        out["hotelStars"] = stars
    paragraphs = [x for x in dig(p, 32, 2, 7, 0, default=[]) or [] if isinstance(x, str) and x]
    out["hotelDescription"] = "\n ".join(paragraphs) if paragraphs else None
    booking_block = dig(p, 35)
    if booking_block:
        out["checkInDate"] = dig(booking_block, 0)
        out["checkOutDate"] = dig(booking_block, 1)
        for h in dig(booking_block, 29, 0, default=[]) or []:
            info = dig(h, 0) or []
            out["similarHotelsNearby"].append({"name": dig(info, 4), "rating": dig(info, 7), "reviews": dig(info, 8),
                                               "description": dig(h, 1), "price": dig(h, 3)})
        for ad in dig(booking_block, 44, default=[]) or []:
            out["hotelAds"].append(_hotel_ad(ad))
    return out


def _hotel_ad(ad: list) -> dict:
    google_url = dig(ad, 5, 0)
    target = None
    if isinstance(google_url, str) and "pcurl=" in google_url:
        target = up.parse_qs(up.urlparse(google_url).query).get("pcurl", [None])[0]
    official = any(dig(tag, 0) == "Official site" or dig(tag, 1) == 1 for tag in dig(ad, 11, default=[]) or [])
    return {"title": dig(ad, 0), "googleUrl": google_url, "isOfficialSite": official,
            "price": dig(ad, 1), "url": target}


def main_image(p: list) -> str | None:
    """First gallery photo that is not a Street View thumbnail."""
    for path in ((72, 0), (37, 0), (51, 0)):
        for photo in dig(p, *path, default=[]) or []:
            url = dig(photo, 6, 0)
            if url and "streetviewpixels" not in url:
                return _strip_image_size(url)
    return None


def closed_flags(p: list) -> tuple[bool, bool]:
    """``[23]`` = 1 marks a permanently closed place (Sears Jersey City, rendered page
    "Permanently closed", 2026-10-01).  ``[160]`` = [1] marks a temporarily closed one, in search
    and detail records alike (3 of 3 places the rendered page shows "Temporarily closed", none of
    4 open ones, 2026-10-08); the earlier guess ``[23]`` = 2 was never seen."""
    return dig(p, 23) == 1, dig(p, 160, 0) == 1


# ---------------------------------------------------------------- the place

def parse_place(p: list, *, language: str | None = None, scraped_at: str | None = None,
                source: str = "details") -> tuple[dict[str, Any], FieldErrors]:
    """Return Apify-named fields and per-field parse errors.

    ``source`` is ``"details"`` for ``preview/place`` and ``"search"`` for a
    search item; detail-only fields are left out of search-sourced output so
    an absent value is never confused with "not present at the source".
    """
    errors = FieldErrors()
    for index, name in STRUCTURED_SECTIONS.items():
        value = p[index] if isinstance(p, list) and len(p) > index else None
        if value is not None and not isinstance(value, list):
            errors[name] = f"unexpected {type(value).__name__} at [{index}]"
    fid = dig(p, 10)
    fid = normalize_fid(fid) if is_fid(fid) else fid
    title = clean_text(dig(p, 11))
    place_id = dig(p, 78)
    out: dict[str, Any] = {
        "title": title,
        "subTitle": clean_text(dig(p, 101)),
        "description": clean_text(dig(p, 32, 1, 1)),
        "price": clean_text(_safe(errors, "price", lambda: _price(p))),
        "categoryName": clean_text(dig(p, 13, 0)),
        "categories": [clean_text(c) for c in (dig(p, 13, default=[]) or [])],
        "categoryIds": [dig(c, 0) for c in (dig(p, 76, default=[]) or []) if dig(c, 0)],
        "address": clean_text(dig(p, 39)),
        "neighborhood": clean_text(dig(p, 183, 1, 0)),
        "street": clean_text(dig(p, 183, 1, 1)),
        "city": clean_text(dig(p, 183, 1, 3)),
        "postalCode": dig(p, 183, 1, 4),
        "state": clean_text(dig(p, 183, 1, 5)),
        "countryCode": dig(p, 183, 1, 6) or dig(p, 243),
        "website": dig(p, 7, 0),
        "phone": clean_text(dig(p, 178, 0, 0)),
        "phoneUnformatted": dig(p, 178, 0, 3),
        "plusCode": clean_text(dig(p, 183, 2, 2, 0)),
        "location": _location(p),
        "totalScore": dig(p, 4, 7),
        "reviewsCount": dig(p, 4, 8),
        "placeId": place_id,
        "fid": fid,
        "cid": cid_from_fid(fid),
        "kgmid": dig(p, 89),
        "businessProfileId": dig(p, 181, 5) or dig(p, 227, 0, 5),
        "url": maps_url(title, place_id),
        "imageUrl": _safe(errors, "imageUrl", lambda: main_image(p)),
        "imagesCount": dig(p, 37, 1),
        "locatedIn": clean_text(dig(p, 134, 0, 0, 0, 0)),
        "locatedInFid": dig(p, 134, 0, 0, 0, 1),
        "floor": dig(p, 183, 1, 7, 0) if dig(p, 134, 0, 0, 0, 0) else None,
        "claimThisBusiness": bool(dig(p, 49, 1)),
        "timeZone": dig(p, 30),
        "language": language,
        "scrapedAt": scraped_at,
    }
    permanently, temporarily = _safe(errors, "closed", lambda: closed_flags(p), (False, False))
    out["permanentlyClosed"], out["temporarilyClosed"] = permanently, temporarily
    out["openingHours"] = _safe(errors, "openingHours", lambda: opening_hours(p), [])
    out["openingHoursGoogleOrder"] = _safe(errors, "openingHoursGoogleOrder", lambda: opening_hours_google_order(p), [])
    out["wasOpenAtScrapeTime"] = _safe(errors, "wasOpenAtScrapeTime", lambda: open_now(p))
    out["additionalInfo"] = _safe(errors, "additionalInfo", lambda: additional_info(p))
    out["attributeValues"] = _safe(errors, "attributeValues", lambda: attribute_values(p), {})
    out["gasPrices"] = _safe(errors, "gasPrices", lambda: gas_prices(p), [])
    out["ownerDescription"] = clean_text(dig(p, 154, 0, 0))
    if source == "details":
        hist, live_text, live_pct = _safe(errors, "popularTimes", lambda: popular_times(p), (None, None, None))
        out["popularTimesHistogram"], out["popularTimesLiveText"], out["popularTimesLivePercent"] = hist, live_text, live_pct
        out["additionalOpeningHours"] = _safe(errors, "additionalOpeningHours", lambda: additional_opening_hours(p))
        # Apify writes these two keys only when Google shows them (core-parity 2026-10-14: Matcha Corner,
        # Emmas Kaffee); a missing notice leaves the key out, never null
        confirmation = _safe(errors, "openingHoursBusinessConfirmationText", lambda: hours_confirmation(p))
        if confirmation:
            out["openingHoursBusinessConfirmationText"] = confirmation
        removed = _safe(errors, "reviewsRemovedNotice", lambda: reviews_removed_notice(p))
        if removed:
            out["reviewsRemovedNotice"] = removed
        out["reviewsDistribution"] = _safe(errors, "reviewsDistribution", lambda: reviews_distribution(p))
        out["reviewsTags"] = _safe(errors, "reviewsTags", lambda: reviews_tags(p), [])
        out["placesTags"] = _safe(errors, "placesTags", lambda: places_tags(p), [])
        out["peopleAlsoSearch"] = _safe(errors, "peopleAlsoSearch", lambda: people_also_search(p), [])
        out["imageCategories"] = _safe(errors, "imageCategories", lambda: image_categories(p), [])
        out["ownerUpdates"] = _safe(errors, "ownerUpdates", lambda: owner_updates(p), [])
        out["updatesFromCustomers"] = _safe(errors, "updatesFromCustomers", lambda: updates_from_customers(p))
        res = _safe(errors, "reservation", lambda: reservation(p),
                    {"tableReservationLinks": [], "reserveTableUrl": None, "tableReservationProviders": []})
        out.update(res)
        links, services = _safe(errors, "booking", lambda: booking(p), ([], None))
        out["bookingLinks"] = links
        menu_or_service = dig(p, 38, 0)
        out["menu"] = None
        out["servicesLink"] = None
        if menu_or_service:
            if any(l["url"] == menu_or_service for l in links) or _action(p, ACTION_BOOK) is not None:
                out["servicesLink"] = menu_or_service
            else:
                out["menu"] = menu_or_service
        out["googleFoodUrl"] = _safe(errors, "googleFoodUrl", lambda: order_online_url(p))
        out["orderOnlineLinks"] = _safe(errors, "orderOnlineLinks", lambda: order_links(p), [])
        out.update(_safe(errors, "hotel", lambda: hotel(p), {}))
        # partner review sources Google shows on the card (native only): the trigger and the day dates
        # of the partner list (maps/travel_reviews.py)
        out["partnerReviewSources"] = _safe(errors, "partnerReviewSources", lambda: partner_sources(p), [])
        out["directory"] = _safe(errors, "directory", lambda: directory_fids(p), [])
    return out, errors


def _location(p: list) -> dict[str, float] | None:
    lat, lng = dig(p, 9, 2), dig(p, 9, 3)
    if lat is None or lng is None:
        return None
    return {"lat": round(float(lat), 7), "lng": round(float(lng), 7)}


def _price(p: list) -> str | None:
    """Restaurant price range (``[4][2]``); gas stations and hotels put a price line in ``[88][0]``."""
    level = dig(p, 4, 2)
    if level:
        return level
    kind = str(dig(p, 88, 1) or "")
    line = dig(p, 88, 0)
    if line and kind in ("SearchResult.TYPE_GAS_STATION", "SearchResult.TYPE_LODGING"):
        return line
    return None
