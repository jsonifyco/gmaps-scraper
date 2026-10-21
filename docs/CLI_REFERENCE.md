# CLI Reference

The `gmaps-scraper` CLI manages input validation, scraping jobs, job status, pauses, resumes, and data exports.

All commands can be invoked via the console script `gmaps-scraper` or via `python3 -m gmaps_scraper`.

```bash
# General invocation syntax
gmaps-scraper <command> [options]

# Or with explicit Python module execution
python3 -m gmaps_scraper <command> [options]
```

---

## Commands Overview

| Command | Purpose |
| --- | --- |
| `validate` | Validates an input JSON file against the Apify schema and prints the normalized plan. |
| `run` | Starts a new scraping job from an input file and exports results upon completion. |
| `status` | Displays live or completed job progress, section coverage, review gaps, and bro costs. |
| `stop` | Sends a graceful stop request to an active job. Open browser sessions close cleanly. |
| `resume` | Resumes an interrupted or stopped job from its SQLite state. |
| `reconcile` | Recovers an orphaned job after a process crash, closing dead browser sessions and recording billing. |
| `export` | Re-exports data from a job's SQLite database into Apify or Native JSON/JSONL/CSV formats. |
| `benchmark` | Executes automated discovery or deep benchmark profiles and compares against Apify datasets. |

---

## Command Reference

### `validate`

Validates the input JSON file without starting any browser session or making network calls.

```bash
gmaps-scraper validate examples/quick-search.json
```

- **Arguments:**
  - `input`: Path to the input JSON file.
- **Output:** Prints a JSON report with `"valid": true` and the normalized plan, or `"valid": false` and an array of `"problems"`.

---

### `run`

Starts a new scraping run. Automatically creates the job directory, plans territories and search cells, launches worker browsers, executes tasks, and exports datasets when done.

```bash
gmaps-scraper run examples/quick-search.json --job-dir runs/my-job
```

- **Options:**
  - `input`: Path to the input JSON file.
  - `--job-dir DIR`: Directory to store the job's SQLite database (`state.sqlite`), logs (`run.log`), and exports. Must not be reused by a different job unless resuming.
  - `--export {apify,native,all,none}`: Export format generated after completion (default: `all`).
  - `--env FILE`: Path to `.env` file containing `BRO_API_KEY`.
  - `-v, --verbose`: Enable debug logging.

---

### `status`

Inspects an active or finished job directory. Reads directly from `state.sqlite` without network calls.

```bash
# Text summary
gmaps-scraper status --job-dir runs/my-job

# Structured JSON output
gmaps-scraper status --job-dir runs/my-job --json
```

- **Options:**
  - `--job-dir DIR`: Path to the job directory.
  - `--json`: Output full machine-readable status as JSON.

---

### `stop`

Requests a graceful shutdown of a running job. Active workers complete their current command, close their remote browser sessions, update final billing, and mark the job as stopped.

```bash
gmaps-scraper stop --job-dir runs/my-job
gmaps-scraper stop --job-dir runs/my-job --reason "user_paused"
```

- **Options:**
  - `--job-dir DIR`: Path to the job directory.
  - `--reason TEXT`: Custom reason string recorded in job history (default: `stop_requested`).

---

### `resume`

Resumes a stopped, interrupted, or partially completed job from its saved SQLite checkpoint.

```bash
# Standard resume
gmaps-scraper resume --job-dir runs/my-job

# Resume and re-queue all failed tasks (e.g. websites that timed out)
gmaps-scraper resume --job-dir runs/my-job --retry-failed

# Re-queue only failed reviews
gmaps-scraper resume --job-dir runs/my-job --retry-failed=reviews

# Adjust browser concurrency for the remaining work
gmaps-scraper resume --job-dir runs/my-job --concurrency 5
```

- **Options:**
  - `--job-dir DIR`: Path to the job directory.
  - `--retry-failed [KIND]`: Re-queues failed tasks. Pass no argument (or `all`) for all failed tasks, or specify a section kind (e.g., `reviews`, `website`, `images`, `search`).
  - `--concurrency N`: Update number of parallel browser sessions for the resumed job.
  - `--export {apify,native,all,none}`: Export format upon completion (default: `all`).
  - `--env FILE`: Path to `.env` file containing `BRO_API_KEY`.
  - `-v, --verbose`: Enable debug logging.

---

### `reconcile`

Recovers a job left in limbo if the scraper process was abruptly killed (e.g., `SIGKILL`, OOM, or power loss). It queries the bro API, stops any leftover browser sessions, records final billing, and re-queues stale in-flight tasks.

```bash
gmaps-scraper reconcile --job-dir runs/my-job
```

- **Options:**
  - `--job-dir DIR`: Path to the job directory.
  - `--env FILE`: Path to `.env` file containing `BRO_API_KEY`.

---

### `export`

Generates dataset files from an existing job's `state.sqlite` database without contacting Google Maps or bro.

```bash
# Apify-compatible JSON dataset
gmaps-scraper export --job-dir runs/my-job --format apify --out dataset.json

# Apify-compatible JSONL or CSV
gmaps-scraper export --job-dir runs/my-job --format apify --out dataset.jsonl --jsonl
gmaps-scraper export --job-dir runs/my-job --format apify --out dataset.csv --csv

# Native multi-file JSONL export (places, reviews, images, contacts, discoveries)
gmaps-scraper export --job-dir runs/my-job --format native --out native/

# Native multi-file CSV export
gmaps-scraper export --job-dir runs/my-job --format native --out native/ --csv
```

- **Options:**
  - `--job-dir DIR`: Path to the job directory.
  - `--format {apify,native}`: Target export format (default: `apify`).
  - `--out PATH`: Destination file (for Apify JSON/JSONL/CSV) or directory (for native multi-table export).
  - `--jsonl`: Export as JSON Lines.
  - `--csv`: Export as CSV.

---

### `benchmark`

Runs standardized measurement profiles or compares scraped data against an Apify reference dataset.

```bash
# Validate benchmark profile input without running
gmaps-scraper benchmark discovery --job-dir runs/bench1 --validate-only

# Run discovery benchmark profile
gmaps-scraper benchmark discovery --job-dir runs/bench1 --concurrency 3

# Run deep details/reviews benchmark profile
gmaps-scraper benchmark deep --job-dir runs/bench2 --concurrency 5

# Compare existing job output against an Apify dataset
gmaps-scraper benchmark compare --job-dir runs/my-job --apify-dataset apify_ref.json
```

- **Options:**
  - `profile`: Profile type (`discovery`, `deep`, or `compare`).
  - `--job-dir DIR`: Destination or existing job directory.
  - `--input FILE`: Custom input JSON replacing the default shipped benchmark profile.
  - `--validate-only`: Validate profile input and exit without starting sessions.
  - `--apify-dataset FILE`: Path to Apify dataset JSON to compare against.
  - `--concurrency N`: Override concurrency level.
  - `--env FILE`: Path to `.env` file.

---

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success: Run finished (`completed` or `stopped`), validation succeeded, or export completed. |
| `1` | Run finished with failures or unhandled runtime error. |
| `2` | Bad input or missing configuration (JSON syntax error, invalid parameters, missing job). |
| `3` | Job locked or busy (another process is currently holding the job directory lock). |
| `130` | Interrupted by user (`SIGINT` / Ctrl+C); state is safely written to SQLite before exiting. |

- **Reports:**
  - Job status (`running`, `completed`, `stopped`, `failed`) and stop reason.
  - Territory area (km²), accepted places, and unverified location count.
  - Tasks count by status (`done`, `running`, `pending`, `failed`).
  - Section breakdown (`details`, `reviews`, `reviewDates`, `images`, `contacts`, `orderOnline`, etc.).
  - Review gaps (places where Google reported more reviews than collected).
  - Billing summary (bro sessions, active sessions, total USD billed, command count).
