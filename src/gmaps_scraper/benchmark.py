"""Benchmark profiles and comparison with an Apify dataset.

``discovery`` and ``deep`` run a job from a standard input (shipped in the package,
``gmaps_scraper/profiles/benchmark-*.json``; ``--input`` replaces it) and record time,
volume, failures and bro billing in ``benchmark.json``; ``compare`` matches an
existing job against an Apify dataset: identifier overlap, per-field equality
on common places (only where both sides have a value), presence counts, and
review depth.  Speed and price are reported separately from data quality.
"""

from __future__ import annotations

import json
import time
from importlib import resources
from collections import Counter
from pathlib import Path
from typing import Any

from .export.apify import apify_items
from .input_model import load_input, parse_input
from .store import Store, loads

PROFILES = ("discovery", "deep")


def profile_input(profile: str) -> tuple[str, dict]:
    """The standard input of a profile, read from the installed package (not from a checkout)."""
    ref = resources.files("gmaps_scraper.profiles") / f"benchmark-{profile}.json"
    return f"gmaps_scraper/profiles/benchmark-{profile}.json", json.loads(ref.read_text(encoding="utf-8"))

#: fields compared on common places; volatile ones (prices, counts, photo URLs) are listed apart
STABLE_FIELDS = ["title", "categoryName", "categories", "address", "neighborhood", "street", "city", "postalCode",
                 "state", "countryCode", "website", "phone", "phoneUnformatted", "location", "plusCode", "placeId",
                 "fid", "cid", "kgmid", "businessProfileId", "permanentlyClosed", "temporarilyClosed", "subTitle",
                 "description", "ownerDescription", "locatedIn", "openingHours", "url", "hotelStars"]
VOLATILE_FIELDS = ["totalScore", "reviewsCount", "imagesCount", "price", "reviewsDistribution", "popularTimesHistogram",
                   "additionalInfo", "peopleAlsoSearch", "reviewsTags", "placesTags", "imageCategories",
                   "wasOpenAtScrapeTime"]


def _empty(v: Any) -> bool:
    """No value.  ``False`` and ``0`` are values (a place that is not closed, zero reviews)."""
    return v is None or (isinstance(v, (str, list, dict)) and not v)


def _merge_chunks(items: list[dict]) -> dict[str, dict]:
    """One record per placeId: places with more than 5 000 reviews come as several items."""
    out: dict[str, dict] = {}
    for item in items:
        pid = item.get("placeId")
        if not pid:
            continue
        if pid not in out:
            out[pid] = {**item, "reviews": list(item.get("reviews") or [])}
            continue
        known = {r.get("reviewId") for r in out[pid]["reviews"]}
        out[pid]["reviews"].extend(r for r in item.get("reviews") or [] if r.get("reviewId") not in known)
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(a, dict) and isinstance(b, dict) and set(a) == {"lat", "lng"} == set(b):
        return abs(a["lat"] - b["lat"]) < 1e-6 and abs(a["lng"] - b["lng"]) < 1e-6
    return a == b


def compare_items(ours: list[dict], theirs: list[dict]) -> dict[str, Any]:
    by_id_ours = _merge_chunks(ours)
    by_id_theirs = _merge_chunks(theirs)
    common = sorted(set(by_id_ours) & set(by_id_theirs))
    fields: dict[str, Counter] = {f: Counter() for f in STABLE_FIELDS + VOLATILE_FIELDS}
    examples: dict[str, list] = {}
    for pid in common:
        a, b = by_id_ours[pid], by_id_theirs[pid]
        for f in STABLE_FIELDS + VOLATILE_FIELDS:
            c = fields[f]
            va, vb = a.get(f), b.get(f)
            if _empty(va) and _empty(vb):
                c["bothEmpty"] += 1
            elif _empty(vb):
                c["onlyOurs"] += 1
            elif _empty(va):
                c["onlyApify"] += 1
            elif _same(va, vb):
                c["equal"] += 1
            else:
                c["differ"] += 1
                ex = examples.setdefault(f, [])
                if len(ex) < 3:
                    ex.append({"placeId": pid, "ours": va, "apify": vb})
    reviews = []
    for pid in common:
        ra = len(by_id_ours[pid].get("reviews") or [])
        rb = len(by_id_theirs[pid].get("reviews") or [])
        if ra or rb:
            ids_a = {r.get("reviewId") for r in by_id_ours[pid].get("reviews") or []}
            ids_b = {r.get("reviewId") for r in by_id_theirs[pid].get("reviews") or []}
            reviews.append({"placeId": pid, "title": by_id_ours[pid].get("title"), "ours": ra, "apify": rb,
                            "commonIds": len(ids_a & ids_b), "googleCount": by_id_ours[pid].get("reviewsCount")})
    stable_eq = sum(fields[f]["equal"] for f in STABLE_FIELDS)
    stable_cmp = sum(fields[f]["equal"] + fields[f]["differ"] for f in STABLE_FIELDS)
    return {
        "ours": len(by_id_ours), "apify": len(by_id_theirs), "common": len(common),
        "onlyOurs": len(set(by_id_ours) - set(by_id_theirs)), "onlyApify": len(set(by_id_theirs) - set(by_id_ours)),
        "stableFieldAgreement": round(stable_eq / stable_cmp, 4) if stable_cmp else None,
        "fields": {f: dict(c) for f, c in fields.items()}, "differExamples": examples, "reviews": reviews,
    }


def _ts(x: str | None) -> float | None:
    from datetime import datetime
    return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() if x else None


def job_metrics(store: Store) -> dict[str, Any]:
    """Cumulative volumes of the job, plus what the last run (start or resume) added.

    Rates are given only per run: dividing cumulative volumes by the last run's time (or by
    a span that includes pauses between runs) would overstate or understate them."""
    job = store.job()
    accepted = store.total_accepted()
    reviews = store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    billed = store.billing_total()
    failed = store.db.execute("SELECT kind, COUNT(*) FROM tasks WHERE status = 'failed' GROUP BY kind").fetchall()
    blocks = store.db.execute("SELECT COUNT(*) FROM events WHERE kind = 'block'").fetchone()[0]
    first = store.db.execute("SELECT MIN(created_at) FROM sessions").fetchone()[0]
    runs = []
    for row in store.db.execute("SELECT ts, data FROM events WHERE kind = 'run_start' ORDER BY id"):
        runs.append({"startedAt": row[0], **(loads(row[1]) or {})})
    ends = [r for r in store.db.execute("SELECT ts, data FROM events WHERE kind = 'run' AND message LIKE 'run ended%' "
                                        "ORDER BY id")]
    last = None
    if runs:
        start = runs[-1]
        end_ts = job["finished_at"] if job["status"] != "running" else None
        secs = (_ts(end_ts) - _ts(start["startedAt"])) if end_ts else None
        d_places = accepted - int(start.get("acceptedPlaces") or 0)
        d_reviews = reviews - int(start.get("reviews") or 0)
        d_billed = billed - float(start.get("billedUsd") or 0.0)
        last = {"startedAt": start["startedAt"], "finishedAt": end_ts, "seconds": secs,
                "addedPlaces": d_places, "addedReviews": d_reviews, "billedUsd": round(d_billed, 4),
                "placesPerMinute": round(d_places / (secs / 60), 2) if secs else None,
                "reviewsPerMinute": round(d_reviews / (secs / 60), 2) if secs else None}
    span = (_ts(job["finished_at"]) - _ts(first)) if first and job["finished_at"] and job["status"] != "running" else None
    return {"status": job["status"], "stopReason": job["stop_reason"], "runs": max(len(runs), len(ends)),
            "firstSessionToFinishSeconds": span,
            "acceptedPlaces": accepted, "reviews": reviews, "broBilledUsd": round(billed, 4),
            "usdPer1000Places": round(1000 * billed / accepted, 4) if accepted else None,
            "lastRun": last, "failedTasks": {r[0]: r[1] for r in failed}, "blocks": blocks}


def run_benchmark(args) -> int:
    from .pipeline.runner import JobRunner, load_env
    job_dir = Path(args.job_dir)
    load_env(Path(args.env) if args.env else None)
    if args.profile == "compare":
        store = Store(job_dir / "state.sqlite")
        from .pipeline.runner import stored_input
        run = parse_input(stored_input(store.job()))
        ours = list(apify_items(store, run))
        theirs = json.loads(Path(args.apify_dataset).read_text(encoding="utf-8"))
        result = {"metrics": job_metrics(store), "comparison": compare_items(ours, theirs)}
        (job_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
        print(json.dumps({k: v for k, v in result["comparison"].items() if k not in ("fields", "differExamples")},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    if args.input:
        input_path = str(args.input)
        run = load_input(input_path)
    else:
        input_path, raw = profile_input(args.profile)
        run = parse_input(raw)
    if getattr(args, "validate_only", False):
        print(json.dumps({"valid": True, "profile": args.profile, "input": input_path, "warnings": run.warnings,
                          "searchTerms": run.search_terms, "placeIds": run.direct_place_ids,
                          "reviewsLimit": run.reviews_limit, "imagesLimit": run.images_limit,
                          "bro": run.bro.model_dump()}, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.concurrency:
        run.bro.concurrency = args.concurrency
    runner = JobRunner(job_dir).lock()
    if runner.store.job() is None:
        runner.prepare(run)
    if args.concurrency:
        # the runner reads the stored input; the override must live there
        runner.store.set_concurrency(args.concurrency)
    t0 = time.time()
    summary = runner.run()
    metrics = job_metrics(runner.store)
    metrics["invocationWallSeconds"] = round(time.time() - t0, 1)
    metrics["profile"] = args.profile
    metrics["input"] = str(input_path)
    metrics["concurrency"] = run.bro.concurrency
    out = {"summary": summary, "metrics": metrics}
    if args.apify_dataset:
        theirs = json.loads(Path(args.apify_dataset).read_text(encoding="utf-8"))
        out["comparison"] = compare_items(list(apify_items(runner.store, run)), theirs)
    (job_dir / "benchmark.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    return 0
