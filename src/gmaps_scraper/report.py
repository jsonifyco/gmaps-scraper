"""Status and coverage report of a job, computed from SQLite."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .store import TERMINAL_SESSION_STATES, Store, loads


def coverage(store: Store) -> dict[str, Any]:
    cells: dict[str, Counter] = defaultdict(Counter)
    gain: dict[str, Counter] = defaultdict(Counter)
    for t in store.db.execute("SELECT status, payload, result FROM tasks WHERE kind = 'search'"):
        payload = loads(t["payload"]) or {}
        result = loads(t["result"]) or {}
        key = payload.get("searchKey", "?")
        cell = payload.get("cell") or {}
        pass_name = cell.get("pass", "viewport" if payload.get("viewport") else "main")
        if t["status"] == "done":
            reason = result.get("endReason")
            cells[key]["processed"] += 1
            if reason == "saturated_split":
                cells[key]["saturated_split"] += 1
            elif reason == "outside_tail_split":
                cells[key]["unresolved_split"] += 1
            elif reason == "saturated_min_size":
                cells[key]["saturated_unsplittable"] += 1
            elif reason == "outside_tail_min_size":
                cells[key]["unresolved_min_size"] += 1
            elif reason in ("limit", "maxCrawledPlaces"):
                cells[key]["stopped_by_limit"] += 1
            cells[key][f"reason:{reason}"] += 1
            gain[key][pass_name] += int(result.get("newAccepted") or 0)
        elif t["status"] == "failed":
            cells[key]["failed"] += 1
        else:
            cells[key]["unfinished"] += 1
    out = {}
    for key in sorted(set(cells) | set(gain)):
        accepted = store.db.execute("SELECT COUNT(*) FROM results WHERE search_key = ? AND accepted = 1",
                                    (key,)).fetchone()[0]
        seen = store.db.execute("SELECT COUNT(*) FROM results WHERE search_key = ?", (key,)).fetchone()[0]
        reasons = Counter(r[0] for r in store.db.execute(
            "SELECT filter_reason FROM results WHERE search_key = ? AND accepted = 0", (key,)))
        out[key] = {"cells": dict(cells[key]), "uniquePlacesSeen": seen, "accepted": accepted,
                    "rejected": dict(reasons), "newPlacesByPass": dict(gain[key])}
    return out


def sections(store: Store) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for r in store.db.execute("SELECT section, status, COUNT(*) FROM sections GROUP BY section, status"):
        out[r[0]][r[1]] = r[2]
    return dict(out)


def review_gaps(store: Store, limit: int = 50) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT s.fid, s.status, s.reason, s.count, s.expected, p.title FROM sections s LEFT JOIN places p ON p.fid = s.fid "
        "WHERE s.section = 'reviews' AND s.expected IS NOT NULL AND s.count < s.expected ORDER BY (s.expected - s.count) DESC "
        "LIMIT ?", (limit,)).fetchall()
    return [{"fid": r["fid"], "title": r["title"], "status": r["status"], "reason": r["reason"],
             "collected": r["count"], "googleCount": r["expected"], "gap": r["expected"] - r["count"]} for r in rows]


def billing(store: Store) -> dict[str, Any]:
    rows = store.db.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
    total = sum(float(r["total_billed"] or 0) for r in rows)
    return {"sessions": len(rows), "open": sum(1 for r in rows if r["status"] not in TERMINAL_SESSION_STATES),
            "totalBilledUsd": round(total, 4),
            "commands": sum(int(r["commands"] or 0) for r in rows),
            "busySeconds": round(sum(float(r["busy_seconds"] or 0) for r in rows), 1),
            "perSession": [{"sessionId": r["session_id"], "worker": r["worker"], "status": r["status"],
                            "billed": r["total_billed"], "options": r["options"], "error": r["error"]} for r in rows]}


def status(store: Store) -> dict[str, Any]:
    job = store.job()
    if job is None:
        return {"job": None}
    places = store.db.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    accepted = store.db.execute("SELECT COUNT(DISTINCT fid) FROM results WHERE accepted = 1").fetchone()[0]
    unverified = store.db.execute("SELECT COUNT(DISTINCT fid) FROM results WHERE filter_reason = 'unverified_location'").fetchone()[0]
    territories = [{"id": r["id"], "label": r["label"], "source": r["source"], "areaKm2": round(r["area_km2"] or 0, 2),
                    "provenance": loads(r["provenance"])} for r in store.db.execute("SELECT * FROM territories")]
    events = [dict(ts=r["ts"], level=r["level"], kind=r["kind"], message=r["message"])
              for r in store.db.execute("SELECT * FROM events WHERE level IN ('warning', 'error') ORDER BY id DESC LIMIT 20")]
    return {
        "job": {"status": job["status"], "createdAt": job["created_at"], "startedAt": job["started_at"],
                "finishedAt": job["finished_at"], "stopReason": job["stop_reason"], "runs": job["runs"],
                "versions": loads(job["versions"]), "warnings": (loads(job["normalized_json"]) or {}).get("warnings")},
        "territories": territories,
        "tasks": store.task_counts(),
        "places": {"known": places, "accepted": accepted, "unverifiedLocation": unverified,
                   "reviews": store.db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
                   "images": store.db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
                   "contacts": store.db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]},
        "coverage": coverage(store),
        "sections": sections(store),
        "reviewGaps": review_gaps(store, 20),
        "billing": billing(store),
        "recentProblems": events,
    }


def render_text(report: dict[str, Any]) -> str:
    if not report.get("job"):
        return "no job"
    j = report["job"]
    lines = [f"job: {j['status']}  runs={j['runs']}  started={j['startedAt']}  finished={j['finishedAt']}"
             + (f"  stop={j['stopReason']}" if j.get("stopReason") else "")]
    for t in report["territories"]:
        prov = t.get("provenance") or {}
        lines.append(f"territory #{t['id']}: {t['label']} ({t['source']}, {t['areaKm2']} km2"
                     + (f", {prov.get('osmId')} {prov.get('displayName', '')[:60]}" if prov.get("osmId") else "") + ")")
    p = report["places"]
    lines.append(f"places: {p['accepted']} accepted / {p['known']} known; reviews {p['reviews']}, images {p['images']}, "
                 f"contacts {p['contacts']}; unverified location {p['unverifiedLocation']}")
    lines.append("tasks: " + ", ".join(f"{k} {dict(v)}" for k, v in sorted(report["tasks"].items())))
    for key, cov in report["coverage"].items():
        lines.append(f"  {key}: accepted {cov['accepted']} of {cov['uniquePlacesSeen']} seen; cells {cov['cells']}; "
                     f"new by pass {cov['newPlacesByPass']}; rejected {cov['rejected']}")
    for name, counts in sorted(report["sections"].items()):
        lines.append(f"  section {name}: {counts}")
    for gap in report["reviewGaps"][:5]:
        lines.append(f"  reviews gap: {gap['title']}: {gap['collected']}/{gap['googleCount']} ({gap['status']}: {gap['reason']})")
    b = report["billing"]
    lines.append(f"bro: {b['sessions']} sessions ({b['open']} open), ${b['totalBilledUsd']}, {b['commands']} commands, "
                 f"{b['busySeconds']} s busy")
    for e in report["recentProblems"][:5]:
        lines.append(f"  [{e['level']}] {e['ts']} {e['kind']}: {e['message'][:160]}")
    return "\n".join(lines)


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
