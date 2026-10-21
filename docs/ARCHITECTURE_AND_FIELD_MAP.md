# Architecture & Field Map

This document outlines the internal architecture of `gmaps-scraper`, the task queue execution model, and where each Apify-compatible output field originates.

---

## 1. Package Architecture

The code is organized under `src/gmaps_scraper/`:

```
src/gmaps_scraper/
├── cli.py                  # CLI argument parsing and commands (validate, run, resume, etc.)
├── input_model.py          # Pydantic schema validation, defaults, and priority rules
├── store.py                # SQLite WAL storage engine, task queue, and atomic transactions
├── report.py               # Aggregates coverage, section completion, review gaps, and billing
├── benchmark.py            # Automated measurement profiles and Apify dataset comparisons
│
├── bro/                    # Remote browser transport
│   └── transport.py        # Sessions, batch commands, recovery, and payload downloads
│
├── geo/                    # Spatial geometry & geocoding
│   ├── geometry.py         # Shapely Area models, bounding boxes, and GeoJSON converters
│   ├── grid.py             # Viewport calculation, cell sizing, subdivision, and offset passes
│   └── nominatim.py        # OpenStreetMap territory lookup with rate limiting and local cache
│
├── maps/                   # Google Maps & Search protocol handlers
│   ├── client.py           # HTTP/CDP communication layer inside bro sessions
│   ├── pb.py               # Viewport protobuf and URL generator
│   ├── search.py           # Viewport search parser
│   ├── place.py            # Place detail card parser
│   ├── search_reviews.py   # Search review list parser
│   ├── reviews.py          # Review record mapping
│   ├── review_page.py      # Single-review Maps page reader (exact dates)
│   ├── travel_reviews.py   # Google Travel partner review parser (Tripadvisor, etc.)
│   ├── photos.py           # Image gallery and photometa author parser
│   ├── questions.py        # Local Q&A Boq application parser
│   ├── order_online.py     # Google Food widget parser (pickups and deliveries)
│   ├── reservation.py      # Google Reserve page parser (table booking providers)
│   └── urls.py             # Maps URL classifier and parameter extractor
│
├── pipeline/               # Worker threads and task execution
│   ├── runner.py           # Job lifecycle manager (lock, prepare, run, reconcile)
│   ├── worker.py           # Worker threads claiming tasks from SQLite queue
│   ├── planner.py          # Initial territory and search grid planning
│   ├── discovery.py        # Viewport search handler and adaptive subdivision
│   ├── details.py          # Place detail batch fetcher
│   ├── review_list.py      # Review list coordinator (Search + Travel)
│   ├── review_dates.py     # Maps exact date enrichment and cross-job date caching
│   ├── review_filter.py    # Keyword filtering logic
│   ├── sections.py         # Photos gallery handler
│   ├── specials.py         # Online ordering and reservation handlers
│   ├── questions.py        # Q&A coordinator
│   └── extras.py           # Google web results organic SERP parser
│
├── enrich/                 # External business website enrichment
│   ├── website.py          # Website crawler and domain orchestrator
│   └── contacts.py         # Regex email, phone (E.164), social, and Impressum extractors
│
└── export/                 # Dataset serializers
    ├── apify.py            # Apify-compatible dataset exporter (JSON, JSONL, CSV)
    └── native.py           # Multi-table native exporter (places, reviews, images, contacts)
```

---

## 2. Task Queue & Priorities

Tasks are stored in SQLite and claimed by workers using `SELECT ... FOR UPDATE` semantics with leased locks (`lease_gen`). Tasks are processed strictly by priority:

| Priority | Task Kind | Description |
| --- | --- | --- |
| `10` | `resolve` | Resolves Maps URLs or non-standard Place IDs to internal identifiers. |
| `20` | `search` | Searches a single viewport cell. |
| `25` | `controlpass`| Generates the offset grid pass once main cells are done. |
| `30` | `place` | Fetches full place detail cards in batches of up to 6 per request. |
| `40` | `reviews` | Pages through review lists from Google Search and Travel. |
| `41` / `50` | `website` | Crawls business websites (priority 41 with `websiteEarly`, else 50). |
| `42` | `review_dates` | Reads individual Maps review pages to extract true timestamps. |
| `45` | `images` | Pages through photo galleries and retrieves photographer metadata. |
| `47` | `reservation` | Inspects Google Reserve for dining booking partners. |
| `48` | `order_online`| Inspects Google Food for delivery and pickup providers. |
| `49` | `questions` | Fetches local Q&A from Google's Q&A application. |
| `55` | `webresults` | Performs Google web search for organic business results. |


---

## 3. Field Mapping Reference

| Output Field | Source | Notes |
| --- | --- | --- |
| `title`, `subTitle` | Place Card `p[11]`, `p[101]` | Business name and secondary title. |
| `description` | Place Card `p[32][1][1]` | Google's editorial description. |
| `ownerDescription` | Place Card `p[154][0][0]` | "From the business" text. |
| `categoryName`, `categories` | Place Card `p[13][0]`, `p[13]` | Primary and secondary categories. |
| `address`, `street`, `city`, `postalCode`, `state`, `countryCode` | Place Card `p[39]`, `p[183][1]` | Full address and structured geographic components. |
| `location` (`lat`, `lng`) | Place Card `p[9]` | Coordinates rounded to 7 decimal places. |
| `plusCode` | Place Card `p[183][2][2][0]` | Open Location Code. |
| `website` | Place Card `p[7][0]` | Official business website URL. |
| `phone`, `phoneUnformatted` | Place Card `p[178][0]` | Display and unformatted phone strings. |
| `totalScore`, `reviewsCount` | Place Card `p[4][7]`, `p[4][8]` | Average rating and total review count. |
| `openingHours` | Place Card `p[203][0]` | Operating hours (Monday-first for English, local for other languages). |
| `reviewsDistribution` | Place Card `p[175][3]` | Breakdown of 1-star through 5-star ratings count. |
| `placesTags`, `reviewsTags` | Place Card `p[186][2]`, `p[153][0]` | Topic tags and review keyword summaries. |
| `claimThisBusiness` | Place Card `p[49][1]` | True if Google shows "Claim this business". |
| `permanentlyClosed`, `temporarilyClosed` | Place Card `p[23]`, `p[160]` | Business operational status flags. |
| `reserveTableUrl`, `tableReservationProviders` | Place Card `p[75]` & Reserve page | Booking links and providers (OpenTable, Resy, etc.). |
| `googleFoodUrl`, `orderOnline` | Place Card `p[75]` & Food widget | Direct ordering, pickup, and delivery options with estimated fees. |
| `questionsAndAnswers` | Google Local Q&A App | Questions, answers, author details, dates, and merchant answers. |
| `reviews[].publishedAtDate` | Maps single-review page | Exact ISO publication timestamp (in `exact` review mode). |
| `reviews[].lastEditedAtDate` | Maps single-review page | Exact ISO edit timestamp if the review was modified. |
| `reviews[].responseFromOwnerDate` | Maps single-review page | Exact ISO timestamp of owner's reply. |
| `images[].uploadedAt` | Photometa | Photo publication date reported by Google. |
| `emails`, `phones`, `socials`, `impressum` | Website enrichment | Harvested direct contact details from business website. |

