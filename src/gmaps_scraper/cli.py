"""Command line: validate, run, status, resume, stop, export, benchmark."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .input_model import InputError, load_input
from .report import render_text, status, to_json
from .store import Store


def _setup_logging(job_dir: Path | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if job_dir is not None:
        job_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(job_dir / "run.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_validate(args) -> int:
    try:
        run = load_input(args.input)
    except InputError as exc:
        print(json.dumps({"valid": False, "problems": exc.problems}, ensure_ascii=False, indent=2))
        return 2
    plan = {"searchTerms": run.search_terms, "startUrls": [s.__dict__ for s in run.start_urls],
            "placeIds": run.direct_place_ids, "locationMode": run.location_mode, "allPlaces": run.all_places_mode,
            "categoryFilter": run.category_filter, "detailsEnabled": run.details_enabled,
            "reviewsLimit": run.reviews_limit, "imagesLimit": run.images_limit, "legacy": run.legacy,
            "bro": run.bro.model_dump(), "coverage": run.bro.effective_coverage}
    print(json.dumps({"valid": True, "warnings": run.warnings, "plan": plan}, ensure_ascii=False, indent=2, default=str))
    return 0


def _progress_printer(interval: float = 30.0):
    last = [0.0]

    def show(ctx) -> None:
        if time.monotonic() - last[0] < interval:
            return
        last[0] = time.monotonic()
        counts = ctx.store.task_counts()
        accepted = ctx.store.total_accepted()
        summary = " | ".join(f"{k}: " + ",".join(f"{s}={n}" for s, n in sorted(v.items())) for k, v in sorted(counts.items()))
        print(f"[{time.strftime('%H:%M:%S')}] accepted {accepted} | {summary}", file=sys.stderr, flush=True)
    return show


def _finish(job_dir: Path, store: Store, summary: dict, export: str) -> None:
    from .export.apify import apify_items, write_json
    from .export.native import export_native
    from .input_model import parse_input
    from .pipeline.runner import stored_input
    # a job's own pinned settings; one created before a setting keeps the value it ran with (rc27 contract)
    run = parse_input(stored_input(store.job()))
    out = {}
    if export in ("apify", "all"):
        out["apify"] = write_json(apify_items(store, run), job_dir / "dataset.json")
    if export in ("native", "all"):
        out["native"] = export_native(store, job_dir / "native")
    report = status(store)
    (job_dir / "report.json").write_text(to_json(report), encoding="utf-8")
    print(render_text(report))
    print(json.dumps({"run": summary, "exports": out}, ensure_ascii=False, indent=2, default=str))


def cmd_run(args) -> int:
    from .pipeline.runner import JobRunner, load_env
    job_dir = Path(args.job_dir)
    _setup_logging(job_dir, args.verbose)
    load_env(Path(args.env) if args.env else None)
    try:
        run = load_input(args.input)
    except InputError as exc:
        print(json.dumps({"valid": False, "problems": exc.problems}, ensure_ascii=False, indent=2))
        return 2
    runner = JobRunner(job_dir).lock()
    try:
        plan = runner.prepare(run)
    except Exception as exc:  # noqa: BLE001 - location / planning errors are reported, not raised
        candidates = getattr(exc, "candidates", None)
        print(json.dumps({"planned": False, "error": str(exc), "candidates": candidates}, ensure_ascii=False, indent=2))
        runner.store.set_job(status="failed", stop_reason=f"plan: {exc}")
        return 3
    print(json.dumps({"planned": True, "tasks": plan.tasks, "warnings": plan.warnings,
                      "territories": plan.territories}, ensure_ascii=False, indent=2, default=str), file=sys.stderr)
    summary = runner.run(on_progress=_progress_printer())
    _finish(job_dir, runner.store, summary, args.export)
    return 0 if summary["status"] in ("completed", "stopped") else 1


def cmd_resume(args) -> int:
    from .pipeline.runner import JobRunner, load_env
    job_dir = Path(args.job_dir)
    _setup_logging(job_dir, args.verbose)
    load_env(Path(args.env) if args.env else None)
    runner = JobRunner(job_dir).lock()
    job = runner.store.job()
    if job is None:
        print(f"no job in {job_dir}")
        return 2
    if job["status"] == "created":
        from .input_model import parse_input
        from .pipeline.runner import stored_input
        runner.prepare(parse_input(stored_input(job)))
    if args.concurrency:
        runner.store.set_concurrency(args.concurrency)
    if args.retry_failed:
        n = runner.store.retry_failed(args.retry_failed)
        print(f"{n} failed task(s) queued again", file=sys.stderr)
    summary = runner.run(on_progress=_progress_printer())
    _finish(job_dir, runner.store, summary, args.export)
    return 0 if summary["status"] in ("completed", "stopped") else 1


def cmd_reconcile(args) -> int:
    """Sync a job whose process died with bro: close sessions, record billing, fix the status."""
    from .pipeline.runner import JobRunner, load_env
    job_dir = Path(args.job_dir)
    load_env(Path(args.env) if args.env else None)
    runner = JobRunner(job_dir)
    if runner.store.job() is None:
        print(f"no job in {job_dir}")
        return 2
    runner.lock()
    try:
        out = runner.reconcile()
    finally:
        runner.job_lock.release()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_status(args) -> int:
    store = Store(Path(args.job_dir) / "state.sqlite")
    report = status(store)
    print(to_json(report) if args.json else render_text(report))
    return 0


def cmd_stop(args) -> int:
    store = Store(Path(args.job_dir) / "state.sqlite")
    if store.job() is None:
        print("no job")
        return 2
    store.request_stop(args.reason)
    print("stop requested; workers finish their current command and close their sessions")
    return 0


def cmd_export(args) -> int:
    from .export.apify import apify_items, write_json
    from .export.native import export_csv, export_native, native_places, write_jsonl
    from .input_model import parse_input
    from .store import loads
    store = Store(Path(args.job_dir) / "state.sqlite")
    job = store.job()
    if job is None:
        print("no job")
        return 2
    from .pipeline.runner import stored_input
    run = parse_input(stored_input(job))
    out = Path(args.out)
    if args.format == "apify":
        if args.csv:
            import csv
            items = list(apify_items(store, run))
            keys: list[str] = []
            for it in items:
                keys += [k for k in it if k not in keys and k not in ("reviews", "images")]
            with open(out, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for it in items:
                    w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in it.items()})
            print(f"{len(items)} rows -> {out}")
        else:
            n = write_json(apify_items(store, run), out, jsonl=args.jsonl)
            print(f"{n} items -> {out}")
    else:
        if args.csv:
            print(json.dumps(export_csv(store, out)))
        elif args.jsonl and out.suffix:
            print(write_jsonl(native_places(store), out), "places ->", out)
        else:
            print(json.dumps(export_native(store, out)))
    return 0


def cmd_benchmark(args) -> int:
    from .benchmark import run_benchmark
    if not args.validate_only:
        _setup_logging(Path(args.job_dir), args.verbose)
    return run_benchmark(args)


def main(argv: list[str] | None = None) -> int:
    # Redirected Windows streams may default to cp1251. Places and reviews
    # contain arbitrary Unicode; CLI output uses the same UTF-8 as exports.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="gmaps-scraper", description=f"Google Maps scraper on bro ({__version__})")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="validate an input file and print the normalised plan")
    p.add_argument("input")
    p.set_defaults(fn=cmd_validate)

    for name, fn, help_text in (
        ("run", cmd_run, "start a new scraping job from an input file and export results"),
        ("resume", cmd_resume, "resume an interrupted or stopped job from its SQLite state"),
    ):
        p = sub.add_parser(name, help=help_text)
        if name == "run":
            p.add_argument("input")
        else:
            p.add_argument("--retry-failed", nargs="?", const="all", default=None,
                           help="queue failed tasks again (all, or a kind such as reviews)")
            p.add_argument("--concurrency", type=int, help="number of bro sessions from now on")
        p.add_argument("--job-dir", required=True)
        p.add_argument("--export", choices=["apify", "native", "all", "none"], default="all")
        p.add_argument("--env", help=".env file with BRO_API_KEY")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("reconcile", help="sync a job left by a dead process with bro (no collection)")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--env")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("status", help="display job progress, section coverage, review gaps, and billing")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("stop", help="request a graceful stop of an active job")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--reason", default="stop_requested")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("export", help="re-export data from state.sqlite into Apify or Native formats")
    p.add_argument("--job-dir", required=True)
    p.add_argument("--format", choices=["apify", "native"], default="apify")
    p.add_argument("--out", required=True)
    p.add_argument("--jsonl", action="store_true")
    p.add_argument("--csv", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("benchmark", help="run a measurement profile and/or compare with an Apify dataset")
    p.add_argument("profile", choices=["discovery", "deep", "compare"])
    p.add_argument("--job-dir", required=True)
    p.add_argument("--input", help="input file (default: the profile's standard input shipped in the package)")
    p.add_argument("--validate-only", action="store_true",
                   help="read and validate the profile's input, print it, and exit (no session, no run)")
    p.add_argument("--apify-dataset", help="Apify dataset JSON to compare against")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--env")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_benchmark)

    args = ap.parse_args(argv)
    from .pipeline.runner import JobBusy
    try:
        return int(args.fn(args) or 0)
    except JobBusy as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
