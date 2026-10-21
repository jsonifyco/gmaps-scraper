"""``apify`` export: dataset items shaped like compass/crawler-google-places 0.14.761.

* one item per accepted place (a place found by several searches is written
  once, under the search that found it first);
* at most 5,000 reviews per item -- further reviews go to copies of the item,
  like the actor does; ``oneReviewPerRow`` writes one review per row instead;
* review and image lists are trimmed to ``maxReviews`` / ``maxImages`` unless
  ``bro.reviewsExhaustive`` / ``bro.imagesExhaustive`` asked for everything;
* sections this run could not obtain are left out rather than written as empty
  lists (see the native export for their status and reason);
* ``orderOnline`` and ``tableReservationProviders`` come from their own reads (pipeline/specials.py)
  when ``scrapeOrderOnline`` / ``scrapeTableReservationProvider`` ask for them; ordering links
  whose service Google does not state are listed in ``orderOnline.links``;
* ``questionsAndAnswers`` (``maxQuestions``) comes from Google's local Q&A app (pipeline/questions.py):
  answered questions in Google's order, Apify's shape.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from ..input_model import RunInput
from ..pipeline.questions import questions_export
from ..pipeline.specials import order_online_export, reservation_export
from ..store import Store, loads

REVIEWS_PER_ITEM = 5000

KEY_ORDER = [
    "title", "subTitle", "description", "ownerDescription", "price", "categoryName", "address", "neighborhood",
    "street", "city", "postalCode", "state", "countryCode", "website", "phone", "phoneUnformatted",
    "claimThisBusiness", "location", "locatedIn", "floor", "plusCode", "menu", "servicesLink", "totalScore",
    "permanentlyClosed", "temporarilyClosed", "placeId", "categories", "fid", "cid", "reviewsCount",
    "reviewsDistribution", "imagesCount", "imageCategories", "scrapedAt", "reserveTableUrl", "googleFoodUrl",
    "hotelStars", "hotelDescription", "checkInDate", "checkOutDate", "hotelAds", "popularTimesLiveText",
    "popularTimesLivePercent", "popularTimesHistogram", "openingHours", "wasOpenAtScrapeTime", "peopleAlsoSearch",
    "placesTags", "reviewsTags", "additionalInfo", "gasPrices", "questionsAndAnswers", "url", "searchPageUrl",
    "searchString", "language", "rank", "inputStartUrl", "inputPlaceId", "isAdvertisement", "imageUrl", "kgmid",
    "businessProfileId", "webResults", "parentPlaceUrl", "tableReservationLinks", "tableReservationProviders",
    "bookingLinks", "orderOnline", "restaurantData", "ownerUpdates", "emails", "phones", "phonesUncertain",
    "linkedIns", "twitters", "instagrams", "facebooks", "youtubes", "tiktoks", "pinterests", "discords", "domain",
    "impressum", "imageUrls", "images", "reviews", "leadsEnrichment", "similarHotelsNearby", "updatesFromCustomers",
    "additionalOpeningHours", "reviewsRemovedNotice", "openingHoursBusinessConfirmationText",
]
INTERNAL = {"directory", "attributeValues", "categoryIds", "locatedInFid", "timeZone", "hotelReviewSummary", "partnerReviewSources", "openingHoursGoogleOrder",
            "categoriesQueryFronted", "orderOnlineLinks"}
#: keys of an Apify ``tableReservationProviders`` entry (native extras such as providerUrl stay out)
PROVIDER_KEYS = ("name", "email", "phone", "address", "reserveTableUrl")
SOCIAL_KEYS = ["linkedIns", "twitters", "instagrams", "facebooks", "youtubes", "tiktoks", "pinterests", "discords"]
SORT_ORDER = {"newest": lambda r: r.get("publishedAtDate") or "", }


#: the photometa calendar Apify reports as ``uploadedAt`` (2026-10-10, 439 photo pairs); its
#: meaning is not established -- native ``dateEvidence`` keeps both calendars with ``meaning: unknown``
APIFY_UPLOAD_FIELD = "[1][0][6][7]"


def apify_uploaded_at(image: dict[str, Any]) -> str | None:
    """Apify's ``uploadedAt`` for a photo: the day of the photometa field Apify uses, in Apify's form
    (``YYYY-MM-DDT00:00:00.000Z``); None when photometa was not read or gives only a month."""
    for obs in ((image.get("dateEvidence") or {}).get("observations") or []):
        if obs.get("fieldPath") == APIFY_UPLOAD_FIELD and obs.get("precision") == "day" and obs.get("value"):
            return f"{obs['value']}T00:00:00.000Z"
    return None


def _ordered(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item[k] for k in KEY_ORDER if k in item}
    for k, v in item.items():
        if k not in out:
            out[k] = v
    return out


#: second-level public suffixes under which a registrable domain has three labels (Apify's ``domain``
#: is the registrable domain: deichmann.com for stores.deichmann.com, gap.com for bananarepublic.gap.com)
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "org.nz", "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "com.br", "net.br", "org.br", "com.mx", "org.mx", "co.za", "org.za", "com.tr", "com.cn", "net.cn", "org.cn",
    "com.hk", "co.in", "net.in", "org.in", "co.kr", "or.kr", "com.sg", "com.ar", "com.co", "com.pe", "com.es",
    "org.es", "gob.es", "co.il", "org.il", "com.pl", "co.at", "or.at", "com.ua", "com.ru", "com.tw", "com.my",
    "co.th", "com.vn", "com.ph", "co.id", "com.eg", "com.sa", "co.ke",
}


def registrable_domain(host: str | None) -> str | None:
    """The registrable part of a host (eTLD+1 for common suffixes)."""
    if not host:
        return None
    labels = host.lower().strip(".").split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


#: Apify's empty forms (from reference datasets): keys it
#: leaves out when there is no value, keys it writes as '' or {} or []
APIFY_OMIT_EMPTY = ("additionalInfo", "additionalOpeningHours", "domain", "website", "updatesFromCustomers",
                    "similarHotelsNearby")
APIFY_EMPTY_STRING = ("phone", "phoneUnformatted")


def apify_forms(item: dict[str, Any], *, details: bool) -> dict[str, Any]:
    """An item with Apify's empty forms (core-2026.10): a missing value is left out, '' , {} or [] as
    Apify writes it; ``leadsEnrichment`` (an excluded add-on) is Apify's [] for a read card."""
    out = dict(item)
    for k in APIFY_OMIT_EMPTY:
        if k in out and out[k] in (None, [], {}):
            del out[k]
    for k in APIFY_EMPTY_STRING:
        if k in out and out[k] is None:
            out[k] = ""
    if "popularTimesHistogram" in out and out["popularTimesHistogram"] is None:
        out["popularTimesHistogram"] = {}
    if details:
        out.setdefault("leadsEnrichment", [])
    # Apify's value conventions (stored datasets; the full values stay in the native export):
    # googleFoodUrl is never filled (0 of 4707 items); additional hours keep their first interval (0 of
    # 7371 rows hold a second one, while 2662 regular rows do); an order fee keeps its first part
    if "googleFoodUrl" in out:
        out["googleFoodUrl"] = None
    if isinstance(out.get("additionalOpeningHours"), dict):
        out["additionalOpeningHours"] = {
            group: [{**row, "hours": row["hours"].split(", ")[0]} if isinstance(row.get("hours"), str) else row
                    for row in rows] if isinstance(rows, list) else rows
            for group, rows in out["additionalOpeningHours"].items()}
    if isinstance(out.get("orderOnline"), dict):
        out["orderOnline"] = {kind: [{k: (v.split(" · ")[0] if k.endswith("Fees") and isinstance(v, str) else v)
                                      for k, v in entry.items()} if isinstance(entry, dict) else entry
                                     for entry in rows] if isinstance(rows, list) else rows
                              for kind, rows in out["orderOnline"].items()}
    return out


def _search_string(result: Any) -> str | None:
    key = result["search_key"]
    if key.startswith("term:"):
        return result["search_term"]
    if key.startswith("url:"):
        return f"Direct Detail URL: {key[4:]}" if result["search_term"] is None else result["search_term"]
    if key.startswith("placeId:"):
        return f"Direct Detail URL: https://www.google.com/maps/place/?q=place_id:{key[8:]}"
    return result["search_term"]


def place_view(store: Store, fid: str) -> dict[str, Any]:
    row = store.place(fid)
    if row is None:
        return {}
    data = dict(loads(row["search_data"]) or {})
    details = loads(row["details"])
    if details:
        data.update(details)
    return data


def contacts_view(store: Store, fid: str, *, registrable: bool = False) -> dict[str, Any]:
    rows = store.db.execute("SELECT kind, value, domain, context FROM contacts WHERE fid = ? ORDER BY rowid", (fid,)).fetchall()
    out: dict[str, Any] = {"emails": [], "phones": [], "phonesUncertain": [], **{k: [] for k in SOCIAL_KEYS}}
    domain = None
    for r in rows:
        domain = domain or r["domain"]
        if r["kind"] == "email":
            out["emails"].append(r["value"])
        elif r["kind"] == "phone":
            out["phones"].append(r["value"])
        elif r["kind"] == "phoneUncertain":
            out["phonesUncertain"].append(r["value"])
        elif r["kind"] in SOCIAL_KEYS:
            out[r["kind"]].append(r["value"])
        elif r["kind"] == "impressum":
            out["impressum"] = loads(r["context"])
    out["domain"] = registrable_domain(domain) if registrable else domain
    return out


#: Apify's review keys in its order (every stored dataset); lastEditedAtDate is not among them
APIFY_REVIEW_KEYS = ["reviewerId", "reviewerUrl", "name", "reviewerNumberOfReviews", "isLocalGuide", "reviewerPhotoUrl",
                     "text", "textTranslated", "publishAt", "publishedAtDate", "likesCount", "reviewId", "reviewUrl",
                     "reviewOrigin", "stars", "rating", "responseFromOwnerDate", "responseFromOwnerText", "reviewImageUrls",
                     "reviewContext", "reviewDetailedRating", "visitedIn", "originalLanguage", "translatedLanguage"]


def apify_review(review: dict) -> dict:
    """A review in Apify's form (core-2026.10): publishedAtDate is the time Google shows -- the last
    edit of an edited review (Apify's standard value for edited reviews);
    the original publish time and the edit stay in the native export.  Extensions (publishedOrEdited
    AtDate of fast mode) follow Apify's keys."""
    out = {k: review.get(k) for k in APIFY_REVIEW_KEYS}
    if review.get("lastEditedAtDate"):
        out["publishedAtDate"] = review["lastEditedAtDate"]
    for k, v in review.items():
        if k not in out and k not in ("lastEditedAtDate", "derivedReviewContext"):
            out[k] = v
    return out


def web_results_export(detail: dict, *, core: bool) -> list[dict]:
    """Apify's webResults: under core-2026.10 the first 3 organic results (sitelinks left out) with
    their destination where it was followed, in Apify's key order; rc27 jobs keep every result."""
    results = detail.get("results") or []
    if not core:
        return results
    targets = {l.get("url"): l.get("targetUrl") for l in detail.get("resultLinks") or [] if l.get("targetUrl")}
    out = []
    for r in results:
        if not r.get("main", True):
            continue
        out.append({"title": r.get("title"), "displayedUrl": r.get("displayedUrl"),
                    "url": targets.get(r.get("url")) or r.get("url"), "description": r.get("description")})
        if len(out) >= 3:
            break
    return out


def sorted_reviews(store: Store, fid: str, sort: str, limit: int | None, start=None, *, fast: bool = False,
                   query: str = "", run: RunInput | None = None) -> list[dict]:
    """The place's reviews for export.  Reviews listed through Search get their dates only from Maps
    observations (pipeline/review_dates.py: never Search's ambiguous time); reviewsStartDate is
    applied by exact publish time, and reviews without one are kept (counted in reviewDates).
    ``query`` is the list's key (pipeline/review_travel.list_key): reviews of other lists never enter.
    Under the core-2026.10 contract, Travel's order places partner reviews among Google's or is the
    list itself (Google's review search, rating selection); the limit applies to the combined list."""
    from ..pipeline.review_dates import export_rows
    from ..pipeline.review_travel import ordered_rows, travel_mode
    core = run is not None and run.bro.core_contract
    reviews, _ = export_rows(store, fid, sort, start, fast=fast, query=query, activity=core)
    if core:
        mode = travel_mode(run, store, fid, sort, query)
        # a partner review leaves only by Google's day before the date; without a day it is kept
        reviews = ordered_rows(store, fid, sort, query, reviews, mode=mode,
                               origin_all=run.apify.get("reviewsOrigin") != "google", start=start)
        reviews = [apify_review(r) for r in reviews]
        return reviews if limit is None else reviews[:limit]
    seen = {r["reviewId"] for r in reviews}
    # reviews stored under another order (a fallback pass of the retired Maps route) belong too;
    # they get the same date merge and the same start-date filter
    others = []
    for (other,) in store.db.execute("SELECT DISTINCT sort FROM reviews WHERE fid = ? AND sort != ? "
                                     "AND COALESCE(query, '') = ?", (fid, sort, query or "")):
        rows, _ = export_rows(store, fid, other, start, fast=fast, query=query)
        others += [r for r in rows if r["reviewId"] not in seen and not seen.add(r["reviewId"])]
    if others:
        if sort == "newest":
            # Google's newest order is by last edit time
            reviews = sorted(reviews + others, key=lambda r: r.get("lastEditedAtDate") or r.get("publishedAtDate") or "",
                             reverse=True)
        else:
            reviews = reviews + others
    return reviews if limit is None else reviews[:limit]


def apify_items(store: Store, run: RunInput) -> Iterator[dict[str, Any]]:
    a = run.apify
    rows = store.db.execute(
        "SELECT r.*, d.zoom AS d_zoom, d.is_ad AS d_is_ad FROM results r LEFT JOIN discoveries d "
        "ON d.id = r.first_discovery_id WHERE r.accepted = 1 ORDER BY r.created_at, r.rowid").fetchall()
    seen: set[str] = set()
    reviews_on = run.reviews_limit is None or (run.reviews_limit or 0) > 0
    images_on = run.images_limit is None or (run.images_limit or 0) > 0
    from ..pipeline.review_travel import list_key
    review_query = list_key(run, a.get("reviewsFilterString"))
    for result in rows:
        fid = result["fid"]
        if fid in seen:
            continue
        seen.add(fid)
        data = place_view(store, fid)
        if not data:
            continue
        item = {k: v for k, v in data.items() if k not in INTERNAL}
        item["searchString"] = _search_string(result)
        if result["search_key"].startswith("term:") or result["search_key"] == "allPlaces":
            item["rank"] = result["rank"]
            item["searchPageUrl"] = result["search_page_url"]
        item["isAdvertisement"] = bool(result["d_is_ad"]) if result["d_is_ad"] is not None else False
        if result["input_start_url"]:
            item["inputStartUrl"] = result["input_start_url"]
        if result["input_place_id"]:
            item["inputPlaceId"] = result["input_place_id"]
        if result["parent_fid"]:
            parent = place_view(store, result["parent_fid"])
            item["parentPlaceUrl"] = parent.get("url")
        item["language"] = run.language
        if a["scrapeTableReservationProvider"]:
            enriched = reservation_export(store, fid)  # the reservation page's providers, when read
            if enriched is not None:
                page = [x for x in enriched if ((x.get("sources") or {}).get("name") == "google_reserve_page")]
                # Apify lists the reservation page's providers (Café Momentum: OpenTable, not the card's
                # Tripleseat); the card's providers stay in native, and stand in when the page names none
                item["tableReservationProviders"] = page or enriched if run.bro.core_contract else enriched
        if "tableReservationProviders" in item:
            item["tableReservationProviders"] = [{k: p.get(k) for k in PROVIDER_KEYS}
                                                 for p in item["tableReservationProviders"] or []]
        providers = item.get("tableReservationProviders") or []
        if "tableReservationProviders" in item or a["scrapeTableReservationProvider"]:
            item["restaurantData"] = {"tableReservationProvider": providers[0] if providers else None}
        if a["scrapeContacts"]:
            contacts = contacts_view(store, fid, registrable=run.bro.core_contract)
            impressum = contacts.pop("impressum", None)
            item.update(contacts)
            if impressum:
                item["impressum"] = impressum
        if a["includeWebResults"]:
            sec = store.section(fid, "webResults")
            if sec is not None and sec["status"] in ("done", "empty"):
                item["webResults"] = web_results_export(loads(sec["detail"]) or {}, core=run.bro.core_contract)
        if images_on:
            images = store.images_for(fid)
            if run.images_limit is not None:
                images = images[:run.images_limit]
            item["images"] = [{"imageUrl": i.get("imageUrl"), "authorName": i.get("authorName"),
                               "authorUrl": i.get("authorUrl"),
                               "uploadedAt": i.get("uploadedAt") or apify_uploaded_at(i)} for i in images]
            item["imageUrls"] = [i.get("imageUrl") for i in images]
        if a["maxQuestions"]:
            questions = questions_export(store, fid)  # left out when the read failed or is pending
            if questions is not None:
                item["questionsAndAnswers"] = questions
        elif run.bro.core_contract and (store.place(fid) or {"details": None})["details"]:
            # maxQuestions 0: Apify writes [] for every place whose card it read (stored runs of 2026-10-09
            # and 2026-10-14), although its input text says the first question would be read
            item["questionsAndAnswers"] = []
        item.pop("orderOnline", None)
        if a["scrapeOrderOnline"]:
            order = order_online_export(store, fid)  # left out when the widget read failed or is pending
            if order is not None:
                item["orderOnline"] = order
        reviews = (sorted_reviews(store, fid, a["reviewsSort"], run.reviews_limit, run.reviews_start(),
                                  fast=run.bro.reviewsMode == "fast", query=review_query, run=run) if reviews_on else [])
        if run.bro.core_contract:
            item = apify_forms(item, details=bool((store.place(fid) or {"details": None})["details"]))
            if not run.language.lower().startswith("en") and data.get("openingHoursGoogleOrder"):
                # Apify keeps Google's order and day names for other languages (Monday-first English for en)
                item["openingHours"] = data["openingHoursGoogleOrder"]
        reviews = [{k: v for k, v in r.items() if k != "derivedReviewContext"} for r in reviews]
        if reviews_on and run.legacy.get("oneReviewPerRow"):
            if not reviews:
                yield _ordered({**item, "reviews": []})
            for review in reviews:
                yield _ordered({**item, **review, "reviews": None})
            continue
        if not reviews_on:
            yield _ordered(item)
            continue
        if not reviews:
            yield _ordered({**item, "reviews": []})
            continue
        for start in range(0, len(reviews), REVIEWS_PER_ITEM):
            yield _ordered({**item, "reviews": reviews[start:start + REVIEWS_PER_ITEM]})


def write_json(items: Iterator[dict], path, *, jsonl: bool = False) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        if jsonl:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                count += 1
        else:
            fh.write("[\n")
            for item in items:
                fh.write((",\n" if count else "") + json.dumps(item, ensure_ascii=False))
                count += 1
            fh.write("\n]\n")
    return count
