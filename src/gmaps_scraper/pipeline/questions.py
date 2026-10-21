"""Questions and answers (``maxQuestions``): a task of its own after the place card.

The questions come from Google's local Q&A app (maps/questions.py): the place's name, its answered
questions in Google's order (at most 5 answers each, Google's cap) and the questions nobody answered,
``maxQuestions`` of each at most.  ``bro.questionsAllAnswers`` also reads every answer of the
questions that have more than the list gives.

Every round of requests (one bro command) is parsed completely before anything is stored, and then
stored with the task's progress in one transaction: a retry or ``resume`` repeats only the round that
was not stored, never re-reads a stored page.  A refused or unreadable answer fails the task (its raw
answer is kept also with ``evidence=errors``), so the worker retries it and, once its attempts are
spent, the section ends ``failed`` and ``--retry-failed`` picks it up.  A place the app does not know
(NOT_FOUND) is such a failure, never "no questions"; so is an answers page of another question.

A question or an answer is held once, by its id: one repeated on the same page or a later one is
dropped (counted in the detail) and never takes a place of ``maxQuestions``.  Answers read with
``bro.questionsAllAnswers`` are added to those already held, never replace them; a question is
``answersComplete`` only when it holds as many answers as Google counts -- a list that ends short of
the count is marked ``answersListEnded`` and kept as it is.

Section ``questionsAndAnswers``: ``done`` (count = answered questions), ``limited`` (``maxQuestions``
stopped a list that goes on), ``empty`` (the app was read and lists no question), ``failed``.  The
detail keeps the unanswered count, the questions with more answers than collected, and the name
the app gives the place (compared with the card's title: Google's names, not a parse of ours).
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..maps import pb
from ..maps.client import MapsClient
from ..maps.questions import (ANSWERS_PAGE_SIZE, PAGE_SIZE, PARSER_VERSION, QaPlaceNotFound, QaUnreadable, answers_call,
                              apify_view, list_call, parse_answers_page, parse_place_name, parse_question_page,
                              place_call, unique)
from ..store import loads
from .context import PRIORITY, RunContext
from .discovery import TaskInterrupted

SECTION = "questionsAndAnswers"
#: questions whose remaining answers are read in one bro command (bro.questionsAllAnswers)
ANSWER_BATCH = 5


def schedule_questions(ctx: RunContext, db, fid: str) -> None:
    a = ctx.run.apify
    ctx.store.set_section(fid, SECTION, "pending", db=db)
    ctx.store.add_task("questions", fid, {"fid": fid, "limit": int(a["maxQuestions"]),
                                          "allAnswers": bool(ctx.run.bro.questionsAllAnswers)},
                       priority=PRIORITY["questions"], db=db)


def _same_name(a: str | None, b: str | None) -> bool:
    return bool(a and b) and " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def handle_questions(ctx: RunContext, maps: MapsClient, task: Any) -> dict:
    store = ctx.store
    p = loads(task["payload"])
    fid, limit, all_answers = p["fid"], max(1, int(p["limit"])), bool(p.get("allAnswers"))
    progress = loads(task["progress"]) or {}
    stored = store.extra(fid, SECTION) if progress else None
    data = (loads(stored["data"]) if stored else None) or {"source": "google_local_qa", "placeName": None,
                                                           "questions": []}
    state = {"identity": False, "answered": {"token": None, "done": False}, "unanswered": {"token": None, "done": False},
             "answers": {}, "requests": 0, "limited": [], "repeated": 0, "repeatedAnswers": 0, "sha": None,
             **progress}
    questions: list[dict] = data["questions"]

    def remaining(kind: str) -> int:
        return limit - sum(1 for q in questions if q["answered"] == (kind == "answered"))

    # Apify reads questions in English whatever the run's language (core-parity acceptance 2026-10-14:
    # a Japanese shrine's questions as "(Translated by Google) …(Original) …", relative dates in English);
    # rc27 jobs keep the run's language
    hl = "en" if ctx.run.bro.core_contract else ctx.hl
    while True:
        if ctx.should_stop():
            raise TaskInterrupted(ctx.stop_reason or "stop")
        # (kind, question id, the token asked with, (path, body))
        calls: list[tuple[str, str | None, str | None, tuple[str, str]]] = []
        maps.reqid += 1000
        if not state["identity"]:
            calls.append(("place", None, None, place_call(fid, hl=hl, reqid=maps.reqid)))
        for kind in ("answered", "unanswered"):
            if not state[kind]["done"]:
                token = state[kind]["token"]
                calls.append((kind, None, token, list_call(fid, token, size=min(PAGE_SIZE, remaining(kind)),
                                                           answered=kind == "answered", hl=hl,
                                                           reqid=maps.reqid + len(calls))))
        if not calls and all_answers:
            todo = [q for q in questions if q["answered"] and not q["answersComplete"]
                    and not q.get("answersListEnded")][:ANSWER_BATCH]
            calls = [("answers", q["questionId"], state["answers"].get(q["questionId"]),
                      answers_call(fid, q["questionId"], state["answers"].get(q["questionId"]), size=ANSWERS_PAGE_SIZE,
                                   hl=hl, reqid=maps.reqid + i))
                     for i, q in enumerate(todo)]
        if not calls:
            break
        responses = maps.post_many([c for *_, c in calls])
        parsed = []
        failure: Exception | None = None
        for (kind, qid, asked, (path, _)), resp in zip(calls, responses):
            try:
                if resp.status != 200 or resp.error:
                    raise QaUnreadable(f"Q&A {kind} request answered HTTP {resp.status} {resp.error or ''}".strip())
                if kind == "place":
                    result = parse_place_name(resp.text)
                elif kind == "answers":
                    result = parse_answers_page(resp.text)
                    nxt = result.next_token
                    if result.question_id != qid:
                        raise QaUnreadable(f"answers page of question {result.question_id}, not of {qid}")
                else:
                    result = parse_question_page(resp.text, answered=kind == "answered")
                    nxt = result.next_token
                if kind != "place" and asked and nxt == asked:
                    raise QaUnreadable(f"Q&A {kind} list did not advance (the same token came back)")
                parsed.append(result)
                ctx.evidence(kind="questions", url=path, status=resp.status, text=resp.text, task_id=task["id"],
                             note=f"{kind} {qid or ''}".strip())
            except (QaPlaceNotFound, QaUnreadable) as exc:
                ctx.evidence(kind="questions", url=path, status=resp.status, text=resp.text, task_id=task["id"],
                             note=f"{kind}: {exc}"[:300], failed=True)
                failure = failure or exc
        if failure is not None:
            raise failure      # nothing of this round is stored: a retry repeats it
        for (kind, qid, _, _), resp, result in zip(calls, responses, parsed):
            state["requests"] += 1
            if kind == "place":
                data["placeName"], state["identity"] = result, True
            elif kind == "answers":
                page = result
                q = next(x for x in questions if x["questionId"] == qid)
                # added to the answers held, never replacing them (the first page lists again the up to 5
                # the question list gave: expected, not counted as repeats)
                merged = unique(q["answers"] + page.answers, "answerId")[0]
                state["repeatedAnswers"] += page.repeated
                q["answers"], q["answersCount"] = merged, page.count
                state["answers"][qid] = page.next_token
                if not page.next_token:
                    state["answers"].pop(qid, None)
                    q["answersListEnded"] = True
                    q["answersComplete"] = len(merged) >= page.count
            else:
                page = result
                held = [{"questionId": q["questionId"]} for q in questions]
                fresh = unique(held + page.questions, "questionId")[0][len(held):]
                # Google listed it again, on this page or an earlier one
                state["repeated"] += len(page.questions) - len(fresh)
                state["repeatedAnswers"] += sum(q.get("repeatedAnswers", 0) for q in fresh)
                room = remaining(kind)
                questions.extend(fresh[:room])
                state["sha"] = hashlib.sha256(resp.text.encode("utf-8")).hexdigest()
                more = bool(page.next_token) or len(fresh) > room
                state[kind]["token"] = page.next_token
                if not page.next_token or remaining(kind) <= 0:
                    state[kind]["done"] = True
                    if more and kind not in state["limited"]:
                        state["limited"].append(kind)
        with store.tx() as db:
            store.put_extra(db, fid, SECTION, data, source_url=pb.qa_page_url(fid, hl=hl),
                            parser_version=PARSER_VERSION, evidence_sha=state["sha"])
            store.save_progress(task["id"], state, db)
            store.set_section(fid, SECTION, "running", count=sum(1 for q in questions if q["answered"]), db=db)
    return _finish(ctx, fid, data, state, limit)


def _finish(ctx: RunContext, fid: str, data: dict, state: dict, limit: int) -> dict:
    store = ctx.store
    questions = data["questions"]
    answered = sum(1 for q in questions if q["answered"])
    row = store.place(fid)
    title = ((loads(row["details"]) or loads(row["search_data"]) or {}).get("title")) if row else None
    detail = {"answered": answered, "unanswered": len(questions) - answered,
              "answersTruncated": sum(1 for q in questions if q["answered"] and not q["answersComplete"]),
              "placeName": data.get("placeName"), "cardTitle": title,
              "nameMatchesCard": _same_name(data.get("placeName"), title), "requests": state["requests"],
              "repeatedQuestions": state.get("repeated", 0), "repeatedAnswers": state.get("repeatedAnswers", 0),
              "answersListShort": sum(1 for q in questions if q.get("answersListEnded") and not q["answersComplete"])}
    if state["limited"]:
        status, reason = "limited", f"maxQuestions={limit}"
    elif not questions:
        status, reason = "empty", "Google's Q&A app was read and lists no question for this place"
    else:
        status, reason = "done", None
    store.set_section(fid, SECTION, status, reason=reason, count=answered, detail=detail)
    return {"fid": fid, "status": status, **detail}


def questions_export(store, fid: str) -> list[dict] | None:
    """Apify's ``questionsAndAnswers`` for a place (None -- left out -- when the read failed or is pending)."""
    sec = store.section(fid, SECTION)
    if sec is None or sec["status"] not in ("done", "limited", "empty"):
        return None
    row = store.extra(fid, SECTION)
    data = loads(row["data"]) if row else None
    return apify_view((data or {}).get("questions") or [])
