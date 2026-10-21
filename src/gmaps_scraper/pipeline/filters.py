"""Apify's place filters, applied to parsed place fields.

* ``searchMatching``: ``only_includes`` -- the title contains the search term;
  ``only_exact`` -- the title equals it (case-insensitive, whitespace folded).
* ``placeMinimumStars``: rating >= threshold; places without a rating fail.
* ``website``: ``withWebsite`` / ``withoutWebsite``.
* ``skipClosedPlaces``: permanently or temporarily closed places fail.
* ``categoryFilterWords``: a place passes if one of its categories equals a
  filter word, or contains all words of a filter (Apify: ``restaurant`` matches
  ``Chinese restaurant``).  Google's language-independent category ids
  (``[76]``, e.g. ``toy_store``) are matched as well, so English filter words
  work for places scraped in other languages.

Each check returns the reason a place was rejected, or ``None``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..input_model import RunInput


def _fold(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^\w]+", _fold(text)) if w]


def category_match(place: dict[str, Any], filters: list[str]) -> bool:
    names = [_fold(c) for c in place.get("categories") or [] if c]
    names += [_fold(c.replace("_", " ")) for c in place.get("categoryIds") or [] if c]
    for f in filters:
        f_fold = _fold(f)
        f_words = _words(f)
        for name in names:
            if name == f_fold:
                return True
            if f_words and all(w in _words(name) for w in f_words):
                return True
    return False


def name_match(title: str | None, term: str | None, mode: str) -> bool:
    if mode == "all" or not term:
        return True
    t, q = _fold(title), _fold(term)
    if mode == "only_exact":
        return t == q
    return q in t


def reject_reason(place: dict[str, Any], run: RunInput, *, search_term: str | None,
                  from_search: bool) -> str | None:
    a = run.apify
    if from_search and not name_match(place.get("title"), search_term, a.get("searchMatching") or "all"):
        return f"searchMatching:{a.get('searchMatching')}"
    stars = run.min_stars
    if stars is not None:
        score = place.get("totalScore")
        if score is None or float(score) < stars:
            return f"placeMinimumStars:{a.get('placeMinimumStars')}"
    site = a.get("website") or "allPlaces"
    if site == "withWebsite" and not place.get("website"):
        return "website:withWebsite"
    if site == "withoutWebsite" and place.get("website"):
        return "website:withoutWebsite"
    if a.get("skipClosedPlaces") and (place.get("permanentlyClosed") or place.get("temporarilyClosed")):
        return "skipClosedPlaces"
    if run.category_filter and not category_match(place, run.category_filter):
        return "categoryFilterWords"
    return None


CHAIN_DOMAINS = ("mcdonalds", "starbucks", "dominos", "pizzahut", "burgerking", "kfc", "subway", "wendys",
                 "dunkindonuts", "tacobell")


def is_big_chain(domain: str | None) -> bool:
    """Apify excludes contacts of these chains from contact enrichment."""
    host = _fold(domain).replace("www.", "")
    return any(host.split(".")[0] == chain or host.startswith(chain + ".") for chain in CHAIN_DOMAINS)
