# Input & Output Reference

The scraper accepts input configurations matching the Apify `compass/crawler-google-places` (build 0.14.761) schema plus an optional `bro` configuration object for runtime controls.

---

## Input Reference

### 1. Discovery & Targets

Specify what places to find or scrape. Precedence rules: `startUrls` and `placeIds` take priority over search queries.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `searchStringsArray` | string[] | `[]` | Search terms (e.g., `["coffee shop", "bakery"]`). Can also accept direct IDs like `place_id:ChIJ...`. |
| `locationQuery` | string | `""` | Free-text location (e.g., `"SoHo, Manhattan, NY"`, `"Berlin, Germany"`). Resolved via Nominatim. |
| `countryCode` | string | `""` | Two-letter ISO country code (e.g., `"US"`, `"DE"`). |
| `state` | string | `""` | State or province name. |
| `city` | string | `""` | City name. |
| `postalCode` | string | `""` | Postal code. Recommended with `countryCode`. |
| `customGeolocation` | object | `null` | GeoJSON geometry (`Point` with `radiusKm`, `Polygon`, or `MultiPolygon`). |
| `startUrls` | object[] | `[]` | Google Maps URLs (search URLs, place detail URLs, CID links, short links, or list URLs). |
| `placeIds` | string[] | `[]` | Direct place IDs (`ChIJ...`). Decoded offline or resolved via Maps. |
| `allPlacesNoSearchAction` | string | `""` | Set to `"all_places_no_search_mouse"` to extract all map pins in the area without a search query. |
| `maxCrawledPlacesPerSearch` | integer | `null` | Maximum accepted places per search query/area. |
| `maxCrawledPlaces` | integer | `null` | Global cap on accepted places across all search queries in the job. |

### 2. Search Filters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `categoryFilterWords` | string[] | `[]` | Filter places whose categories contain any of these keywords. |
| `placeMinimumStars` | string | `""` | Minimum rating filter: `"two"`, `"twoAndHalf"`, `"three"`, `"threeAndHalf"`, `"four"`, `"fourAndHalf"`. |
| `skipClosedPlaces` | boolean | `false` | Skip permanently and temporarily closed establishments. |
| `websiteFilter` | string | `"all"` | `"all"`, `"withWebsite"`, or `"withoutWebsite"`. |
| `searchStringMatch` | string | `"all"` | `"all"` or `"name"` (requires place name to match the search query). |

### 3. Place Details & Enrichments

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `scrapePlaceDetailPage` | boolean | `true` | Fetch the full place detail card (opening hours, attributes, phone, website, photos count). |
| `language` | string | `"en"` | Language code for Google Maps and Search responses (e.g. `"en"`, `"de"`, `"fr"`, `"es"`, `"ja"`). |
| `maxReviews` | integer | `0` | Number of reviews to collect per place (0 disables reviews). |
| `reviewsSort` | string | `"mostRelevant"` | Review sort order: `"mostRelevant"`, `"newest"`, `"highestRanking"`, `"lowestRanking"`. |
| `reviewsStartDate` | string | `""` | Cutoff date for reviews (ISO date `"2026-01-01"` or relative `"30 days"`). |
| `reviewsFilterString` | string | `""` | Review keyword search. Under `core-2026.10`, queries Google's review search backend. |
| `reviewsOrigin` | string | `"all"` | `"all"` includes partner reviews (Tripadvisor, Priceline, etc.); `"google"` keeps only Google reviews. |
| `scrapeReviewsPersonalData` | boolean | `true` | When `false`, omits reviewer names, URLs, IDs, and avatars. |
| `maxImages` | integer | `0` | Maximum photos to extract per place (0 disables photos). |
| `scrapeImageAuthors` | boolean | `false` | Resolves image author names and profile URLs via `photometa`. |
| `scrapeContacts` | boolean | `false` | Extracts emails, phone numbers, social links, and Impressum from the business website. |
| `maxQuestions` | integer | `0` | Maximum Q&A items to collect from Google's local Q&A app (0 disables Q&A). |
| `scrapeTableReservationProvider`| boolean | `false` | Reads table booking providers from Google Reserve (`reserveTableUrl`). |
| `scrapeOrderOnline` | boolean | `false` | Reads delivery and pickup providers from Google Food (`googleFoodUrl`). |
| `includeWebResults` | boolean | `false` | Reads organic Google web search results for the business name. |
| `scrapeDirectories` | boolean | `false` | Scrapes places located inside the business (e.g., mall stores, airport terminals). |


---

## The `bro` Settings Object

Project-level controls and runtime parameters configured under the optional top-level `"bro": { ... }` block:

```json
{
  "bro": {
    "concurrency": 3,
    "coverage": "deep",
    "reviewsMode": "exact",
    "maxRunSeconds": 7200,
    "proxy": {
      "enabled": true,
      "tier": "basic"
    }
  }
}
```

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `concurrency` | integer | `1` | Number of simultaneous remote browser sessions (1–20). |
| `coverage` | string | `null` | Map search density: `"standard"`, `"deep"` (subdivides down to `minCellKm` plus an offset control pass), or `"extended"` (searches related categories). |
| `discoveryAlgorithm` | string | `null` | Spatial search engine: `"legacy-v1"` or `"adaptive-v4"` (default for `deep`/`extended`). |
| `minCellKm` | number | `0.4` | Smallest cell size in kilometers during adaptive grid subdivision (down to `0.05`). |
| `saturationThreshold`| integer | `40` | Number of in-cell places before a cell splits into 4 sub-cells. |
| `reviewsMode` | string | `"exact"` | `"exact"` reads single-review Maps pages to obtain true publication, edit, and owner-reply dates. `"fast"` uses Search timestamps and skips Maps reads. |
| `reviewsDateCache` | string | `null` | Path to an SQLite file to cache exact review dates across multiple jobs. |
| `reviewsDateTtlDays` | number | `30.0` | Days before mutable dates (edits and owner replies) are re-verified. |
| `reviewsEnrichBatch` | integer | `50` | Number of reviews per date-enrichment worker task. |
| `reviewsExhaustive` | boolean | `false` | When `true`, scrapes all reviews to the end of the source, ignoring `maxReviews`. |
| `imagesExhaustive` | boolean | `false` | When `true`, scrapes all photos to the end of the source. |
| `maxWebsitePages` | integer | `10` | Max same-domain pages crawled per website for contact extraction (1–50). |
| `websiteReadMode` | string | `"open"` | `"open"` opens pages in the browser. `"fetch"` uses same-origin background requests for faster extraction. |
| `websiteEarly` | boolean | `false` | Schedules website contact extraction tasks earlier in the queue. |
| `questionsAllAnswers`| boolean | `false` | Fetches all answers for questions that have more than 5 answers. |
| `maxRunSeconds` | number | `21600` | Maximum job duration in seconds (up to 7 days). Jobs can be resumed if stopped. |
| `sessionTimeout` | number | `21600` | Maximum lifetime of an individual remote browser session (up to 6 hours). |
| `maxSessionReplacements` | integer | `5` | Allowable browser session replacements on failures before terminating. |
| `maxTaskAttempts` | integer | `3` | Maximum retry attempts per individual worker task. |
| `proxy` | object | `{}` | Proxy configuration: `{enabled, tier: "lite"|"basic"|"premium", policy, country, city}`. |
| `reviewsProxy` | object | `null` | Dedicated proxy settings for review tasks (defaults to a lite proxy in the target country). |
| `evidence` | string | `"errors"` | Level of raw HTTP/DOM responses saved to disk: `"none"`, `"errors"`, or `"all"`. |

---

## Output Reference

A job outputs all artifacts into the directory specified by `--job-dir`:

```
runs/my-job/
├── state.sqlite        # SQLite WAL database (single source of truth for all data and state)
├── run.log             # Full execution log
├── report.json         # Complete job summary, coverage metrics, and billing
├── dataset.json        # Apify-compatible dataset (if export=apify or all)
├── native/             # Multi-table JSONL/CSV datasets (if export=native or all)
│   ├── places.jsonl
│   ├── reviews.jsonl
│   ├── images.jsonl
│   ├── contacts.jsonl
│   └── discoveries.jsonl
└── evidence/           # Saved raw responses and error payloads (if bro.evidence != "none")
```

### 1. Apify Format (`dataset.json` / `dataset.csv`)

Matches `compass/crawler-google-places` field names and nesting:
- **Place Details:** `title`, `subTitle`, `description`, `price`, `categoryName`, `categories`, `address`, `neighborhood`, `street`, `city`, `postalCode`, `state`, `countryCode`, `website`, `phone`, `location` (`{lat, lng}`), `plusCode`, `totalScore`, `reviewsCount`, `openingHours`, `placesTags`, `additionalInfo`, `url`, `placeId`, `fid`, `cid`, `kgmid`.
- **Reviews:** Embedded array `reviews[]` (up to 5,000 per record; places with more are split across multiple items, or written one review per row with `oneReviewPerRow`):
  - `reviewId`, `text`, `textTranslated`, `stars`, `publishedAtDate`, `lastEditedAtDate`, `name`, `reviewerId`, `reviewerUrl`, `reviewerPhotoUrl`, `reviewerNumberOfReviews`, `isLocalGuide`, `responseFromOwnerText`, `responseFromOwnerDate`, `reviewOrigin`, `reviewDetailedRating`, `reviewContext`.
- **Images:** `images[]` (`imageUrl`, `authorName`, `authorUrl`, `uploadedAt`).
- **Contacts:** `emails[]`, `phones[]`, `phonesUncertain[]`, `instagrams[]`, `facebooks[]`, `linkedIns[]`, `twitters[]`, `youtubes[]`, `tiktoks[]`, `domain`, `impressum`.
- **Enrichments:** `questionsAndAnswers[]`, `orderOnline` (`{pickUps, deliveries}`), `tableReservationProviders[]`, `webResults[]`.

### 2. Native Format (`native/`)

Normalized relational tables with complete provenance and section statuses:
- **`places.jsonl` / `places.csv`:** Full place metadata, search rankings, acceptance status, and per-section lifecycle statuses (`not_requested`, `pending`, `done`, `limited`, `failed`, `absent`, `empty`).
- **`reviews.jsonl` / `reviews.csv`:** Distinct review records with exact timestamps, edit state, owner reply state, and source provenance.
- **`images.jsonl` / `images.csv`:** Photos with original resolution URLs and `dateEvidence` observations.
- **`contacts.jsonl` / `contacts.csv`:** Extracted contact records tagged with domain, source URL, and branch vs. domain ownership attribution.
- **`discoveries.jsonl`:** Audit trail of which search cell, zoom level, and pass discovered each place.

### 3. Job Report (`report.json`)

Contains machine-readable metadata about the completed or in-progress run:
- **`job`**: Execution state, runtime, parser versions, and warning log.
- **`places`**: Accepted, known, and unverified location counts.
- **`tasks`**: Status breakdown for all task queues (`search`, `place`, `reviews`, `review_dates`, `images`, `website`, `questions`, etc.).
- **`coverage`**: Cells processed, saturated, split, and unique place gains per pass.
- **`sections`**: Completion status counts for each data section.
- **`reviewGaps`**: Discrepancies between Google's displayed review counter and collected reviews.
- **`billing`**: Exact dollar amounts billed by the bro service, broken down by session.

