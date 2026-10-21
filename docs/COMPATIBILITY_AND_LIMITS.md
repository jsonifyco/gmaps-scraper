# Compatibility, Limitations & Trade-Offs

This guide provides an honest, transparent breakdown of how `gmaps-scraper` compares to Apify's `compass/crawler-google-places`, which add-ons are unsupported, and what practical trade-offs to keep in mind.

---

## 1. Apify Compatibility Matrix

| Feature Area | Support Status | Notes |
| --- | --- | --- |
| Input schema | **100% compatible** | Accepts all 39 Apify input fields with identical types, enums, defaults, and validation constraints. |
| Output schema | **100% compatible** | Outputs identical Apify dataset shapes in JSON, JSONL, and CSV. |
| Spatial discovery | **Supported & Enhanced** | Supports Nominatim lookup, structured locations, and GeoJSON shapes. `adaptive-v4` and offset control passes uncover up to 2.5x more places in dense areas. |
| Place details | **Supported** | Full business details (phone, website, hours, tags, categories, coordinates, plus codes, status). |
| Reviews | **Supported** | Extracts Google & partner reviews (Tripadvisor, Priceline, etc.). Supports `reviewsSort`, keyword search (`reviewsFilterString`), and start date filters. |
| Exact review dates | **Enhanced** | Separates publication dates, edit dates, and owner reply dates via single-review Maps page verification (`reviewsMode: exact`). |
| Photo galleries | **Supported** | Paged extraction of high-resolution images and author profile attribution. |
| Website contacts | **Supported** | Direct extraction of emails, E.164 phones, social links, and Impressum without external credit costs. |
| Local Q&A | **Supported** | Direct integration with Google's local Q&A Boq application (`maxQuestions`). |
| Booking & Ordering | **Supported** | Google Reserve booking links and Google Food pickup/delivery options. |
| State & Resume | **Enhanced** | Local SQLite checkpointing (`state.sqlite`). Stop, resume, retry failed tasks, or re-export at any time. |

---

## 2. Unsupported Apify Add-Ons

The following paid add-ons are not implemented and are rejected at input validation before any browser session starts:

| Parameter | Reason | Alternative |
| --- | --- | --- |
| `maximumLeadsEnrichmentRecords > 0` | Third-party paid enrichment service. | Use `scrapeContacts: true` to extract emails and phones directly from the business website for free. |
| `verifyLeadsEnrichmentEmails: true` | Third-party paid email deliverability check. | Verify extracted emails using your preferred email validation pipeline. |
| `scrapeSocialMediaProfiles` | Third-party paid profile lookup. | `scrapeContacts: true` automatically harvests official Instagram, Facebook, LinkedIn, X, TikTok, and YouTube profile links from the site. |
| `enableCompetitorAnalysis` | Apify AI prompt generation add-on. | Run your own LLM analysis directly over the resulting `dataset.json`. |

---

## 3. Input Priority & Override Rules

To match Apify's exact precedence behavior, the scraper applies the following overrides and issues warnings during validation:
1. **Direct targets take precedence:** If `startUrls` or `placeIds` are supplied, general `searchStringsArray` terms are ignored.
2. **Free-text location takes precedence:** `locationQuery` overrides structured fields (`countryCode`, `state`, `city`) and `customGeolocation`.
3. **Custom geolocation overrides structured fields:** `customGeolocation` overrides `countryCode`, `state`, and `city`.
4. **Categories as fallback queries:** If no `searchStringsArray` is provided, `categoryFilterWords` are automatically utilized as search queries.

---

## 4. Known Limitations & Edge Cases

### Pure HTTP-Only Websites
The remote browser runs a modern Chrome environment configured with strict transport security. Websites served exclusively over unencrypted HTTP (without HTTPS support) cannot be loaded. Their contact extraction tasks fail gracefully and are marked as `failed` in the contact section.

### Partner Review Publication Dates
Third-party hotel reviews (Tripadvisor, Priceline, Trip.com, etc.) served through Google's Travel backend provide relative date strings (e.g. `"2 weeks ago"`). Exact calendar dates are only populated for the few reviews that Google also highlights directly on the main place card.

### Exact Review Dates vs. Run Duration
- In `"exact"` review mode, the scraper reads single-review Maps pages to retrieve true publication, edit, and owner-reply timestamps (~6 seconds per review on a cold cache). For places with thousands of reviews, cold runs take significantly longer.
- For high-speed runs where approximate timestamps are sufficient, set `"reviewsMode": "fast"`.
- For recurring scrapes, enable `"reviewsDateCache"`: known reviews are retrieved instantly from SQLite at zero cost.

### Search Limits on Sparse Queries
Searching for a broad category without a location (e.g., `"restaurant"` with a global viewport) relies on Google's automatic geocoding and may cap results at 120–165 pins. Always specify a `locationQuery` or geographic bounding box for complete coverage.
