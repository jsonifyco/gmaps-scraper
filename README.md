# Open-Source Python Google Maps Scraper

Open Source Google Maps scraper built on the [getbro.ws](https://getbro.ws) stealth browser framework.

This scraper accepts Apify ([`compass/crawler-google-places`](https://apify.com/compass/crawler-google-places)) input fields, produces identical Apify dataset outputs, and supports spatial grid discovery, rich place details, reviews with exact timestamps, high-res photos, and website contact extraction. Every request runs inside a real Chrome hosted remotely in a *bro session* with its own residential IP.

There is no need for local browser installations, no Playwright, and no complex system dependencies. All job state is backed by SQLite, so you can stop, resume, and re-export runs at any time.

```bash
export BRO_API_KEY=sk_...
gmaps-scraper run examples/quick-search.json --job-dir runs/job1
```

---

## What You Can Scrape

| Target | Description | Details Included |
| --- | --- | --- |
| **Places & Search** | Keyword searches, coordinates, or city areas. | Name, categories, address, coordinates, plus codes, status. |
| **Place Details** | Complete business profile cards. | Operating hours, rating breakdown, phone, website, attributes. |
| **Reviews** | Customer reviews with exact timestamps. | Review text, star rating, publish & edit dates, owner replies, partner reviews. |
| **Photos** | High-resolution gallery images. | Full-res photo URLs, photographer names, profile links, and upload dates. |
| **Website Contacts** | Direct contacts from business websites. | Emails, E.164 phones, social links (IG, FB, LinkedIn, X, etc.), Impressum. |
| **Questions & Answers** | Google local community Q&A. | Questions, answers, community author info, and merchant responses. |
| **Ordering & Booking** | Food orders and table reservations. | Pickup/delivery providers with estimated fees (Food) & booking links (Reserve). |
| **Web Results** | Organic Google Search results. | Top search engine results for the business name with resolved destinations. |
| **Directories** | Businesses located inside larger venues. | Sub-places located in malls, airports, and commercial centers. |

---

## Install

Requires Python 3.12+ and a getbro API key ([sign up at getbro.ws](https://getbro.ws) to get your API key):

```bash
git clone https://github.com/jsonifyco/gmaps-scraper.git && cd gmaps-scraper
cp .env.example .env        # add your BRO_API_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # installs dependencies and the `gmaps-scraper` command
```

Or install directly via `pip`:

```bash
pip install gmaps-scraper-bro
```

If you prefer to run from source without installing the package:

```bash
python3 -m pip install -r requirements.lock
PYTHONPATH=src python3 -m gmaps_scraper --help
```



---

## Quick Start CLI Examples

```bash
# 1. Validate an input file before running
gmaps-scraper validate examples/quick-search.json

# 2. Run a quick search in an area
gmaps-scraper run examples/quick-search.json --job-dir runs/pittsburgh-coffee

# 3. High-density spatial search with adaptive grid subdivision
gmaps-scraper run examples/deep-coverage.json --job-dir runs/soho-cafes

# 4. Scrape full place details, reviews, and website contacts across 2 parallel browsers
gmaps-scraper run examples/e2e-full.json --job-dir runs/berlin-bakeries

# 5. Extract filtered reviews with exact timestamps and cross-job date caching
gmaps-scraper run examples/reviews-filtered.json --job-dir runs/reviews-batch

# 6. Comprehensive place enrichment (Q&A, website contacts, food orders, table bookings)
gmaps-scraper run examples/place-enrichments.json --job-dir runs/place-details

# 7. Check live job progress, coverage stats, and bro costs
gmaps-scraper status --job-dir runs/berlin-bakeries

# 8. Gracefully stop an active run
gmaps-scraper stop --job-dir runs/berlin-bakeries

# 9. Resume a paused or interrupted run (scale up to 5 browsers)
gmaps-scraper resume --job-dir runs/berlin-bakeries --concurrency 5

# 10. Resume and retry only failed tasks (e.g. websites that timed out)
gmaps-scraper resume --job-dir runs/berlin-bakeries --retry-failed=website

# 11. Re-export collected data to Apify CSV or Native multi-table format
gmaps-scraper export --job-dir runs/berlin-bakeries --format apify --out dataset.csv --csv
gmaps-scraper export --job-dir runs/berlin-bakeries --format native --out native/ --csv
```

---

## Input & Output Basics

Control the scraper completely via a JSON file compatible with Apify standard inputs:

**`input.json` Example:**
```json
{
  "searchStringsArray": ["bakery"],
  "locationQuery": "Kreuzberg, Berlin, Germany",
  "maxCrawledPlacesPerSearch": 20,
  "language": "en",
  "scrapePlaceDetailPage": true,
  "maxReviews": 10,
  "reviewsSort": "newest",
  "maxImages": 5,
  "scrapeContacts": true,
  "bro": {
    "concurrency": 2,
    "coverage": "deep",
    "reviewsMode": "exact"
  }
}
```

Run it with: `gmaps-scraper run input.json --job-dir runs/my-job`

**Where does the data go?**
All job artifacts live inside `--job-dir`:
- **`dataset.json`**: Standard Apify dataset items (or `.csv` / `.jsonl` depending on `--export`).
- **`native/`**: Normalized relational tables (`places.jsonl`, `reviews.jsonl`, `images.jsonl`, `contacts.jsonl`).
- **`report.json`**: Summary metrics, spatial coverage statistics, review gaps, and exact USD billing.
- **`state.sqlite`**: Transactional database storing every task, cursor, and result.
- **`run.log`**: Detailed execution log.

---

## How It Works: Discovery, Reviews & State

- **Adaptive Spatial Coverage:** Rather than relying on simple map scrolling, `gmaps-scraper` splits geographic boundaries into overlapping viewport cells, dynamically subdividing dense clusters (`adaptive-v4`) and running offset passes to uncover up to 2.5x more pins than standard search.
- **Exact Review Timestamps:** Google Search lists reviews quickly but collapses publish and edit dates into a single relative string. In `exact` mode, the scraper reads single-review Maps pages in parallel to record true `publishedAtDate`, `lastEditedAtDate`, and owner response dates.
- **Shared Date Caching:** Set `bro.reviewsDateCache` to an SQLite file to share known review dates across recurring runs. Unchanged reviews cost zero browser requests on subsequent scrapes.
- **Zero Data Loss:** Every result and pagination cursor is committed to SQLite atomically. If your machine restarts or connection drops, run `gmaps-scraper resume` to continue instantly.

---

## Documentation

For deep dives into configurations, schemas, and architecture, see the guides in `docs/`:

- **[CLI Reference](docs/CLI_REFERENCE.md)** — Complete command-line manual, flags, and exit codes.
- **[Input & Output Reference](docs/INPUT_OUTPUT.md)** — Apify input fields, the `bro` configuration object, and export schemas.
- **[Discovery & Map Coverage](docs/DISCOVERY_AND_COVERAGE.md)** — Nominatim geocoding, custom GeoJSON, grid subdivision, and coverage diagnostics.
- **[Reviews & Data Enrichment](docs/REVIEWS_AND_ENRICHMENT.md)** — Review dates, caching, website contacts, Impressum, Q&A, and booking options.
- **[Sessions, Proxies & Resumability](docs/SESSIONS_AND_RESUMABILITY.md)** — Multi-browser concurrency, residential IP routing, stops, resumes, and crash recovery.
- **[Architecture & Field Map](docs/ARCHITECTURE_AND_FIELD_MAP.md)** — Pipeline internals, task priority queue, and complete Apify-to-Maps field mapping.
- **[Compatibility & Limits](docs/COMPATIBILITY_AND_LIMITS.md)** — Apify feature parity matrix, unsupported add-ons, and known technical boundaries.

---

## License

MIT

