"""Input: the Apify compass/crawler-google-places 0.14.761 contract plus a ``bro`` object.

Validation is driven by the saved input schema (``contracts/apify-0.14.761/
input_schema.json``): every declared property is checked for type, enum,
minimum/maximum, length and pattern exactly as Apify's platform would, and
the schema defaults are applied.  On top of that:

* add-ons outside this release (leads enrichment, email verification, social
  profile enrichment, competitor analysis) are rejected when enabled, before
  any browser is started;
* unknown keys are an error -- except the legacy JSON-only keys the actor's
  changelog says keep working, which are accepted and either applied or
  reported as "no effect";
* the documented priorities are applied and every override is reported:
  start URLs / place IDs over search terms, ``locationQuery`` over the
  geolocation fields.
* project settings live in ``bro`` (coverage, concurrency, run time, proxy,
  evidence, exhaustive reviews/images).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

CONTRACT_DIR = Path(__file__).resolve().parent / "contracts" / "apify-0.14.761"


class InputError(ValueError):
    """The input cannot be run; ``problems`` lists every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@lru_cache(maxsize=1)
def apify_schema() -> dict[str, Any]:
    path = CONTRACT_DIR / "input_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


#: add-ons this release does not implement: (key, "enabled" predicate, label)
EXCLUDED_ADDONS = [
    ("maximumLeadsEnrichmentRecords", lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
     "business leads enrichment"),
    ("verifyLeadsEnrichmentEmails", lambda v: v is True, "email verification"),
    ("scrapeSocialMediaProfiles", lambda v: isinstance(v, dict) and any(v.values()), "social media profile enrichment"),
    ("enableCompetitorAnalysis", lambda v: v is True, "AI competitor analysis"),
]

#: JSON-only keys documented in the actor changelog as still working
LEGACY_APPLIED = {
    "zoom": "integer zoom (1-21) for every map viewport",
    "deeperCityScrape": "default true; false disables grid subdivision",
    "maxAutomaticZoomOut": "accepted; bounds how far a search viewport may widen (kept for compatibility)",
    "oneReviewPerRow": "apify export writes one review per output row",
    "maxCrawledPlaces": "overall cap on places across all searches",
    "onlyDataFromSearchPage": "skip place detail requests; search data only",
    "exportPlaceUrls": "deprecated alias of onlyDataFromSearchPage",
}
LEGACY_NO_EFFECT = {
    "includeHistogram", "includeOpeningHours", "includePeopleAlsoSearch", "additionalInfo",
    "proxyConfiguration", "maxConcurrency", "debug",
    "scrapeReviewerName", "scrapeReviewerId", "scrapeReviewerUrl", "scrapeReviewId", "scrapeReviewUrl",
}

RELATIVE_DATE = re.compile(r"^(\d+)\s*(minute|hour|day|week|month|year)s?$")
STARS = {"": None, "two": 2.0, "twoAndHalf": 2.5, "three": 3.0, "threeAndHalf": 3.5, "four": 4.0, "fourAndHalf": 4.5}


# ------------------------------------------------------------------ schema
def _type_ok(value: Any, expected: Any) -> bool:
    types = expected if isinstance(expected, list) else [expected]
    for t in types:
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "object" and isinstance(value, dict):
            return True
        if t == "null" and value is None:
            return True
    return False


def _check(name: str, value: Any, spec: dict[str, Any], problems: list[str]) -> None:
    if value is None:
        return  # Apify treats null as "not provided"
    if "type" in spec and not _type_ok(value, spec["type"]):
        problems.append(f"{name}: expected {spec['type']}, got {type(value).__name__}")
        return
    if "enum" in spec and value not in spec["enum"]:
        sample = ", ".join(map(repr, spec["enum"][:8]))
        problems.append(f"{name}: {value!r} is not one of {sample}{' ...' if len(spec['enum']) > 8 else ''}")
    if "minimum" in spec and isinstance(value, (int, float)) and value < spec["minimum"]:
        problems.append(f"{name}: must be >= {spec['minimum']}")
    if "maximum" in spec and isinstance(value, (int, float)) and value > spec["maximum"]:
        problems.append(f"{name}: must be <= {spec['maximum']}")
    if "maxLength" in spec and isinstance(value, str) and len(value.encode("utf-8")) > spec["maxLength"]:
        problems.append(f"{name}: longer than {spec['maxLength']} bytes")
    if "pattern" in spec and isinstance(value, str) and not re.match(spec["pattern"], value):
        problems.append(f"{name}: {value!r} does not match {spec['pattern']}")
    if isinstance(value, list) and isinstance(spec.get("items"), dict):
        for i, item in enumerate(value):
            _check(f"{name}[{i}]", item, spec["items"], problems)
    if isinstance(value, dict) and isinstance(spec.get("properties"), dict):
        for req in spec.get("required") or []:
            if req not in value:
                problems.append(f"{name}: missing required '{req}'")
        for key, sub in spec["properties"].items():
            if key in value:
                _check(f"{name}.{key}", value[key], sub, problems)


# ------------------------------------------------------------------ bro
class BroProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    tier: Literal["lite", "basic", "premium"] = "lite"
    policy: Literal["html_only", "basic", "extended", "full"] = "basic"
    country: str | None = None
    city: str | None = None
    blockUnproxied: bool = False


#: bro settings of the retired Maps list route (removed 2026-10-07): accepted in stored inputs so
#: older jobs resume, ignored with a warning
RETIRED_BRO_KEYS = ("reviewsDriver", "reviewScrollsPerCommand", "reviewsPruneEvery", "reviewsStallProbe",
                    "reviewsBounceAfterEmpty", "reviewsWaitMode", "reviewsAwaitBudget", "reviewsWaitAB",
                    "reviewsWaitABFrom", "reviewsWaitABMinutes")


DISCOVERY_ALGORITHMS = ("legacy-v1", "adaptive-v2", "adaptive-v3", "adaptive-v4")
#: strategies with the adaptive grid (unique-identity saturation, subdividing outside tails)
ADAPTIVE = ("adaptive-v2", "adaptive-v3", "adaptive-v4")
#: strategies that parse a record once and take the measured preset (v4 = v3 + the places of batch
#: pages fetched after a stop, with the same traversal)
OPTIMIZED = ("adaptive-v3", "adaptive-v4")
#: discoveryAlgorithm of ``deep``/``extended`` coverage when the input names none: adaptive-v4,
#: which passed the discovery gate on fresh controls (cost and time per found place <= 5x
#: legacy, >= 90% of complete Apify references, >= 98% of legacy IDs).
#: ``standard`` coverage stays legacy-v1; jobs pin the resolved value, so older jobs never change
DEEP_DEFAULT_ALGORITHM = "adaptive-v4"
#: the settings adaptive-v3 was measured with (coverage-quality-next O1, 2026-10-11); given to the
#: knobs an input leaves unset (adaptive-v3 and adaptive-v4)
ADAPTIVE_V3_PRESET = {"minCellKm": 0.1, "saturationThreshold": 10, "discoveryFetchMode": "batch"}
#: collection contracts (what the review route reads, core-parity plan 2026-10-14): ``rc27`` = Google
#: reviews from Search only, reviewsFilterString matched locally, rating sorts from Search's list;
#: ``core-2026.10`` = partner reviews and the mixed order from Google's Travel list (reviewsOrigin
#: all), Google's own review search for reviewsFilterString, rating sorts from Travel's longer list.
#: New jobs pin the current contract; a stored job without one is rc27, so a resume never changes
#: the source, the sort or the meaning of the filter
COLLECTION_CONTRACTS = ("rc27", "core-2026.10")
CURRENT_CONTRACT = "core-2026.10"


class BroSettings(BaseModel):
    """Project-specific settings (``bro`` object of the input)."""
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired(cls, data: Any) -> Any:
        if isinstance(data, dict):
            retired = [k for k in RETIRED_BRO_KEYS if k in data]
            if retired:
                data = {k: v for k, v in data.items() if k not in RETIRED_BRO_KEYS}
                data["_retired"] = retired
        return data

    #: retired keys found in the input (reported as a warning)
    retired: list[str] = Field(default_factory=list, alias="_retired")
    profile: Literal["default", "quality"] = "default"
    coverage: Literal["standard", "deep", "extended"] | None = None
    concurrency: int = Field(1, ge=1, le=20)
    maxRunSeconds: float = Field(6 * 3600, gt=0, le=7 * 24 * 3600)
    sessionTimeout: float = Field(21600, ge=300, le=21600)
    maxSessionReplacements: int = Field(5, ge=0, le=100)
    maxGatedSessions: int = Field(6, ge=0, le=50)
    maxTaskAttempts: int = Field(3, ge=1, le=10)
    proxy: BroProxy = Field(default_factory=BroProxy)
    #: sessions serving review tasks.  None = ``proxy`` when it is enabled, otherwise a lite
    #: proxy in the ``gl`` country (anonymous no-proxy VMs were often refused review sorting
    #: on 2026-10-01).  ``{"enabled": false}`` forces review sessions without proxy.
    reviewsProxy: BroProxy | None = None
    evidence: Literal["none", "errors", "all"] = "errors"
    reviewsExhaustive: bool = False
    imagesExhaustive: bool = False
    #: with maxQuestions: also read every answer of a question that has more than the 5 Google's
    #: question list gives (one more request per such question)
    questionsAllAnswers: bool = False
    maxWebsitePages: int = Field(10, ge=1, le=50)
    #: "open": every website page opened in the browser (waits for the load); "fetch": the homepage
    #: opened, the other pages of its origin read by same-origin GETs, a short or failed one opened
    websiteReadMode: Literal["open", "fetch"] = "open"
    #: queue the website (contacts) tasks right after the review lists instead of after every other section
    websiteEarly: bool = False
    gl: str | None = Field(None, pattern=r"^[a-zA-Z]{2}$")
    nominatimUrl: str = "https://nominatim.openstreetmap.org"
    nominatimContact: str | None = None
    osmId: str | None = Field(None, pattern=r"^[NWRnwr][0-9]+$")
    minCellKm: float = Field(0.4, gt=0.05, le=50)
    #: None = DEEP_DEFAULT_ALGORITHM for deep/extended coverage, else legacy-v1 (resolved on validation)
    discoveryAlgorithm: Literal["legacy-v1", "adaptive-v2", "adaptive-v3", "adaptive-v4"] | None = None
    discoveryFetchMode: Literal["batch", "sequential", "hybrid"] = "batch"
    saturationThreshold: int = Field(40, ge=10, le=200)
    placeBatchSize: int = Field(6, ge=1, le=20)
    #: with reviewsStartDate on the newest list: stop after this many consecutive pages listed
    #: entirely before the date (by the last edit time, so no later publication is cut); 0 reads
    #: the list to its end
    reviewsDateCutoffPages: int = Field(2, ge=0, le=50)
    #: "exact": the Search list, then exact dates (publish, edit, owner reply) read from Maps
    #: single-review pages for every review without a trustworthy cached date (~6 s per review
    #: per session).  "fast": the Search list only; exact dates only where the cache has them,
    #: otherwise publishedAtDate stays null and Search's one time is given apart as
    #: publishedOrEditedAtDate (the last edit, else the publish time).
    reviewsMode: Literal["exact", "fast"] = "exact"
    #: SQLite file with exact review dates shared by jobs (None = this job only): filled by the
    #: Maps date reads, so a place collected again re-reads only new or changed reviews
    reviewsDateCache: str | None = None
    #: how old an observed edit / owner-reply date may be before it is read again (0 = never)
    reviewsDateTtlDays: float = Field(30.0, ge=0, le=3650)
    #: reviews per date task (one task = one session reading their single-review pages)
    reviewsEnrichBatch: int = Field(50, ge=1, le=500)
    #: reload the single-review page after this many in-app navigations
    reviewsPageRecycle: int = Field(200, ge=10, le=5000)
    allPlacesZoom: float = Field(17.0, ge=14, le=21)
    #: None = CURRENT_CONTRACT (resolved on validation, pinned in the job); see COLLECTION_CONTRACTS
    collectionContract: Literal["rc27", "core-2026.10"] | None = None
    #: reviewsFilterString: "google" = Google's own review search (the reviews Apify returns);
    #: "local_phrase" = the whole list matched here (pipeline/review_filter.py).  None = "google" under
    #: core-2026.10, "local_phrase" under rc27.  A refused Google search never falls back to local
    reviewsFilterMode: Literal["google", "local_phrase"] | None = None
    #: highestRanking/lowestRanking: "google" = Google's rating-sorted selection (it ends before the
    #: count: section limited); "complete" = the complete most-relevant list sorted by rating here
    #: (ties keep Google's relevance order), only when that list is confirmed complete
    ratingSortMode: Literal["google", "complete"] = "google"

    #: knobs filled from ADAPTIVE_V3_PRESET (reported as a warning)
    _preset: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _resolve_contract(self) -> "BroSettings":
        if self.collectionContract is None:
            self.collectionContract = CURRENT_CONTRACT
        if self.reviewsFilterMode is None:
            self.reviewsFilterMode = "local_phrase" if self.collectionContract == "rc27" else "google"
        return self

    @property
    def core_contract(self) -> bool:
        return self.collectionContract != "rc27"

    @model_validator(mode="after")
    def _resolve_discovery(self) -> "BroSettings":
        if self.discoveryAlgorithm is None:
            self.discoveryAlgorithm = (DEEP_DEFAULT_ALGORITHM if self.effective_coverage in ("deep", "extended")
                                       else "legacy-v1")
        if self.discoveryAlgorithm in OPTIMIZED:
            unset = {k: v for k, v in ADAPTIVE_V3_PRESET.items() if k not in self.model_fields_set}
            for key, value in unset.items():
                setattr(self, key, value)
            self._preset = unset
        return self

    @property
    def effective_coverage(self) -> str:
        if self.coverage:
            return self.coverage
        return "deep" if self.profile == "quality" else "standard"


# ------------------------------------------------------------------ result
@dataclass
class StartUrl:
    url: str
    kind: str                 # place | search | cid | short | list | reviews | unknown
    fid: str | None = None
    place_id: str | None = None
    query: str | None = None
    lat: float | None = None
    lng: float | None = None
    zoom: float | None = None


@dataclass
class RunInput:
    raw: dict[str, Any]
    apify: dict[str, Any]                     # schema-validated with defaults
    bro: BroSettings
    warnings: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    direct_place_ids: list[str] = field(default_factory=list)
    start_urls: list[StartUrl] = field(default_factory=list)
    location_mode: str = "none"               # none | query | structured | custom
    category_filter: list[str] = field(default_factory=list)
    all_places_mode: bool = False
    legacy: dict[str, Any] = field(default_factory=dict)
    #: moment a relative reviewsStartDate ("3 hours") is counted from: fixed once per input
    reviews_anchor: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get(self, key: str, default: Any = None) -> Any:
        value = self.apify.get(key)
        return default if value is None else value

    # convenience views used by the pipeline
    @property
    def language(self) -> str:
        return self.apify["language"]

    @property
    def max_places_per_search(self) -> int | None:
        return self.apify.get("maxCrawledPlacesPerSearch")

    @property
    def details_enabled(self) -> bool:
        if self.legacy.get("onlyDataFromSearchPage"):
            return False
        a = self.apify
        return bool(a["scrapePlaceDetailPage"] or a["maxReviews"] or a["maxImages"] or a["scrapeTableReservationProvider"]
                    or a["scrapeOrderOnline"] or a["includeWebResults"] or a["scrapeDirectories"] or a["maxQuestions"]
                    or self.bro.profile == "quality" or self.start_urls or self.direct_place_ids)

    @property
    def reviews_limit(self) -> int | None:
        """``None`` = to the end of the source (bro.reviewsExhaustive)."""
        if self.bro.reviewsExhaustive:
            return None
        return int(self.apify["maxReviews"] or 0)

    @property
    def images_limit(self) -> int | None:
        if self.bro.imagesExhaustive:
            return None
        return int(self.apify["maxImages"] or 0)

    @property
    def min_stars(self) -> float | None:
        return STARS.get(self.apify.get("placeMinimumStars") or "")

    def reviews_start(self, _now: datetime | None = None) -> datetime | None:
        """The review cutoff.  A relative value counts from :attr:`reviews_anchor` (the moment the
        input was read), not from the moment a place is processed: one job has one cutoff.  A new
        job stores the resolved absolute date (:func:`resolve_relative_start`), so resumes keep it.
        ``_now`` is ignored (kept for older callers)."""
        raw = self.apify.get("reviewsStartDate")
        if not raw:
            return None
        now = self.reviews_anchor
        m = RELATIVE_DATE.match(raw.strip())
        if m:
            n, unit = int(m.group(1)), m.group(2)
            days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30.4375, "year": 365.25}[unit]
            return now - timedelta(days=n * days)
        text = raw.strip().rstrip("Z")
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def resolve_relative_start(raw: dict[str, Any], run: "RunInput") -> dict[str, Any]:
    """The input with a relative reviewsStartDate replaced by its absolute UTC date."""
    value = raw.get("reviewsStartDate")
    if not value or not RELATIVE_DATE.match(str(value).strip()):
        return raw
    start = run.reviews_start()
    return {**raw, "reviewsStartDate": start.strftime("%Y-%m-%dT%H:%M:%SZ")}


def pin_discovery(raw: dict[str, Any], run: "RunInput") -> dict[str, Any]:
    """The input with the resolved discovery strategy (and the preset knobs it took) written out, so a
    job resumed by a later version with another default or preset keeps its traversal."""
    bro = dict(raw.get("bro") or {})
    pinned = {"discoveryAlgorithm": run.bro.discoveryAlgorithm,
              **{k: getattr(run.bro, k) for k in run.bro._preset}}
    if all(bro.get(k) == v for k, v in pinned.items()):
        return raw
    return {**raw, "bro": {**bro, **pinned}}


def pin_contract(raw: dict[str, Any], run: "RunInput") -> dict[str, Any]:
    """The input with the resolved collection contract and filter mode written out (resumes keep them)."""
    bro = dict(raw.get("bro") or {})
    pinned = {"collectionContract": run.bro.collectionContract, "reviewsFilterMode": run.bro.reviewsFilterMode}
    if all(bro.get(k) == v for k, v in pinned.items()):
        return raw
    return {**raw, "bro": {**bro, **pinned}}


def parse_input(raw: dict[str, Any]) -> RunInput:
    """Validate and normalise an input; raise :class:`InputError` with all problems."""
    if not isinstance(raw, dict):
        raise InputError(["input must be a JSON object"])
    schema = apify_schema()
    props: dict[str, Any] = schema["properties"]
    problems: list[str] = []
    warnings: list[str] = []
    legacy: dict[str, Any] = {}

    for key in raw:
        if key in props or key == "bro":
            continue
        if key in LEGACY_APPLIED:
            legacy[key] = raw[key]
            continue
        if key in LEGACY_NO_EFFECT:
            warnings.append(f"'{key}' is a legacy Apify option with no effect here")
            continue
        problems.append(f"unknown input parameter '{key}'")

    apify: dict[str, Any] = {}
    for key, spec in props.items():
        value = raw.get(key)
        _check(key, value, spec, problems)
        if value is None and "default" in spec:
            value = json.loads(json.dumps(spec["default"]))
        apify[key] = value

    from .pipeline.review_filter import normalize as normalize_phrase, phrase_problem
    problem = phrase_problem(raw.get("reviewsFilterString"))
    if problem:
        problems.append(problem)
    filter_given = isinstance(raw.get("reviewsFilterString"), str) and bool(raw["reviewsFilterString"])

    for key, enabled, label in EXCLUDED_ADDONS:
        if enabled(raw.get(key)):
            problems.append(f"'{key}' enables {label}, which this release does not provide; remove it")
    if raw.get("leadsEnrichmentDepartments") and not (raw.get("maximumLeadsEnrichmentRecords") or 0):
        warnings.append("'leadsEnrichmentDepartments' has no effect without leads enrichment")

    for key in ("zoom", "maxAutomaticZoomOut", "maxCrawledPlaces"):
        if key in legacy and not (isinstance(legacy[key], int) and not isinstance(legacy[key], bool)):
            problems.append(f"{key}: expected integer")
    if "zoom" in legacy and isinstance(legacy["zoom"], int) and not 1 <= legacy["zoom"] <= 21:
        problems.append("zoom: must be between 1 and 21")
    for key in ("deeperCityScrape", "oneReviewPerRow", "onlyDataFromSearchPage", "exportPlaceUrls"):
        if key in legacy and not isinstance(legacy[key], bool):
            problems.append(f"{key}: expected boolean")
    if legacy.get("exportPlaceUrls"):
        legacy["onlyDataFromSearchPage"] = True

    try:
        bro = BroSettings.model_validate(raw.get("bro") or {})
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            problems.append(f"bro.{loc}: {err['msg']}")
        bro = BroSettings()

    if problems:
        raise InputError(problems)

    if filter_given:
        if not normalize_phrase(raw["reviewsFilterString"]):
            warnings.append("reviewsFilterString holds only whitespace: no filter is applied")
        elif bro.reviewsFilterMode == "local_phrase":
            warnings.append("reviewsFilterString is matched locally against the review text and its translation "
                            "(whole phrase, case-insensitive, diacritics kept); Google's own review search may "
                            "select other reviews")
    if bro.ratingSortMode == "complete" and apify.get("reviewsSort") not in ("highestRanking", "lowestRanking"):
        warnings.append("bro.ratingSortMode applies to highestRanking/lowestRanking only: ignored")
    if bro.retired:
        warnings.append("bro." + ", bro.".join(bro.retired) + " belong to the retired Maps review-list route and are "
                        "ignored: reviews are listed through Google Search with exact dates from Maps review pages")
    if bro._preset:
        warnings.append(f"bro.discoveryAlgorithm '{bro.discoveryAlgorithm}' uses its measured settings for "
                        + ", ".join(f"{k}={v}" for k, v in bro._preset.items()) + " (set them explicitly to change)")
    if "discoveryFetchMode" in bro.model_fields_set and bro.discoveryAlgorithm not in OPTIMIZED:
        warnings.append("bro.discoveryFetchMode applies to adaptive-v3/v4 only: ignored")
    if bro.websiteReadMode == "fetch":
        warnings.append("bro.websiteReadMode 'fetch' is experimental: about 2.4x faster on websites, but it misses social "
                        "links a site's scripts build and those of pages the site serves as 404, and it may glue text "
                        "into false addresses; 'open' keeps every contact")
    run = RunInput(raw=raw, apify=apify, bro=bro, warnings=warnings, legacy=legacy)
    _resolve_sources(run)
    if not (run.search_terms or run.start_urls or run.direct_place_ids or run.all_places_mode):
        raise InputError(["nothing to scrape: give searchStringsArray, startUrls, placeIds, "
                          "categoryFilterWords or allPlacesNoSearchAction"])
    if run.all_places_mode and run.location_mode == "none":
        raise InputError(["allPlacesNoSearchAction needs a location (locationQuery, geolocation fields "
                          "or customGeolocation)"])
    if run.apify.get("reviewsStartDate") and bro.reviewsMode == "fast":
        warnings.append("bro.reviewsMode 'fast' with reviewsStartDate: reviews last active before the date are "
                        "dropped, but a review published earlier and edited after the date can only be told "
                        "apart with exact dates (reviewsMode 'exact'); such reviews are kept")
    if run.apify.get("reviewsStartDate") and run.apify["reviewsSort"] != "newest":
        warnings.append("reviewsStartDate is applied as a filter only: early stop needs reviewsSort 'newest'")
    if bro.profile == "quality" and not apify["scrapePlaceDetailPage"]:
        apify["scrapePlaceDetailPage"] = True
        warnings.append("bro.profile 'quality' turns scrapePlaceDetailPage on")
    return run


def _resolve_sources(run: RunInput) -> None:
    from .maps.urls import classify_start_url

    a = run.apify
    terms = [t.strip() for t in (a.get("searchStringsArray") or []) if isinstance(t, str) and t.strip()]
    place_terms = [t for t in terms if t.lower().startswith("place_id:")]
    terms = [t for t in terms if not t.lower().startswith("place_id:")]
    run.direct_place_ids = [t.split(":", 1)[1].strip() for t in place_terms if t.split(":", 1)[1].strip()]
    run.direct_place_ids += [p.strip() for p in (a.get("placeIds") or []) if p and p.strip()]
    for entry in a.get("startUrls") or []:
        url = entry.get("url") if isinstance(entry, dict) else entry if isinstance(entry, str) else None
        if url:
            run.start_urls.append(classify_start_url(url.strip()))
    unknown = [s.url for s in run.start_urls if s.kind == "unknown"]
    if unknown:
        raise InputError([f"startUrls: not a Google Maps URL: {u}" for u in unknown])

    run.category_filter = [c.strip().lower() for c in (a.get("categoryFilterWords") or []) if c and c.strip()]
    run.all_places_mode = bool(a.get("allPlacesNoSearchAction"))
    if a.get("allPlacesNoSearchAction") == "all_places_no_search_mouse":
        run.warnings.append("'all_places_no_search_mouse' is deprecated in Apify; the map-visible mode is used")

    if run.start_urls or run.direct_place_ids:
        if terms:
            run.warnings.append("startUrls/placeIds take priority: searchStringsArray is ignored (Apify rule)")
            terms = []
    if run.all_places_mode and terms:
        run.warnings.append("allPlacesNoSearchAction ignores search terms (Apify rule)")
        terms = []
    if not terms and run.category_filter and not (run.start_urls or run.direct_place_ids or run.all_places_mode):
        terms = list(run.category_filter)
        run.warnings.append("categoryFilterWords without search terms are used as search terms too (Apify rule)")
    run.search_terms = terms

    geo_fields = [k for k in ("countryCode", "state", "county", "city", "postalCode") if a.get(k)]
    if a.get("locationQuery"):
        run.location_mode = "query"
        ignored = geo_fields + (["customGeolocation"] if a.get("customGeolocation") else [])
        if ignored:
            run.warnings.append(f"locationQuery takes priority; ignored: {', '.join(ignored)} (Apify rule)")
    elif a.get("customGeolocation"):
        run.location_mode = "custom"
        if geo_fields:
            run.warnings.append(f"customGeolocation is used; ignored: {', '.join(geo_fields)}")
    elif geo_fields:
        run.location_mode = "structured"
        if a.get("postalCode") and a.get("city"):
            run.warnings.append("postalCode should be combined with countryCode only, not city (Apify note)")


def load_input(path: str | Path) -> RunInput:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InputError([f"input is not valid JSON: {exc}"]) from exc
    return parse_input(raw)
