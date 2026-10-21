# Sessions, Proxies & Resumability

`gmaps-scraper` executes automation remotely in real Chrome instances managed by the [getbro.ws](https://getbro.ws) service. All scraping state is tracked in a local SQLite database, providing true multi-browser concurrency, automatic fault recovery, and 100% crash-proof resumability.

---

## 1. Remote Browser Sessions

Every worker thread connects to its own isolated remote browser VM via the official `bro-api-sdk`:
- **No Local Chrome or Playwright:** No heavy Chromium binaries, display drivers, or system memory exhaustion on your machine.
- **Dedicated Sessions:** Each session runs in its own sandbox with clean storage and isolated network routing.
- **Session Lifetimes:** Remote browser sessions can live up to 6 hours (`bro.sessionTimeout: 21600`). Before a session expires, the worker rotates to a fresh session automatically.
- **Billing Sync:** When a browser session closes, exact billing metrics (compute time, proxy traffic, total USD) are reconciled and written to SQLite.

---

## 2. Multi-Browser Concurrency

Configure the number of parallel browsers using `bro.concurrency` (or the CLI `--concurrency` flag on resume):

```json
{
  "bro": {
    "concurrency": 5
  }
}
```

```bash
# Start a job with 5 parallel browsers
gmaps-scraper run input.json --job-dir runs/job1

# Resume a job and scale up to 10 browsers
gmaps-scraper resume --job-dir runs/job1 --concurrency 10
```

- **Shared Work Queue:** Workers pull tasks dynamically from the SQLite queue. Faster workers naturally pull more work.
- **Independent Teardown:** When a worker runs out of available tasks, it terminates its remote browser immediately to stop compute billing without waiting for other workers.
- **Up to 20 Browsers:** Supports up to 20 concurrent browsers per job (subject to your getbro account tier limit).

---

## 3. Residential Proxies & Geo-Targeting

Configure proxy routing under `bro.proxy`:

```json
{
  "bro": {
    "proxy": {
      "enabled": true,
      "tier": "basic",
      "policy": "basic",
      "country": "US"
    },
    "reviewsProxy": {
      "enabled": true,
      "tier": "lite",
      "country": "US"
    }
  }
}
```

| Parameter | Options | Description |
| --- | --- | --- |
| `tier` | `"lite"`, `"basic"`, `"premium"` | Residential IP pool quality. `"lite"` is cost-effective for general browsing; `"premium"` provides highest purity IPs. |
| `policy` | `"html_only"`, `"basic"`, `"extended"`, `"full"` | Traffic routing policy. `"basic"` proxies HTML/XHR/WebSockets while unproxied static assets bypass the proxy to save bandwidth costs. |
| `country` | 2-letter ISO code (e.g. `"US"`, `"DE"`, `"GB"`) | Exit country for residential IP routing. |
| `city` | string (e.g., `"Frankfurt"`, `"London"`) | Specific exit city. |
| `reviewsProxy`| object or `null` | Separate proxy configuration for review tasks. Defaults to a lite proxy in the target country to ensure Google serves sorted review lists. |

---

## 4. Crash-Proof Resumability

All progress is journaled in `runs/<job-dir>/state.sqlite` using SQLite Write-Ahead Logging (WAL):
- **Atomic Commits:** Each page of search results, reviews, or place cards commits in a single transaction together with its continuation cursor.
- **Zero Data Loss:** If the machine reboots, connection drops, or the terminal is closed, no collected data is lost.

### Graceful Stopping
To stop a long-running scrape without abandoning in-flight tasks:

```bash
gmaps-scraper stop --job-dir runs/job1
```
Workers finish their current HTTP/CDP command, close their remote browser sessions, sync billing, and mark the job as stopped.

### Resuming Work
To pick up right where the job left off:

```bash
gmaps-scraper resume --job-dir runs/job1
```
The scraper reads `state.sqlite`, boots fresh browser sessions, and resumes work on the remaining task queue.

### Retrying Failed Tasks (`--retry-failed`)
If third-party websites timed out or temporary network errors caused individual tasks to fail:

```bash
# Re-queue all failed tasks across all sections
gmaps-scraper resume --job-dir runs/job1 --retry-failed

# Re-queue only failed website contact tasks
gmaps-scraper resume --job-dir runs/job1 --retry-failed=website

# Re-queue only failed review tasks
gmaps-scraper resume --job-dir runs/job1 --retry-failed=reviews
```

### Post-Mortem Reconciliation (`reconcile`)
If the scraper process crashed abruptly (e.g. `kill -9` or server power outage) leaving remote sessions running:

```bash
gmaps-scraper reconcile --job-dir runs/job1
```
`reconcile` inspects the job's database, contacts getbro to stop any orphaned remote sessions, records final billed amounts, recovers unfinished tasks back into the pending queue, and transitions the job to `stopped`.
