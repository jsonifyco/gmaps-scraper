# Reviews & Data Enrichment

`gmaps-scraper` extracts rich, verified business data: customer reviews with exact publication timestamps, photographer attributions, website contact information, local Q&A, and online ordering/booking options.

---

## 1. Reviews

Reviews are configured via standard Apify parameters and fine-tuned using `bro` options (see `examples/reviews-filtered.json`):

```json
{
  "maxReviews": 50,
  "reviewsSort": "newest",
  "reviewsStartDate": "2026-01-01",
  "reviewsFilterString": "outdoor patio",
  "reviewsOrigin": "all",
  "bro": {
    "reviewsMode": "exact",
    "reviewsDateCache": "shared_reviews_cache.sqlite",
    "reviewsEnrichBatch": 50
  }
}
```

### Review Route & Date Modes

Google Search's backend lists reviews at 60 items per request without the memory bloat or scroll instability of the standard Maps client. However, search listings provide only a single ambiguous timestamp (the last edit date if modified, or publication date if untouched).

To solve this, the scraper offers two review modes:

| Mode (`bro.reviewsMode`) | How it Works | Dates Populated | Speed & Cost |
| --- | --- | --- | --- |
| `"exact"` *(default)* | Fetches Search list, then reads individual Maps review pages in parallel tasks. | `publishedAtDate`, `lastEditedAtDate`, `responseFromOwnerDate`. | Thorough (~6s per review cold; 0s warm). |
| `"fast"` | Extracts Search list only; skips Maps single-review pages unless already cached. | `publishedOrEditedAtDate` (Search timestamp); `publishedAtDate` null if uncached. | Fast & minimal bro cost. |

### Cross-Job Date Caching (`bro.reviewsDateCache`)
Set `bro.reviewsDateCache` to an SQLite file path (e.g., `"shared_dates.sqlite"`). Publication dates are immutable; once fetched, they never expire. When you re-scrape the same places or run recurring jobs:
- Known reviews are matched from the cache at zero cost.
- Only newly posted reviews or reviews with modified owner replies are fetched from Maps.
- `bro.reviewsDateTtlDays` (default: 30 days) controls how often mutable fields (edits and replies) are re-checked.

### Partner Reviews & Ordering
- **`reviewsOrigin: "all"`** *(default)*: Includes both Google reviews and third-party partner reviews (Tripadvisor, Priceline, Trip.com, etc.) in the exact ranking order shown on Google Travel.
- **`reviewsOrigin: "google"`**: Restricts extraction strictly to Google-authored reviews.

### Review Filtering & Date Cutoffs
- **`reviewsFilterString`**: Performs keyword filtering. Under `core-2026.10`, this queries Google's review search backend, returning matched snippets with exact date lookups.
- **`reviewsStartDate`**: Accepts ISO dates (`"2026-01-01"`) or relative expressions (`"60 days"`, `"6 months"`). When sorted by `newest`, the scraper stops reading once it encounters `reviewsDateCutoffPages` (default: 2) consecutive pages older than the cutoff.

---

## 2. Photos & Photographer Credits

Configure photo extraction using `maxImages` and `scrapeImageAuthors`:

```json
{
  "maxImages": 20,
  "scrapeImageAuthors": true,
  "bro": {
    "imagesExhaustive": false
  }
}
```

- **Paging:** Reads Google's gallery endpoint (`hspqX`) in batches of 100 photos per request.
- **High Resolution:** Photo URLs are signed and normalized to `=w1920-h1080-k-no`.
- **Photographer Attribution:** When `scrapeImageAuthors: true`, queries Google's `photometa` endpoint in batches of 25 to extract author names and profile links.
- **Upload Timestamps:** `uploadedAt` reflects the photometa timestamp reported by Apify. Raw observations and metadata hashes are preserved in native exports under `dateEvidence`.


---

## 3. Website Contact Enrichment

When `scrapeContacts: true` is enabled, the scraper visits the business website to harvest direct contact details:

```json
{
  "scrapeContacts": true,
  "bro": {
    "maxWebsitePages": 10,
    "websiteReadMode": "open",
    "websiteEarly": false
  }
}
```

### What is Extracted
- **Emails:** Regex and `mailto:` parsing with false-positive filtering.
- **Phones:** Cleaned and normalized to international **E.164** format using the place's country.
- **Social Profiles:** Direct profile links across Instagram, Facebook, LinkedIn, X/Twitter, YouTube, TikTok, Pinterest, and Discord.
- **Impressum:** Dedicated German/EU legal imprint parser extracting company name, registration number, VAT ID, and authorized representatives.

### Chain vs. Branch Ownership Attribution
If several locations share the same domain (e.g., Starbucks or a retail chain):
- A phone number found on the website that matches the specific branch's Maps card is attributed as `"ownership": "place"`.
- General corporate phone numbers or emails found on the shared domain are attributed as `"ownership": "domain"` and listed under `phonesUncertain` so you don't confuse corporate headquarters with a local store.

### Performance Tuning: `open` vs `fetch`
- **`websiteReadMode: "open"`** *(default)*: Opens each page in the real browser. Handles heavy SPAs and complex JavaScript.
- **`websiteReadMode: "fetch"`**: Opens the homepage, then downloads subsequent pages on the same domain via background HTTP requests. ~2.4x faster, but may miss links generated purely by client-side JavaScript.
- **`websiteEarly: true`**: When running high concurrency (10–20 workers), schedules contact extraction earlier so website loading overlaps with review scraping.

---

## 4. Local Questions & Answers (Q&A)

Google retired its public Q&A API in late 2025. `gmaps-scraper` directly queries Google's local Q&A Boq application (`/local/place/qa/question/...`; see `examples/place-enrichments.json`):

```json
{
  "maxQuestions": 10,
  "bro": {
    "questionsAllAnswers": false
  }
}
```

- Extracts answered questions in Google's ranking order.
- Each question includes up to 5 answers with author name, author avatar, answer date, and merchant badge indicator.
- Set `bro.questionsAllAnswers: true` to page through and retrieve every answer for questions with more than 5 responses.
- In native exports (`native/places.jsonl`), unanswered community questions (`"Others are asking"`) are also preserved.

---

## 5. Online Ordering & Table Reservations

```json
{
  "scrapeOrderOnline": true,
  "scrapeTableReservationProvider": true
}
```

- **`scrapeOrderOnline`**: Reads Google Food widget (`googleFoodUrl`). Returns pickup and delivery options with partner name, order link, logo, and estimated fees/times.
- **`scrapeTableReservationProvider`**: Reads Google Reserve (`reserveTableUrl`). Returns table booking providers (OpenTable, Resy, DesignMyNight, etc.) and direct reservation URLs.

---

## 6. Google Web Results

```json
{
  "includeWebResults": true
}
```

Renders a live Google web search for the business name and extracts top organic SERP results (titles, breadcrumbs, descriptions, and destination URLs, resolving Google `/goto` redirects).

