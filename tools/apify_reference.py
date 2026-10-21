"""Bounded Apify reference runs of compass/crawler-google-places (dev/benchmark tool).

Not part of the shipped package. Every run is reserved in
``verification/apify-ledger.json`` before it starts and is started with a
server-side charge cap (``maxTotalChargeUsd``).

Outcomes are recorded only when they are known:

* a start answered 4xx was refused by Apify (no run): its cap is released;
* a start answered 5xx, or not answered (timeout, connection), may still have created a run: the
  entry stays ``start unknown`` with its cap reserved, and no new run starts until ``reconcile``
  has looked it up in the actor's run list (the found run is then waited for, or the entry is
  closed as not started);
* ``wait`` polls until a terminal status (SUCCEEDED, FAILED, ABORTED, TIMED-OUT); ABORTING and
  TIMING-OUT are transitional. A terminal run without ``usageTotalUsd`` keeps ``finalUsd`` null
  (the cap stays counted) instead of recording zero.

Check and reservation happen under a lock file next to the ledger.

    python tools/apify_reference.py run INPUT.json --purpose details --cap 0.8 --estimate 0.5 --label refs-1
    python tools/apify_reference.py wait RUN_ID
    python tools/apify_reference.py reconcile LABEL
    python tools/apify_reference.py ledger
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "verification" / "apify-ledger.json"
OUT = ROOT / "verification" / "apify"
ACTOR = "compass~crawler-google-places"
BUILD = "0.14.761"
BUDGET = {"total": 10.0, "search": 4.0, "details": 4.0, "reserve": 2.0}
FEATURES = {"limit": 1.50, "untouchable": 5.00, "subs": {"refs": 0.40, "filter": 0.70, "recheck": 0.40}}
FINAL = {"limit": 5.00, "subs": {"discovery": 2.00, "details": 2.00, "recheck": 1.00}}
CORE = {"limit": 10.00, "subs": {"partner": 2.00, "sorts": 1.50, "targeted": 1.50, "final": 5.00},
        "research": ("partner", "sorts", "targeted"), "margin": 1.25, "prefix": "core-",
        "absolute": {"baselineUsd": 2.3084, "remainingUsd": 8.0, "since": "2026-10-14"}}
RC34 = {"prefix": "core-rc34-", "limit": 7.6916, "groups": {"full": 2.50, "search": 2.00, "special": 1.00, "recheck": 1.00},
        "reserve": 1.1916}
ACTIVE_AUTHORIZATION = None
TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT")
UNKNOWN = "start unknown"
API = "https://api.apify.com/v2"


def token() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APIFY_API_KEY="):
                os.environ.setdefault("APIFY_API_KEY", line.split("=", 1)[1].strip())
    return os.environ["APIFY_API_KEY"]


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"budget": BUDGET, "entries": []}


def save_ledger(ledger: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def committed(ledger: dict, purpose: str | None = None) -> float:
    total = 0.0
    for e in ledger["entries"]:
        if purpose and e["purpose"] != purpose:
            continue
        total += e["finalUsd"] if e.get("finalUsd") is not None else e["capUsd"]
    return total


def event_prices() -> dict:
    """The actor's current price per event (the account's tier) and the pricing's start date."""
    headers = {"Authorization": f"Bearer {token()}"}
    act = requests.get(f"{API}/acts/{ACTOR}", headers=headers, timeout=60).json()["data"]
    tier = None
    try:
        me = requests.get(f"{API}/users/me", headers=headers, timeout=60).json()["data"]
        tier = {"FREE": "FREE", "STARTER": "BRONZE", "SCALE": "SILVER", "BUSINESS": "GOLD"}.get((me.get("plan") or {}).get("id"))
    except Exception:  # noqa: BLE001 - the tier is informative
        pass
    info = (act.get("pricingInfos") or [{}])[-1]
    out = {"pricingStartedAt": info.get("startedAt"), "tier": tier, "minimalMaxTotalChargeUsd": info.get("minimalMaxTotalChargeUsd"),
           "latestBuild": ((act.get("taggedBuilds") or {}).get("latest") or {}).get("buildNumber"), "events": {}}
    for name, ev in ((info.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}).items():
        tiers = ev.get("eventTieredPricingUsd") or {}
        out["events"][name] = (tiers.get(tier) or {}).get("tieredEventPriceUsd") if tier in tiers else ev.get("eventPriceUsd")
    return out


@contextlib.contextmanager
def ledger_lock(timeout: float = 30.0):
    """An exclusive lock file next to the ledger: check and reservation are one step."""
    lock = LEDGER.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                sys.exit(f"refused: the ledger is locked ({lock}); remove it only if no other command is active")
            time.sleep(0.5)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def final_check(ledger: dict, cap: float, estimate: float, sub: str | None) -> None:
    spent = committed(ledger, "final_comparison")
    if spent + cap > FINAL["limit"] + 1e-9:
        sys.exit(f"refused: final_comparison runs would exceed ${FINAL['limit']} (committed {spent:.4f} + cap {cap})")
    if sub:
        used = sum((e["finalUsd"] if e.get("finalUsd") is not None else e["capUsd"]) for e in ledger["entries"]
                   if e.get("purpose") == "final_comparison" and e.get("sub") == sub)
        if used + estimate > FINAL["subs"][sub] + 1e-9:
            print(f"note: sub-allocation {sub} (${FINAL['subs'][sub]:.2f}) is exceeded by the estimate; "
                  f"it uses amounts left by the other parts (the ${FINAL['limit']} limit holds)", file=sys.stderr)


def core_check(ledger: dict, cap: float, estimate: float, sub: str | None, label: str) -> None:
    """core_parity: $10 of its own; research sub-limits; the final $5 untouched by research."""
    if sub not in CORE["subs"]:
        sys.exit(f"refused: core_parity needs --sub one of {list(CORE['subs'])}")
    if not label.startswith(CORE["prefix"]):
        sys.exit(f"refused: core_parity labels start with {CORE['prefix']!r}")
    if cap + 1e-9 < estimate * CORE["margin"]:
        sys.exit(f"refused: cap {cap} does not cover the estimate {estimate} with a 25% margin")
    charge = lambda e: e["finalUsd"] if e.get("finalUsd") is not None else e["capUsd"]
    mine = [e for e in ledger["entries"] if e.get("purpose") == "core_parity"]
    # the user's absolute ceiling (2026-10-14): at most $8 of new Apify spend after the stage's $2.3084,
    # for every remaining test up to the release, never exceeded
    after = sum(charge(e) for e in mine) - CORE["absolute"]["baselineUsd"]
    if after + cap > CORE["absolute"]["remainingUsd"] + 1e-9:
        sys.exit(f"refused: the absolute ceiling of ${CORE['absolute']['remainingUsd']} after the baseline would be exceeded "
                 f"(spent since {after:.4f} + cap {cap})")
    if sum(map(charge, mine)) + cap > CORE["limit"] + 1e-9:
        sys.exit(f"refused: core_parity runs would exceed ${CORE['limit']} (committed {sum(map(charge, mine)):.4f} + cap {cap})")
    if label.startswith(RC34["prefix"]):
        group = label[len(RC34["prefix"]):].split("-")[0]
        if group not in RC34["groups"]:
            sys.exit(f"refused: rc34 labels are {RC34['prefix']}<{'|'.join(RC34['groups'])}>-...")
        stage = [e for e in mine if e.get("label", "").startswith(RC34["prefix"])]
        if sum(map(charge, stage)) + cap > RC34["limit"] + 1e-9:
            sys.exit(f"refused: the rc34 stage limit ${RC34['limit']} would be exceeded (committed "
                     f"{sum(map(charge, stage)):.4f} + cap {cap})")
        used = {g: sum(charge(e) for e in stage if e["label"][len(RC34["prefix"]):].split("-")[0] == g) for g in RC34["groups"]}
        used[group] += cap
        over = sum(max(0.0, used[g] - RC34["groups"][g]) for g in RC34["groups"])
        if over > RC34["reserve"] + 1e-9:
            sys.exit(f"refused: group {group} (${RC34['groups'][group]:.2f}) would take {over:.4f} from the reserve "
                     f"(${RC34['reserve']})")
    if sub in CORE["research"]:
        used = sum(charge(e) for e in mine if e.get("sub") == sub)
        if used + cap > CORE["subs"][sub] + 1e-9:
            sys.exit(f"refused: research {sub} (${CORE['subs'][sub]:.2f}) would be exceeded (committed {used:.4f} + cap {cap})")
    else:
        research = sum(charge(e) for e in mine if e.get("sub") in CORE["research"])
        used = sum(charge(e) for e in mine if e.get("sub") == "final")
        # the final reserve plus research amounts left over (research is counted at its commitment)
        allowed = CORE["limit"] - research
        if used + cap > allowed + 1e-9:
            sys.exit(f"refused: the final comparison would exceed ${allowed:.4f} (committed {used:.4f} + cap {cap})")


def stage_check(ledger: dict, cap: float, sub: str | None, label: str) -> None:
    """The follow-up reuses remaining authorisation and protects its final reserve."""
    auth = ACTIVE_AUTHORIZATION or {}
    prefix = auth.get("stagePrefix")
    if not prefix:
        return
    if not label.startswith(prefix):
        sys.exit("refused: label must identify the authorised follow-up stage")
    entries = [e for e in ledger["entries"] if e.get("label", "").startswith(prefix)]
    charge = lambda e: e["finalUsd"] if e.get("finalUsd") is not None else e["capUsd"]
    if sum(map(charge, entries)) + cap > float(auth["stageCapUsd"]) + 1e-9:
        sys.exit("refused: follow-up stage budget exceeded")
    if sub != "details" and sum(charge(e) for e in entries if e.get("sub") != "details") + cap > float(auth["researchCapUsd"]) + 1e-9:
        sys.exit("refused: research would consume the protected final reserve")
    if sub == "details" and sum(charge(e) for e in entries if e.get("sub") == "details") + cap > float(auth["finalReserveUsd"]) + 1e-9:
        sys.exit("refused: final verification budget exceeded")


def features_check(ledger: dict, cap: float, estimate: float, sub: str | None) -> None:
    spent = committed(ledger, "features")
    if spent + cap > FEATURES["limit"] + 1e-9:
        sys.exit(f"refused: features runs would exceed ${FEATURES['limit']} (committed {spent:.4f} + cap {cap})")
    if committed(ledger) + cap > BUDGET["total"] - FEATURES["untouchable"] + 1e-9:
        sys.exit(f"refused: the untouchable ${FEATURES['untouchable']} would be touched")
    if sub:
        order = list(FEATURES["subs"])
        allowed = sum(FEATURES["subs"][s] for s in order[:order.index(sub) + 1])  # unused amounts carry over
        used = sum((e["finalUsd"] if e.get("finalUsd") is not None else e["capUsd"]) for e in ledger["entries"]
                   if e.get("purpose") == "features" and e.get("sub") in order[:order.index(sub) + 1])
        if used + estimate > allowed + 1e-9:
            sys.exit(f"refused: sub-allocation {sub} (with carry-over ${allowed:.2f}) would be exceeded by the estimate")


def _update_entry(label: str, reserved_at: str, **fields) -> None:
    with ledger_lock():
        ledger = load_ledger()
        if ACTIVE_AUTHORIZATION is not None:
            ledger["budget"] = dict(BUDGET)
            ledger["authorization"] = ACTIVE_AUTHORIZATION
        for e in ledger["entries"]:
            if e.get("label") == label and e.get("reservedAt") == reserved_at:
                e.update(fields)
        save_ledger(ledger)


def cmd_run(args) -> None:
    inp = json.loads(Path(args.input).read_text(encoding="utf-8"))
    cap = float(args.cap)
    with ledger_lock():
        ledger = load_ledger()
        open_starts = [e["label"] for e in ledger["entries"] if str(e.get("status", "")).startswith(UNKNOWN)]
        if open_starts:
            sys.exit(f"refused: start outcome unknown for {open_starts}; run `reconcile` first")
        unsettled = [e.get("label") for e in ledger["entries"] if e.get("runId") and e.get("finalUsd") is None]
        if unsettled:
            sys.exit(f"refused: run or charge not settled for {unsettled}; run `reconcile` first")
        if args.purpose != "core_parity" and committed(ledger) + cap > BUDGET["total"] + 1e-9:
            sys.exit(f"refused: total budget would exceed ${BUDGET['total']} (committed {committed(ledger):.3f} + cap {cap})")
        if args.purpose == "core_parity":
            core_check(ledger, cap, float(args.estimate), args.sub, args.label)
        elif args.purpose == "features":
            features_check(ledger, cap, float(args.estimate), args.sub)
        elif args.purpose == "final_comparison":
            final_check(ledger, cap, float(args.estimate), args.sub)
            stage_check(ledger, cap, args.sub, args.label)
        else:
            limit = BUDGET.get(args.purpose, 0) + max(0.0, BUDGET["reserve"] - committed(ledger, "reserve"))
            if committed(ledger, args.purpose) + cap > limit + 1e-9:
                sys.exit(f"refused: {args.purpose} budget would exceed ${limit}")
        prices = event_prices()
        minimal = prices.get("minimalMaxTotalChargeUsd")
        if minimal is not None and cap + 1e-9 < float(minimal):
            sys.exit(f"refused: cap {cap} is below the actor's minimal maxTotalChargeUsd {minimal}")
        entry = {"label": args.label, "purpose": args.purpose, "sub": args.sub, "estimateUsd": float(args.estimate),
                 "capUsd": cap, "reservedAt": datetime.now(timezone.utc).isoformat(), "input": args.input, "runId": None,
                 "finalUsd": None, "status": "reserved", "build": args.build, "eventPrices": prices}
        entry["budgetLimits"] = {"totalUsd": BUDGET["total"], "finalComparisonUsd": FINAL["limit"]}
        if args.purpose == "core_parity":
            entry["budgetLimits"] = {"coreParityUsd": CORE["limit"], "subs": CORE["subs"]}
        ledger["entries"].append(entry)
        save_ledger(ledger)
    label, reserved_at = entry["label"], entry["reservedAt"]
    params = {"build": args.build, "maxTotalChargeUsd": cap, "memory": args.memory}
    try:
        resp = requests.post(f"{API}/acts/{ACTOR}/runs", params=params, json=inp,
                             headers={"Authorization": f"Bearer {token()}"}, timeout=60)
    except requests.RequestException as exc:
        # no answer: the run may have been created -- the cap stays reserved until reconciled
        _update_entry(label, reserved_at, status=f"{UNKNOWN}: {type(exc).__name__}")
        sys.exit(f"start outcome unknown ({exc}); the cap stays reserved -- run `reconcile {label}`")
    if resp.status_code >= 500:
        _update_entry(label, reserved_at, status=f"{UNKNOWN}: HTTP {resp.status_code}")
        sys.exit(f"start outcome unknown: HTTP {resp.status_code} {resp.text[:300]}; the cap stays reserved -- "
                 f"run `reconcile {label}`")
    if resp.status_code >= 300:
        # a 4xx is Apify refusing the start: no run exists
        _update_entry(label, reserved_at, status=f"start refused {resp.status_code}", finalUsd=0.0)
        sys.exit(f"start refused: {resp.status_code} {resp.text[:500]}")
    run = resp.json()["data"]
    _update_entry(label, reserved_at, runId=run["id"], status=run["status"], datasetId=run.get("defaultDatasetId"))
    print("started", run["id"], run["status"], "dataset", run.get("defaultDatasetId"))
    if args.wait:
        wait(run["id"])


def _get_run(run_id: str, headers: dict) -> dict | None:
    try:
        return requests.get(f"{API}/actor-runs/{run_id}", headers=headers, timeout=60).json()["data"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        print(time.strftime("%H:%M:%S"), "run status not read:", exc, file=sys.stderr)
        return None


def _expected_event_charge(run: dict, ledger: dict) -> float | None:
    """A non-null usageTotalUsd can lag the terminal event counts (seen live 2026-10-09).
    Prices are those reserved for this run/account. Unknown prices keep this check unavailable.
    """
    entry = next((e for e in ledger["entries"] if e.get("runId") == run.get("id")), None)
    prices = ((entry or {}).get("eventPrices") or {}).get("events")
    # At the server-side cap an emitted event can remain uncharged. The API
    # reports the actually billed subset separately; do not reserve forever.
    counts = run.get("accountedChargedEventCounts")
    if not isinstance(counts, dict):
        counts = run.get("chargedEventCounts")
    if not isinstance(prices, dict) or not isinstance(counts, dict):
        return None
    if any(count and not isinstance(prices.get(name), (int, float)) for name, count in counts.items()):
        return None
    return sum(float(count) * float(prices.get(name) or 0) for name, count in counts.items())


def wait(run_id: str, *, usage_tries: int = 6) -> None:
    """Poll until a terminal status; record the final charge only when Apify reports it."""
    headers = {"Authorization": f"Bearer {token()}"}
    while True:
        run = _get_run(run_id, headers)
        if run is not None and run.get("status") in TERMINAL:
            break
        if run is not None:
            print(time.strftime("%H:%M:%S"), run.get("status"), "usage", run.get("usageTotalUsd"),
                  "charged", (run.get("chargedEventCounts") or {}))
        time.sleep(20)
    # The terminal status alone does not settle billing. Require two stable snapshots,
    # and do not release a reservation while the billed amount trails known charged events.
    settled = False
    expected = None
    for _ in range(usage_tries):
        previous = run
        time.sleep(10)
        current = _get_run(run_id, headers)
        if current is None:
            continue
        run = current
        expected = _expected_event_charge(run, load_ledger())
        usage = run.get("usageTotalUsd")
        stable = (usage is not None and usage == previous.get("usageTotalUsd")
                  and run.get("chargedEventCounts") == previous.get("chargedEventCounts"))
        if stable and (expected is None or float(usage) + 1e-7 >= expected):
            settled = True
            break
    usage = run.get("usageTotalUsd") if settled else None
    with ledger_lock():
        ledger = load_ledger()
        for e in ledger["entries"]:
            if e.get("runId") == run_id:
                e["status"] = run["status"] if usage is not None else f"{run['status']} (charge not settled yet)"
                e["finalUsd"] = float(usage) if usage is not None else None
                e["lastReportedUsd"] = run.get("usageTotalUsd")
                e["expectedEventUsd"] = expected
                e["billingCheckedAt"] = datetime.now(timezone.utc).isoformat()
                e["chargedEventCounts"] = run.get("chargedEventCounts")
                e["startedAt"], e["finishedAt"] = run.get("startedAt"), run.get("finishedAt")
                e["buildNumber"] = run.get("buildNumber")
        save_ledger(ledger)
    OUT.mkdir(parents=True, exist_ok=True)
    items = requests.get(f"{API}/datasets/{run['defaultDatasetId']}/items", params={"clean": "true", "format": "json"},
                         headers=headers, timeout=300).json()
    (OUT / f"{run_id}.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / f"{run_id}.run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print("finished", run["status"], "usd", run.get("usageTotalUsd"), "items", len(items), "build", run.get("buildNumber"))


def reconcile(label: str) -> None:
    """Settle an entry whose start outcome is unknown: find its run in the actor's run list."""
    headers = {"Authorization": f"Bearer {token()}"}
    ledger = load_ledger()
    entry = next((e for e in reversed(ledger["entries"]) if e.get("label") == label), None)
    if entry is None:
        sys.exit(f"no ledger entry {label}")
    if entry.get("runId"):
        wait(entry["runId"])
        return
    runs = requests.get(f"{API}/acts/{ACTOR}/runs", params={"desc": "true", "limit": 50}, headers=headers,
                        timeout=60).json()["data"]["items"]
    known = {e.get("runId") for e in ledger["entries"] if e.get("runId")}
    since = datetime.fromisoformat(entry["reservedAt"]).timestamp() - 5
    cand = [r for r in runs if r["id"] not in known
            and datetime.fromisoformat(r["startedAt"].replace("Z", "+00:00")).timestamp() >= since]
    if len(cand) > 1:
        sys.exit(f"refused: {len(cand)} unrecorded runs started after the reservation: {[r['id'] for r in cand]}")
    if cand:
        _update_entry(label, entry["reservedAt"], runId=cand[0]["id"], status=cand[0]["status"],
                      datasetId=cand[0].get("defaultDatasetId"), reconciledAt=datetime.now(timezone.utc).isoformat())
        print("found", cand[0]["id"], cand[0]["status"])
        wait(cand[0]["id"])
    else:
        _update_entry(label, entry["reservedAt"], status="not started (no run in the actor's run list)", finalUsd=0.0,
                      reconciledAt=datetime.now(timezone.utc).isoformat())
        print("no run was started for", label, "- cap released")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="op", required=True)
    r = sub.add_parser("run")
    r.add_argument("input")
    r.add_argument("--purpose", choices=["search", "details", "reserve", "features", "final_comparison", "core_parity"],
                   required=True)
    r.add_argument("--sub", choices=list(FEATURES["subs"]) + list(FINAL["subs"]) + list(CORE["subs"]))
    r.add_argument("--build", default=BUILD)
    r.add_argument("--cap", required=True)
    r.add_argument("--estimate", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--memory", type=int, default=4096)
    r.add_argument("--wait", action="store_true")
    r.add_argument("--budget-file", help="Explicit user authorization to increase the original budget (JSON)")
    w = sub.add_parser("wait")
    w.add_argument("run_id")
    rc = sub.add_parser("reconcile")
    rc.add_argument("label")
    sub.add_parser("ledger")
    a = ap.parse_args()
    if a.op == "run" and a.budget_file:
        global ACTIVE_AUTHORIZATION
        authorization = json.loads(Path(a.budget_file).read_text(encoding="utf-8"))
        extra = authorization.get("additionalUsd")
        if not isinstance(extra, (int, float)) or isinstance(extra, bool) or not 0 <= extra <= 100:
            sys.exit("invalid explicit additional budget")
        if not authorization.get("userInstruction"):
            sys.exit("budget authorization must record the user instruction")
        BUDGET["total"] = 10.0 + extra
        FINAL["limit"] = 5.0 + extra
        ACTIVE_AUTHORIZATION = authorization
    if a.op == "run":
        cmd_run(a)
    elif a.op == "wait":
        wait(a.run_id)
    elif a.op == "reconcile":
        reconcile(a.label)
    else:
        led = load_ledger()
        print(json.dumps(led, indent=1))
        print("committed", round(committed(led), 4), {p: round(committed(led, p), 4) for p in
                                                      ("search", "details", "reserve", "features", "final_comparison", "core_parity")})


if __name__ == "__main__":
    main()
