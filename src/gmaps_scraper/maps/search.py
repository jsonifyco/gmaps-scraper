"""Search endpoint (``/search?tbm=map``) responses.

``[64]`` holds the result items.  An item whose ``[1]`` is a place array is
a place; an item without one is a text ad (``[5]`` carries its copy) and is
counted, never stored as a place.  An empty or missing ``[64]`` on a later
page is how Google ends a viewport (~165 places per viewport, 2026-10-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .xssi import dig, loads_guarded

PARSER_VERSION = "search-2026.10.08"  # query-neutral category order kept


@dataclass
class SearchItem:
    position: int          # 0-based position among places of this page
    place: list            # the place array
    is_ad: bool = False


@dataclass
class SearchPage:
    items: list[SearchItem]
    text_ads: int
    viewport: list | None
    raw_items: int
    query_echo: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items


def parse_search(text: str) -> SearchPage:
    data = loads_guarded(text)
    if not isinstance(data, list):
        raise ValueError("search response is not a list")
    raw = dig(data, 64, default=[]) or []
    items: list[SearchItem] = []
    ads = 0
    for entry in raw:
        place = dig(entry, 1)
        if isinstance(place, list) and dig(place, 10):
            items.append(SearchItem(position=len(items), place=place, is_ad=False))
        else:
            ads += 1
    notes: list[str] = []
    if not raw:
        single = _single(data)
        if single is not None:
            items.append(SearchItem(position=0, place=single))
            notes.append("single_place_answer")
    return SearchPage(items=items, text_ads=ads, viewport=dig(data, 1), raw_items=len(raw),
                      query_echo=dig(data, 0, 0), notes=notes)


def _single(data: list) -> list | None:
    for entry in dig(data, 0, 1, default=[]) or []:
        place = dig(entry, 14)
        if isinstance(place, list) and dig(place, 10):
            return place
    return None


def single_place_answer(text: str) -> list | None:
    """A search that Google resolves to one place answers with the place in ``[0][1][0][14]``
    (verified with "Arepittas Pittsburgh", 2026-10-01) and an empty ``[64]``."""
    return _single(loads_guarded(text))
