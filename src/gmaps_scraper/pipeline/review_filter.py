"""``reviewsFilterString``: a local, deterministic phrase filter over the Search review list.

No request field of Google Search's review endpoint filters by keyword (2026-10-07,
devtools/search_capabilities_probe.py), so the whole list is read and every review is matched
here (method ``local_phrase``):

* the phrase and the texts are normalised alike: Unicode NFKC, case folded (``str.casefold``),
  every run of whitespace turned into one space, the ends trimmed;
* diacritics stay (``café`` does not match ``cafe``); no regular expressions, no morphology, no
  search operators: the normalised phrase must occur as one piece;
* the original text and the translation are matched separately (a phrase is never matched across
  the two); the owner's reply and the author's name take no part;
* a phrase empty after normalisation turns the filter off.

Google's own review search (the one Apify's actor drives) may follow other rules, so the selected
reviews can differ from Apify's; the section detail records the method, the normalised phrase and
the algorithm version (:data:`ALGORITHM`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: version of the matching rules, recorded with every filtered list
ALGORITHM = "local-phrase-2026.10.09"
METHOD = "local_phrase"
#: Google limits the filter to 500 bytes (input schema of Apify 0.14.761)
MAX_BYTES = 500

_SPACE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """NFKC, case folded, whitespace runs collapsed to one space, trimmed."""
    if not text:
        return ""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def phrase_problem(raw: Any) -> str | None:
    """Why ``reviewsFilterString`` is not usable (None when it is, or when it is empty)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return "reviewsFilterString: expected a string"
    size = len(raw.encode("utf-8"))
    if size > MAX_BYTES:
        return f"reviewsFilterString: {size} bytes in UTF-8; Google limits the filter to {MAX_BYTES} bytes"
    return None


def match(review: dict[str, Any], phrase: str) -> str | None:
    """Where the normalised ``phrase`` occurs: 'text', 'translation' or None."""
    if not phrase:
        return "text"
    if phrase in normalize(review.get("text")):
        return "text"
    if phrase in normalize(review.get("textTranslated")):
        return "translation"
    return None


def describe(raw: str | None) -> dict[str, Any] | None:
    """The record kept with a filtered list (section detail ``filter``)."""
    phrase = normalize(raw)
    if not phrase:
        return None
    return {"method": METHOD, "phrase": raw, "normalized": phrase, "algorithm": ALGORITHM}
