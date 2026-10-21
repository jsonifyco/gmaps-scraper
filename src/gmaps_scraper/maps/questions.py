"""Questions and answers of a place, from Google's local Q&A app.

Google stopped showing Q&A on the Maps card in December 2025 (the place record's field 131 is 0 and
field 127 empty for every place), but the frame the Maps client opens for
"More questions" -- ``/local/place/qa/question/<fid>`` (Boq app ``LocalPlaceqaFrontendUi``) -- still
serves them.  Its RPCs answer unsigned POSTs from a
www.google.com page:

* ``aDelc`` ``[fid, token, page size, answers per question, null, 1]`` -- answered questions in
  Google's order, each with at most 5 answers (Google's cap, also Apify's) and its answer count;
  ``[fid, token, page size, 0, 1]`` -- questions nobody answered ("Others are asking");
  the answer is ``[[question, ...], next token]`` (an empty token ends the list) or ``[]`` (none);
* ``IQyqnf`` ``[fid, question id, token, 1, null, page size <= 20]`` -- every answer of one question,
  ``[[question, answers, answer count, null, next token]]``;
* ``dJeg2b`` ``[fid]`` -- ``[place name]``.

A fid the app does not know answers error ``[5]`` (NOT_FOUND) -- :class:`QaPlaceNotFound`, never an
empty list; any other refusal or shape is :class:`QaUnreadable`.  Only the shapes Google was seen to
send are accepted (46 live answers and the fixtures, 2026-10-13): ``[]`` for no questions, a string
token (``""`` at the end of a list), records with a string id, an integer answer count.  A cursor of
another type or a page without its records is unreadable, never the end of a list (rc21 review).

A record is ``[text, counter, ?, author, relative date, parent question id, id, ...]`` and an author
``[name, photo URL, profile URL, merchant (1/0), ?, badges]``; questions Google itself asks ("How would
you describe the atmosphere?") have no author and no date.  Dates are Google's relative texts ("8 years
ago"), as on the page and in Apify's output; a translated text keeps Google's "(Translated by Google)
... (Original) ..." form.  The counter at ``[1]`` is not shown on the page: kept as ``votes``, its
meaning not established.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import pb
from .xssi import ResponseFormatError, rpc_payload

PARSER_VERSION = "local-qa-2026.10.13b"
LIST_RPC, ANSWERS_RPC, PLACE_RPC = "aDelc", "IQyqnf", "dJeg2b"
#: answers per question in the list (Google answers at most 5 whatever is asked)
LIST_ANSWERS = 5
#: questions per request (the app asks 20; 50-200 were answered alike, 2026-10-13)
PAGE_SIZE = 50
#: answers per IQyqnf request: 20 is the app's value and Google's maximum (21 or more: INVALID_ARGUMENT)
ANSWERS_PAGE_SIZE = 20
#: Google's error slot for an unknown feature
NOT_FOUND = 5


class QaPlaceNotFound(Exception):
    """The Q&A app does not know the place (error NOT_FOUND): not evidence of zero questions."""


class QaUnreadable(Exception):
    """The answer is not one of the Q&A app's known shapes."""


# --------------------------------------------------------------------------- requests
def list_call(fid: str, token: str | None, *, size: int, answered: bool, hl: str, reqid: int) -> tuple[str, str]:
    inner = [fid, token, size, LIST_ANSWERS, None, 1] if answered else [fid, token or "", size, 0, 1]
    return (pb.qa_batchexecute_path(LIST_RPC, fid, hl=hl, reqid=reqid), pb.batchexecute_body(LIST_RPC, inner, tag="1"))


def answers_call(fid: str, question_id: str, token: str | None, *, size: int, hl: str, reqid: int) -> tuple[str, str]:
    inner = [fid, question_id, token, 1, None, size]
    return (pb.qa_batchexecute_path(ANSWERS_RPC, fid, hl=hl, reqid=reqid), pb.batchexecute_body(ANSWERS_RPC, inner, tag="1"))


def place_call(fid: str, *, hl: str, reqid: int) -> tuple[str, str]:
    return (pb.qa_batchexecute_path(PLACE_RPC, fid, hl=hl, reqid=reqid), pb.batchexecute_body(PLACE_RPC, [fid], tag="1"))


# --------------------------------------------------------------------------- records
def _person(p: Any) -> dict[str, Any] | None:
    if not isinstance(p, list) or not p or not isinstance(p[0], str):
        return None
    return {"name": p[0], "photoUrl": p[1] if len(p) > 1 and isinstance(p[1], str) else None,
            "profileUrl": p[2] if len(p) > 2 and isinstance(p[2], str) else None,
            "merchant": bool(p[3]) if len(p) > 3 else False}


def _record(r: Any, what: str) -> dict[str, Any]:
    if not (isinstance(r, list) and len(r) > 6 and isinstance(r[0], str) and isinstance(r[6], str) and r[6]):
        raise QaUnreadable(f"unexpected {what} record: {str(r)[:120]}")
    return {"id": r[6], "text": r[0],
            "votes": r[1] if isinstance(r[1], int) and not isinstance(r[1], bool) else None,
            "author": _person(r[3]), "date": r[4] if isinstance(r[4], str) else None}


def _answer(a: Any) -> dict[str, Any]:
    r = _record(a, "answer")
    return {"answerId": r["id"], "answer": r["text"], "answerDate": r["date"], "answeredBy": r["author"],
            "votes": r["votes"]}


def _count(value: Any, what: str) -> int:
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        raise QaUnreadable(f"{what}: the answer count is {value!r}, not a number")
    return value


def _token(value: Any, rpc: str) -> str | None:
    """A continuation token: a string, ``""`` at the end of the list (None is returned for it)."""
    if not isinstance(value, str):
        raise QaUnreadable(f"{rpc}: the continuation token is {type(value).__name__}, not a string")
    return value or None


def unique(records: list[dict[str, Any]], key: str) -> tuple[list[dict[str, Any]], int]:
    """The records in order without a repeated id, and how many repeats were dropped."""
    seen: set[str] = set()
    out = [r for r in records if not (r[key] in seen or seen.add(r[key]))]
    return out, len(records) - len(out)


def _answers(value: Any, count: int, what: str) -> list[dict[str, Any]]:
    if value is None and count == 0:
        return []
    if not isinstance(value, list):
        raise QaUnreadable(f"{what}: answers {str(value)[:80]} for an answer count of {count}")
    return [_answer(a) for a in value]


def _question(item: Any, *, answered: bool) -> dict[str, Any]:
    if not (isinstance(item, list) and len(item) >= 3):
        raise QaUnreadable(f"unexpected question item: {str(item)[:120]}")
    q = _record(item[0], "question")
    count = _count(item[2], "question")
    answers, repeated = unique(_answers(item[1], count, "question"), "answerId")
    if len(answers) > count:
        raise QaUnreadable(f"question {q['id']}: {len(answers)} answers listed for an answer count of {count}")
    out = {"questionId": q["id"], "question": q["text"], "askDate": q["date"], "askedBy": q["author"],
           "votes": q["votes"], "answered": answered, "answersCount": count, "answers": answers,
           "answersComplete": len(answers) >= count}
    if repeated:
        out["repeatedAnswers"] = repeated
    return out


# --------------------------------------------------------------------------- answers
def _payload(text: str, rpc: str) -> Any:
    try:
        env = rpc_payload(text, rpc)
    except ResponseFormatError as exc:
        raise QaUnreadable(f"{rpc}: {exc}") from exc
    if env.error is not None:
        code = env.error[0] if isinstance(env.error, list) and env.error else env.error
        if code == NOT_FOUND:
            raise QaPlaceNotFound(f"{rpc}: the Q&A app does not know this place (NOT_FOUND)")
        raise QaUnreadable(f"{rpc} refused the request (error {env.error})")
    if env.payload is None:
        raise QaUnreadable(f"{rpc} answered without a payload")
    return env.payload


@dataclass
class QuestionPage:
    questions: list[dict[str, Any]]
    next_token: str | None      # None or "" -- the list ended


def parse_question_page(text: str, *, answered: bool) -> QuestionPage:
    """``[]`` -- no questions; ``[[item, ...], token]`` -- a page.  Questions repeated within the page
    are kept here: the task drops them against everything it holds."""
    data = _payload(text, LIST_RPC)
    if data == []:
        return QuestionPage([], None)
    if not (isinstance(data, list) and len(data) >= 2 and isinstance(data[0], list)):
        raise QaUnreadable(f"unexpected {LIST_RPC} payload: {str(data)[:120]}")
    token = _token(data[1], LIST_RPC)
    return QuestionPage([_question(it, answered=answered) for it in data[0]], token)


@dataclass
class AnswersPage:
    question_id: str            # the question the page belongs to (checked against the one asked)
    answers: list[dict[str, Any]]
    count: int                  # Google's answer count of the question
    next_token: str | None      # None -- the list ended
    repeated: int = 0           # answers listed twice on the page (dropped)


def parse_answers_page(text: str) -> AnswersPage:
    """``[[question, answers, answer count, null, token]]``."""
    data = _payload(text, ANSWERS_RPC)
    item = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else None
    if item is None or len(item) < 5:
        raise QaUnreadable(f"unexpected {ANSWERS_RPC} payload: {str(data)[:120]}")
    question = _record(item[0], "question")
    count = _count(item[2], ANSWERS_RPC)
    answers, repeated = unique(_answers(item[1], count, ANSWERS_RPC), "answerId")
    return AnswersPage(question["id"], answers, count, _token(item[4], ANSWERS_RPC), repeated)


def parse_place_name(text: str) -> str | None:
    data = _payload(text, PLACE_RPC)
    return data[0] if isinstance(data, list) and data and isinstance(data[0], str) else None


# --------------------------------------------------------------------------- export
def apify_view(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apify's ``questionsAndAnswers``: answered questions, ``url`` = the person's photo (as
    compass/crawler-google-places 0.14.761 writes it; Google's own questions have null name and url)."""
    def who(p):
        return {"name": (p or {}).get("name"), "url": (p or {}).get("photoUrl")}
    return [{"question": q["question"],
             "answers": [{"answer": a["answer"], "answerDate": a["answerDate"], "answeredBy": who(a["answeredBy"])}
                         for a in q["answers"]],
             "askDate": q["askDate"], "askedBy": who(q["askedBy"])}
            for q in questions if q.get("answered")]
