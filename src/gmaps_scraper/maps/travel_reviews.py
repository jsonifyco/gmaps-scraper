"""Google's review list of the Travel hotel page (``ocp93e``): partner reviews, the mixed order and
Google's own review search.

Google shows partner reviews (Tripadvisor, Trip.com, Priceline, ...) among its own reviews in the
Maps card and on the Travel hotel page.  The Maps list is signed and an anonymous session sees
only five records of it; the Travel page pages the same list with an unsigned batchexecute RPC,
``ocp93e`` of the TravelFrontendUi app, which a same-origin XHR from any google.com page can send
(nothing is signed, edited or replayed: the request is the page's own form, built here).

Request (the RPC's inner array)::

    [null, null, null, <page size <= 200>, <order>, <sources: null = all | [ids]>, null,
     "<text query>", "<entity>", "<cursor | ''>", null, [[]], null, ""]

* order: 1 most helpful (= Apify's mostRelevant), 2 most recent (newest), 3 highest, 4 lowest;
* sources: Google -1, Tripadvisor 100532569, Trip.com 84, Priceline 220 (the ids Google uses in
  the card's own source menu); ``null`` lists every source;
* the text query is Google's review search -- the reviews Apify returns for reviewsFilterString
  (guava 3/3, выставка 62/62, 御朱印 55/55, no match in translations), not a local match;
* entity: base64url of the protobuf {1: {1: <cid>}, 2: 1} (the Travel page adds the kgmid; the
  list is the same without it).

Answer: ``[[records, null, null, null, "<query echo>", "<next cursor>", null, [meta]]]``; the
cursor is ``"<key>:<count>"`` and the last page has ``""``.  A record is::

    [[source name, null, icon, kind (1 Google, 2 partner), source id],
     [[author, author or partner URL, [photo, w, h]], relative time, [rating, scale], text blocks,
      partner review URL, ..., review id at [8], ...], report URL, meta]

Review ids are the ids Apify and Search use, for Google and partner reviews alike; the Google
records of a page must belong to the place's own Search list (identity check of the caller).
The record has no absolute time: a partner review keeps Google's relative time, and a day date
only where Google showed one elsewhere (the card's "reviews on other sites" block).

Newest and most helpful lists are complete (the count Google shows, an empty last cursor; Google's
part equals Search's list in the same order).  Highest/lowest are a selection Google makes: the
same set in both directions, ordered by rating and, inside one rating, by helpfulness; they end
long before the count (Arepittas 677 of 1182) -- an end, never proof of completeness.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .xssi import dig

log = logging.getLogger(__name__)

#: b (rc32): a partner review takes the card sample's day only when both links name the same review
#: 2026.10.15 (rc35): the search highlight <b>..</b> is removed from texts
PARSER_VERSION = "travel-reviews-2026.10.15"
RPC = "ocp93e"
#: Apify sort -> the RPC's order code
ORDER = {"mostRelevant": 1, "newest": 2, "highestRanking": 3, "lowestRanking": 4}
GOOGLE_SOURCE = -1
#: the page accepts up to 200 records per request (verified 50/100/200, 2026-10-14)
PAGE_SIZE = 200
#: a partner reviewer's photo in the size Apify reports (Travel serves 40 px)
PARTNER_PHOTO_SIZE = "s1920-c-br100"


class TravelUnreadable(Exception):
    """An answer that is not a review page of the expected shape; ``raw`` keeps its text."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


class TravelRefused(Exception):
    """Google answered with its unusual-traffic page or HTTP 429."""


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def entity_token(fid: str) -> str:
    """The Travel entity of a place: base64url({1: {1: cid}, 2: 1}) from the fid's cid half."""
    try:
        cid = int(fid.split(":")[1], 16)
    except (IndexError, ValueError) as exc:
        raise ValueError(f"not a fid: {fid!r}") from exc
    inner = b"\x08" + _varint(cid)
    return base64.urlsafe_b64encode(b"\x0a" + _varint(len(inner)) + inner + b"\x10\x01").decode().rstrip("=")


def request_inner(fid: str, *, order: int, cursor: str | None = None, query: str = "",
                  sources: list[int] | None = None, page_size: int = PAGE_SIZE) -> list:
    return [None, None, None, page_size, order, sources, None, query or "", entity_token(fid), cursor or "",
            None, [[]], None, ""]


def source_path(fid: str) -> str:
    return f"/travel/hotels/entity/{entity_token(fid)}/reviews"


#: one request per call: the RPC's own form, from the worker's google.com tab
FETCH_JS = r"""(() => { const u = '/_/TravelFrontendUi/data/batchexecute?rpcids=ocp93e&source-path=' + encodeURIComponent(%s) + '&hl=' + %s + '&gl=' + %s + '&soc-app=162&soc-platform=1&soc-device=1&rt=c'; const x = new XMLHttpRequest(); x.open('POST', u, false); x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8'); x.setRequestHeader('X-Same-Domain', '1'); try { x.send('f.req=' + encodeURIComponent(%s) + '&'); } catch (e) { return {error: String(e)}; } return {s: x.status, t: x.responseText}; })()"""


def fetch_js(fid: str, inner: list, *, hl: str, gl: str) -> str:
    freq = json.dumps([[[RPC, json.dumps(inner, separators=(",", ":")), None, "generic"]]], separators=(",", ":"))
    return FETCH_JS % (json.dumps(source_path(fid)), json.dumps(hl), json.dumps(gl), json.dumps(freq))


@dataclass
class TravelRecord:
    review_id: str
    source: str
    source_id: int
    kind: int
    author: str | None
    author_url: str | None
    photo: str | None
    relative: str | None
    rating: int | float | None
    scale: int | float | None
    text: str | None
    partner_url: str | None
    raw: list = field(repr=False, default_factory=list)

    @property
    def is_google(self) -> bool:
        return self.source_id == GOOGLE_SOURCE

    @property
    def origin_key(self) -> str:
        """The internal source namespace of the id: ``google`` or the partner's name."""
        return "google" if self.is_google else self.source


@dataclass
class TravelPage:
    records: list[TravelRecord]
    next_cursor: str | None
    query_echo: str | None = None
    #: records dropped before parsing because their id repeats within the page
    repeated: int = 0
    raw: str = field(repr=False, default="")


#: text block types: 1 the text; 2 / 3 a partner's "positive" / "negative" parts (Priceline), which
#: Apify reports as 'Positive: … Negative: …' -- the form the card shows too
TEXT_LABELS = {2: "Positive", 3: "Negative"}


#: Google's review search marks the matched words in both text variants of a record (<b>Frühstück</b>,
#: final rc34 comparison 2026-10-15); a review's own "<" arrives escaped (&lt;), so the bare tags are markup
HIGHLIGHT_RE = re.compile(r"</?b>")


def strip_highlight(text: str) -> str:
    return HIGHLIGHT_RE.sub("", text)


def _text(blocks: Any) -> str | None:
    """The review text from its blocks: the full variant of each (Google serves <br> for breaks and
    HTML entities), labelled parts in Apify's form."""
    parts, labelled = [], False
    for block in dig(blocks, 0, default=[]) or []:
        kind, full = dig(block, 0), dig(block, 1)
        if not isinstance(full, str) or not full:
            continue
        text = html.unescape(strip_highlight(full).replace("<br>", "\n"))
        if kind in TEXT_LABELS:
            labelled = True
            parts.append(f"{TEXT_LABELS[kind]}: {text.strip()}")
        else:
            parts.append(text)
    if not parts:
        return None
    return " ".join(parts) if labelled else "\n".join(parts)


def _record(rec: Any, index: int, raw: str) -> TravelRecord:
    src, body = dig(rec, 0), dig(rec, 1)
    if not isinstance(src, list) or not isinstance(body, list):
        raise TravelUnreadable(f"record {index} has no source/body blocks", raw)
    name, kind, sid = dig(src, 0), dig(src, 3), dig(src, 4)
    rid = dig(body, 8)
    if not isinstance(name, str) or not name or not isinstance(sid, int) or isinstance(sid, bool):
        raise TravelUnreadable(f"record {index} has no source name/id", raw)
    if not isinstance(rid, str) or not rid:
        raise TravelUnreadable(f"record {index} has no review id", raw)
    rating, scale = dig(body, 2, 0), dig(body, 2, 1)
    for value in (rating, scale):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise TravelUnreadable(f"record {index} has a rating that is not a number", raw)
    author = dig(body, 0) or []
    relative = dig(body, 1)
    return TravelRecord(review_id=rid, source=name, source_id=sid, kind=kind if isinstance(kind, int) else 0,
                        author=dig(author, 0), author_url=dig(author, 1), photo=dig(author, 2, 0),
                        relative=relative if isinstance(relative, str) else None, rating=rating, scale=scale,
                        text=_text(dig(body, 3)), partner_url=dig(body, 4) or None, raw=rec)


def parse_answer(text: str) -> TravelPage:
    """Records and next cursor of one ocp93e answer.  A cursor that is not a string, a record without
    its source or id, or a missing payload is a schema change (TravelUnreadable), never the end."""
    if not text.startswith(")]}'"):
        if "/sorry/" in text[:3000] or "unusual traffic" in text[:5000].lower():
            raise TravelRefused("Google answered with its unusual-traffic page")
        raise TravelUnreadable(f"not a batchexecute answer: {text[:80]!r}", text)
    payload, errors = None, []
    for line in text.split("\n"):
        if not line.startswith("[["):
            continue
        try:
            envelopes = json.loads(line)
        except ValueError:
            continue
        for env in envelopes:
            if isinstance(env, list) and len(env) > 2 and env[0] == "wrb.fr" and env[1] == RPC:
                if isinstance(env[2], str):
                    payload = env[2]
                else:
                    errors.append(json.dumps(env)[:200])
    if payload is None:
        raise TravelUnreadable("no ocp93e payload" + (f": {errors[0]}" if errors else ""), text)
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise TravelUnreadable(f"payload is not JSON: {exc}", text) from exc
    top = dig(data, 0)
    if not isinstance(top, list) or len(top) < 6:
        raise TravelUnreadable("answer has no list block of 6+ elements at [0]", text)
    records, nxt = top[0], top[5]
    if records is None:
        records = []
    if not isinstance(records, list):
        raise TravelUnreadable(f"records at [0][0] are a {type(records).__name__}", text)
    if nxt is not None and not isinstance(nxt, str):
        raise TravelUnreadable(f"next cursor at [0][5] is a {type(nxt).__name__}, not a string", text)
    parsed, seen, repeated = [], set(), 0
    for i, rec in enumerate(records):
        r = _record(rec, i, text)
        key = (r.origin_key, r.review_id)
        if key in seen:
            repeated += 1
            continue
        seen.add(key)
        parsed.append(r)
    echo = top[4] if isinstance(top[4], str) else None
    return TravelPage(parsed, nxt or None, query_echo=echo, repeated=repeated, raw=text)


def _photo(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    return re.sub(r"=s\d+-c-br100$", "=" + PARTNER_PHOTO_SIZE, url)


#: "(Translated by Google) <translation>\n(Original)\n<original>" in any language (Japanese uses full-width
#: brackets): Travel serves a partner review this way when the request's language is not the review's
TRANSLATED_RE = re.compile(r"^[(（][^)）\n]*Google[^)）\n]*[)）]\s*(?P<translated>.*?)\n[(（][^)）\n]+[)）]\n(?P<original>.*)$", re.S)
#: languages that write a decimal comma (Apify shows a Trip.com score as "4,7/5" in Spanish)
DECIMAL_COMMA = {"de", "fr", "es", "it", "pt", "nl", "ru", "pl", "tr", "cs", "sk", "sv", "da", "nb", "no", "fi", "uk",
                 "ro", "hu", "el", "bg", "hr", "sl", "sr", "lt", "lv", "et", "id", "vi", "ca"}


def split_translation(text: str | None) -> tuple[str | None, str | None]:
    """(original, Google's translation) of a text that carries both; (text, None) otherwise."""
    if not text:
        return text, None
    m = TRANSLATED_RE.match(text)
    if not m:
        return text, None
    return m.group("original").strip(), m.group("translated").strip()


def partner_review(rec: TravelRecord, *, day_date: str | None = None, personal_data: bool = True,
                   hl: str = "en") -> dict[str, Any]:
    """A partner review in Apify's review fields.  ``day_date`` is Google's day of the review when it
    was observed (YYYY-MM-DDT23:59:59.999Z, Apify's form); otherwise the date stays null and the
    relative time is kept -- a relative text is never turned into a moment."""
    rating = None
    if rec.rating is not None and rec.scale is not None:
        rating = f"{_num(rec.rating)}/{_num(rec.scale)}"
        if hl.split("-")[0].lower() in DECIMAL_COMMA:
            rating = rating.replace(".", ",")
    text, translated = split_translation(rec.text)
    review: dict[str, Any] = {
        "reviewerId": None,
        # Apify gives the partner's review link here too (the author has no profile on Google)
        "reviewerUrl": rec.author_url or rec.partner_url,
        "name": rec.author,
        "reviewerNumberOfReviews": None,
        "isLocalGuide": False,
        "reviewerPhotoUrl": _photo(rec.photo),
        "text": text,
        "textTranslated": translated,
        "publishAt": rec.relative,
        "publishedAtDate": day_date,
        "lastEditedAtDate": None,
        "likesCount": 0,
        "reviewId": rec.review_id,
        "reviewUrl": rec.partner_url,
        "reviewOrigin": rec.source,
        "stars": None,
        "rating": rating,
        "responseFromOwnerDate": None,
        "responseFromOwnerText": None,
        "reviewImageUrls": [],
        "reviewContext": {},
        "reviewDetailedRating": {},
        "visitedIn": None,
        "originalLanguage": None,
        "translatedLanguage": hl if translated else None,
    }
    if not personal_data:
        for key in ("reviewerUrl", "name", "reviewerPhotoUrl", "reviewUrl"):
            review[key] = None
    return review


def _num(value: int | float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def google_stub(rec: TravelRecord, fid: str, *, hl: str, personal_data: bool = True) -> dict[str, Any]:
    """A Google review known only from Travel (Search did not list it): the fields Travel carries,
    the rest null; its Maps single-review page can still give the exact dates."""
    reviewer_id = None
    m = re.search(r"/contrib/(\d+)", rec.author_url or "")
    if m:
        reviewer_id = m.group(1)
    review = {
        "reviewerId": reviewer_id,
        "reviewerUrl": f"https://www.google.com/maps/contrib/{reviewer_id}?hl={hl}" if reviewer_id else None,
        "name": rec.author, "reviewerNumberOfReviews": None, "isLocalGuide": False,
        "reviewerPhotoUrl": re.sub(r"=s\d+-", "=s120-", rec.photo, count=1) if rec.photo else None,
        "text": rec.text, "textTranslated": None, "publishAt": rec.relative, "publishedAtDate": None,
        "lastEditedAtDate": None, "likesCount": 0, "reviewId": rec.review_id,
        "reviewUrl": maps_review_url(rec.review_id, fid, hl=hl), "reviewOrigin": "Google",
        "stars": int(rec.rating) if isinstance(rec.rating, (int, float)) else None, "rating": None,
        "responseFromOwnerDate": None, "responseFromOwnerText": None, "reviewImageUrls": [], "reviewContext": {},
        "reviewDetailedRating": {}, "visitedIn": None, "originalLanguage": None, "translatedLanguage": None,
    }
    if not personal_data:
        for key in ("reviewerId", "reviewerUrl", "name", "reviewerPhotoUrl", "reviewUrl"):
            review[key] = None
    return review


def maps_review_url(review_id: str, fid: str, *, hl: str) -> str | None:
    """The Maps single-review link of a Google review id (the form Search and Apify give):
    ``…/reviews/data=!4m8!14m7!1m6!2m5!1s<id>!2m1!1s0x0:<cid>!3m1!1s2@1:<inner id>%7C%7C?hl=``."""
    try:
        raw = base64.urlsafe_b64decode(review_id + "=" * (-len(review_id) % 4))
        if raw[0] != 0x0A:
            return None
        length, pos = raw[1], 2
        inner = raw[pos:pos + length].decode("ascii")
        cid = fid.split(":")[1]
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    return (f"https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5!1s{review_id}!2m1!1s0x0:{cid}"
            f"!3m1!1s2@1:{inner}%7C%7C?hl={hl}")


def stable_key(rec: TravelRecord) -> str:
    """An identity for a partner record without an id (never seen; kept so such a record is not
    merged with another): source + author + partner URL + text."""
    basis = "\x1f".join(str(x or "") for x in (rec.source, rec.author, rec.partner_url, rec.text))
    return "noid:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


class TravelReviewsLister:
    """Pages one place's Travel review list from the worker's google.com tab."""

    def __init__(self, maps: Any, fid: str, *, sort: str = "newest", query: str = "",
                 sources: list[int] | None = None, page_size: int = PAGE_SIZE, timeout: float = 45.0, tries: int = 2):
        if sort not in ORDER:
            raise ValueError(f"unknown review order {sort!r}")
        self.maps, self.fid, self.order = maps, fid, ORDER[sort]
        self.query, self.sources, self.page_size = query or "", sources, page_size
        self.timeout, self.tries = timeout, tries

    def page(self, cursor: str | None, *, home: Any = None) -> TravelPage:
        if home is not None:
            home()
        inner = request_inner(self.fid, order=self.order, cursor=cursor, query=self.query, sources=self.sources,
                              page_size=self.page_size)
        code = fetch_js(self.fid, inner, hl=self.maps.hl, gl=self.maps.gl)
        last: Exception | None = None
        for _attempt in range(self.tries):
            try:
                ans = self.maps.js(code, timeout=self.timeout) or {}
            except RuntimeError as exc:  # run_js failed / timed out in the page; bro errors propagate
                last = exc
                continue
            status, text = ans.get("s"), ans.get("t") or ""
            if status == 200:
                return parse_answer(text)
            if status == 429 or "/sorry/" in text[:3000]:
                raise TravelRefused(f"Travel reviews answered HTTP {status}")
            last = RuntimeError(f"Travel reviews answered HTTP {status}: {ans.get('error') or text[:120]!r}")
        raise last or RuntimeError("Travel reviews request failed")


# --------------------------------------------------------------------------- the card's sources block
def day_from_local_midnight_ms(ms: Any) -> str | None:
    """The card's partner sample carries the review day as local midnight in ms; Apify reports the
    day as its end in UTC form.  +12 h gives the local day for every offset within (-12 h, +12 h)."""
    from datetime import datetime, timedelta, timezone
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    try:
        day = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(hours=12)).date()
    except (OverflowError, OSError, ValueError):
        return None
    return f"{day.isoformat()}T23:59:59.999Z"


def partner_sources(p: list) -> list[dict[str, Any]]:
    """The card's "reviews on other travel sites" block (``p[175][12]``): one entry per partner
    source with its id, name, review count, scale, average and one sample review with its day."""
    out = []
    for entry in dig(p, 175, 12, default=[]) or []:
        info = dig(entry, 1, 0) or []
        sid, name = dig(info, 0), dig(info, 1)
        if not isinstance(sid, int) or not isinstance(name, str):
            continue
        sample = dig(entry, 0, 0) or []
        out.append({
            "sourceId": sid, "name": name, "reviewsCount": dig(entry, 1, 1), "scale": dig(entry, 1, 2),
            "average": dig(entry, 1, 3), "url": dig(entry, 1, 5) or None,
            "sample": {"url": dig(sample, 0), "relative": dig(sample, 1), "author": dig(sample, 2),
                       "text": dig(sample, 3), "rating": dig(sample, 6), "dayDate": day_from_local_midnight_ms(dig(sample, 7)),
                       "dayMs": dig(sample, 7)} if sample else None,
        })
    return out


#: partner links that name one review: source id -> (host, the review's own id in the link).  Tripadvisor's
#: link carries the hotel and the review (ShowUserReviews-g<geo>-d<hotel>-r<review>); the links of
#: Trip.com and Priceline name only the hotel (every review of it has the same link, 2026-10-14)
REVIEW_LINKS = {100532569: ("tripadvisor.", re.compile(r"-d(\d+)-r(\d+)"))}


def partner_review_ref(source_id: int | None, url: str | None) -> str | None:
    """The partner's own identity of one review read from its link, or None when the link does not
    name a single review.  The card's sample and a Travel record are the same publication only when
    both links give the same identity: an author's display name or the text never prove it."""
    rule = REVIEW_LINKS.get(source_id) if isinstance(source_id, int) else None
    if rule is None or not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if rule[0] not in parts.netloc.lower():
        return None
    m = rule[1].search(parts.path)
    return f"{m.group(1)}-{m.group(2)}" if m else None
