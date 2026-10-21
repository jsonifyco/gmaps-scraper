"""Reviews from Google Search's reviews endpoint (GetLocalBoqProxy) -> Apify review fields.

The endpoint is the one Search's "N Google reviews" dialog pages with.
Its record layout differs from the Maps ``qv9Egd`` record; field positions were located by
matching 2 393 reviews of ROW NYC against the Maps records of the same ids (2026-10-06, see
devtools/compare_search_maps.py):

    [1] stars                    [2] [relative date, ?, "<ms>"]   (ms = last edit, else publish)
    [3] [name, photo, "…/contrib/<id>/reviews?hl=", nReviews, nPhotos, [?, isLocalGuide]]
    [4] owner reply: [?, relative date, text, ?, language name, translated text, ?, target language]
    [5] review id                [12] review URL                    [14] photos: [[url, …], …]
    [26] original language code  [27] text                          [30] guided questions
    [32] the visit, as Google words it ("Visité en juillet 2022"; '' when not shown -- served in fr sessions,
         final rc34 comparison 2026-10-15)
    [36] original language name  [37] translated text               [41] target language name
    [44] [origin, …]             [47] [?, [[reaction type, count], …]] (type 4 = likes)

What the endpoint does not carry (compared with Maps): the original publish time of an edited
review (only the edit time) and the absolute time of the owner's reply (only a relative date).
Its one time (``list_time_ms``: the last edit, else the publish time) is therefore kept apart and
never exported as ``publishedAtDate``; exact dates come from Maps (pipeline/review_dates.py).

The list itself is paged here too (:class:`SearchReviewsLister`): the request carries no
per-request signature, so it is sent from a google.com tab of the worker's session with a
synchronous XHR.  The cursor is a session-independent position key, so an interrupted list
continues from its last stored cursor in any session.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import urllib.parse as up
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..bro.transport import command
from .reviews import guided_questions
from .xssi import dig

log = logging.getLogger(__name__)

PARSER_VERSION = "search-reviews-2026.10.15"  # free-form guided context; visitedIn from [32] (rc35)
ENDPOINT = "https://www.google.com/httpservice/web/PrivateLocalSearchUiDataService/GetLocalBoqProxy"
#: list orders; the same codes as Maps (verified on Arepittas 2026-10-07: 3 = only 5-star first,
#: 4 = only 1-star first, 2 = newest, 1 = most relevant)
ORDER = {"mostRelevant": 1, "newest": 2, "highestRanking": 3, "lowestRanking": 4}
#: the endpoint returns at most 60 reviews per request (asking for more still gives 60)
PAGE_SIZE = 60

CONTRIB_RE = re.compile(r"/contrib/(\d+)")
#: Maps asks hotel guests one multi-select question whose answer lists the topics they wrote tips
#: about; Search serves one entry per topic instead (code HOTELS_TIPS_TOPICS_<TOPIC>, the tip
#: text at [10]).  The question text below is Maps' English label, used as the Apify key.
TIPS_PREFIX = "HOTELS_TIPS_TOPICS_"
TIPS_QUESTION_EN = "Can you share more about these topics?"


def _iso_ms(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str | None:
    """Search serves line breaks as ``<br>`` (and could escape HTML); Maps serves plain text."""
    if not isinstance(value, str) or not value:
        return None
    return html.unescape(value.replace("<br>", "\n"))


def _likes(record: list) -> int:
    # reaction type 4 equals Maps' likesCount (type 3 is another reaction Maps does not show)
    for pair in dig(record, 47, 1, default=[]) or []:
        if isinstance(pair, list) and len(pair) >= 2 and pair[0] == 4:
            return int(pair[1] or 0)
    return 0


def parse_search_review(record: list, *, hl: str = "en", personal_data: bool = True) -> dict[str, Any]:
    """One GetLocalBoqProxy review record -> the same fields as ``reviews.parse_review``."""
    author = dig(record, 3) or []
    contrib = dig(author, 2) or ""
    m = CONTRIB_RE.search(contrib)
    reviewer_id = m.group(1) if m else None
    photo = dig(author, 1)
    if isinstance(photo, str):
        photo = re.sub(r"=s\d+-", "=s120-", photo, count=1)  # Maps serves the 120 px variant
    owner = dig(record, 4) or []
    owner_text = _text(dig(owner, 2))
    translated = _text(dig(record, 37))
    text = _text(dig(record, 27))
    origin = dig(record, 44, 0)
    images = [dig(p, 0) for p in (dig(record, 14, default=[]) or []) if isinstance(p, list) and dig(p, 0)]
    questions = dig(record, 30, default=[]) or []
    ratings, context = guided_questions(questions)
    visited = dig(record, 32)
    tips = [dig(q, 1) for q in questions if isinstance(q, list) and str(dig(q, 0, 0) or "").startswith(TIPS_PREFIX)
            and dig(q, 1)]
    derived_context = {"topics": tips, "source": "google_search", "derived": True} if tips else None
    review: dict[str, Any] = {
        "reviewId": dig(record, 5),
        "reviewerId": reviewer_id,
        "reviewerUrl": f"https://www.google.com/maps/contrib/{reviewer_id}?hl={hl}" if reviewer_id else None,
        "name": dig(author, 0),
        "reviewerNumberOfReviews": dig(author, 3),
        "isLocalGuide": bool(dig(author, 5, 1)),
        "reviewerPhotoUrl": photo,
        "text": text,
        "textTranslated": translated if translated and translated != text else None,
        "publishAt": dig(record, 2, 0),
        # the endpoint's one time is the last edit, else the publish time: not a publish date
        # (see list_time_ms); exact dates are filled from Maps
        "publishedAtDate": None,
        "lastEditedAtDate": None,
        "likesCount": _likes(record),
        "reviewUrl": re.sub(r"([?&]hl=)[^&]*", lambda m: m.group(1) + hl, dig(record, 12)) if dig(record, 12) else None,
        "reviewOrigin": origin,
        "stars": dig(record, 1),
        "rating": None,
        "responseFromOwnerDate": None,  # only a relative date is served: dig(owner, 1)
        "responseFromOwnerText": owner_text,
        "reviewImageUrls": images,
        "reviewContext": context,
        "derivedReviewContext": derived_context,
        "reviewDetailedRating": ratings,
        "visitedIn": visited.strip() or None if isinstance(visited, str) else None,
        "originalLanguage": dig(record, 26),
        "translatedLanguage": None,
    }
    if review["textTranslated"]:
        review["translatedLanguage"] = hl  # [41] is the language name ("English"), the target is hl
    if not personal_data:
        for key in ("reviewerId", "reviewerUrl", "name", "reviewerPhotoUrl", "reviewUrl"):
            review[key] = None
    return review


def list_time_ms(record: list) -> int | None:
    """The record's only time: the last edit of an edited review, else its publish time."""
    try:
        return int(dig(record, 2, 2))
    except (TypeError, ValueError):
        return None


def owner_hash(text: str | None) -> str | None:
    """A short fingerprint of the owner's reply text (a changed reply asks for its date again)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] if text else None


# ---------------------------------------------------------------------------- list paging
def reqpld(fid: str, order: int, cursor: str | None, page_size: int = PAGE_SIZE) -> list:
    """The dialog's own request payload; the cursor goes without Maps' ":<count>" suffix."""
    inner = [None, order, None, None, None, None, None, None, None, page_size, None, [fid],
             None, None, None, None, [1, 1, None, [[3], [4], [5], [6], [7]]], None, None,
             (cursor.split(":")[0] if cursor else None), None, None, None, 0]
    return [None, [None] * 9 + [inner]]


def page_url(fid: str, order: int, cursor: str | None, *, hl: str, gl: str, page_size: int = PAGE_SIZE) -> str:
    payload = json.dumps(reqpld(fid, order, cursor, page_size), separators=(",", ":"))
    return ENDPOINT + "?" + up.urlencode({"gl": gl, "hl": hl, "reqpld": payload})


FETCH_JS = r"""(() => { const x = new XMLHttpRequest(); x.open('GET', %s, false); x.withCredentials = true;
  try { x.send(); } catch (e) { return {error: String(e)}; }
  return {s: x.status, t: x.responseText}; })()"""


class SearchRefused(Exception):
    """Search answered with its unusual-traffic page (a property of the session's IP)."""


class SearchUnreadable(Exception):
    """An answer that is neither a review page nor a known refusal; ``raw`` keeps its text."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


@dataclass
class SearchPage:
    records: list[list]
    next_cursor: str | None
    raw: str = field(repr=False, default="")

    @property
    def ids(self) -> list[str]:
        return [dig(r, 5) for r in self.records]


def parse_list_answer(text: str) -> SearchPage:
    """Records and the next cursor of one GetLocalBoqProxy answer.

    ``data[1][10]`` = [?, ?, [records], ?, ?, ?, "<next cursor>"]; the last page has an empty
    cursor ``""`` (every list read on 2026-10-07: 422 pages, block of 7, records a list, cursor a
    string); a JSON null there is taken as the same end.  Any other shape -- a cursor that is not a string, a missing block element, a record
    without its id -- is a schema change, not the end of the list: SearchUnreadable, with the text.
    A refusal page raises SearchRefused."""
    if not text.startswith(")]}'"):
        if "/sorry/" in text[:3000] or "unusual traffic" in text[:5000].lower():
            raise SearchRefused("Search answered with its unusual-traffic page")
        raise SearchUnreadable(f"not a list answer: {text[:80]!r}", text)
    try:
        data = json.loads(text[text.index("\n") + 1:])
    except ValueError as exc:
        raise SearchUnreadable(f"answer is not JSON: {exc}", text) from exc
    block = dig(data, 1, 10)
    if not isinstance(block, list) or len(block) < 7:
        raise SearchUnreadable("answer has no review block of 7 elements at [1][10]", text)
    records, nxt = block[2], block[6]
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SearchUnreadable(f"review records at [1][10][2] are a {type(records).__name__}", text)
    bad = [i for i, r in enumerate(records) if not (isinstance(r, list) and isinstance(dig(r, 5), str) and dig(r, 5))]
    if bad:
        raise SearchUnreadable(f"{len(bad)} of {len(records)} records without a review id (first at {bad[0]})", text)
    if nxt is not None and not isinstance(nxt, str):
        raise SearchUnreadable(f"next cursor at [1][10][6] is a {type(nxt).__name__}, not a string", text)
    return SearchPage(records, nxt or None, raw=text)


class SearchReviewsLister:
    """Pages one place's review list through the Search endpoint from the worker's session."""

    HOME = "https://www.google.com/?hl={hl}&gl={gl}"

    def __init__(self, maps: Any, fid: str, *, sort: str = "newest", timeout: float = 45.0, tries: int = 2):
        if sort not in ORDER:
            raise ValueError(f"unknown review order {sort!r}")
        self.maps, self.fid, self.order = maps, fid, ORDER[sort]
        self.timeout, self.tries = timeout, tries

    def ensure_home(self) -> None:
        """Requests go out from a google.com tab (same origin); other tasks may have moved it."""
        browser = self.maps.browser
        home = self.HOME.format(hl=self.maps.hl, gl=self.maps.gl)
        if getattr(browser, "current_url", None) != home:
            browser.execute([command("open_url", url=home), command("sleep", wait_time=2.0)], timeout=120)
            browser.current_url = home
            browser.review_page_ready = False  # the single-review reader must reopen its page

    def page(self, cursor: str | None) -> SearchPage:
        self.ensure_home()
        url = page_url(self.fid, self.order, cursor, hl=self.maps.hl, gl=self.maps.gl)
        last: Exception | None = None
        for _attempt in range(self.tries):
            try:
                ans = self.maps.js(FETCH_JS % json.dumps(url), timeout=self.timeout) or {}
            except RuntimeError as exc:  # run_js failed / timed out in the page; bro errors propagate
                last = exc
                continue
            status, text = ans.get("s"), ans.get("t") or ""
            if status == 200:
                return parse_list_answer(text)
            if status == 429 or "/sorry/" in text[:3000]:
                raise SearchRefused(f"Search answered HTTP {status}")
            last = RuntimeError(f"Search reviews answered HTTP {status}: {ans.get('error') or text[:120]!r}")
        raise last or RuntimeError("Search reviews request failed")
