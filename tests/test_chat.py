import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium import api
from infonium.intake import failure_reason
from infonium.language_model import WireApi, endable_openai_model
from tests.model_standin import (
    STANDIN_MODEL,
    Asked,
    Finish,
    Rejection,
    Reply,
    pieces_of,
)
from tests.service import LEDGER, REVISED, SITE, App, numbered_blocks_asked

LEDGER_TITLE = "Quillmoor ledger"
QUESTION = "When did the night ferry leave?"
# Words of statements and questions, none of them in the article, among them what JSON must
# escape and what is not ASCII.
WORDING = ("quorvex", "zindlap", 'the "plovane"', "back\\slash", "line\nbreak", "ベルト", "évier")
EXAMPLES = settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
# How long a reader who left may wait for the conversation to take the next question, and
# for the model's request to end.
LEAVING_SECONDS = 2.0
MODEL_FAILURES = (
    "status_500",
    "status_429",
    "refused",
    "cut_short",
    "filtered",
    "failed",
    "unmetered",
    "unparsable",
    "misshapen",
)
# Failures whose wording depends on the form of reply asked for; their reason is judged by
# its presence alone.
FORM_FAILURES = frozenset({"unparsable", "misshapen"})


class Citation(BaseModel):
    block_number: int
    quote: str


class Said(BaseModel):
    # A statement as the model writes it in the form the conversation asks for.
    text: str
    citations: list[Citation]


class Spoken(BaseModel):
    statements: list[Said]


class Ledger(BaseModel):
    # The item of the made-up article a test asks about, read.
    item_id: str
    document_id: str
    blocks: list[str]


class Asking(BaseModel):
    # What a question's response was: its status and, for a stream, its events in order.
    status: int
    events: list[dict[str, Any]]
    body: str


wording = st.lists(st.sampled_from(WORDING), min_size=1, max_size=6).map(" ".join)
cuts = st.lists(st.integers(0, 10**4), max_size=10)


def spoken_reply(spoken: Spoken, at: list[int] | None = None) -> Reply:
    return Reply(pieces=pieces_of(spoken.model_dump_json(), at or []))


def plain(texts: list[str]) -> Spoken:
    return Spoken(statements=[Said(text=text, citations=[]) for text in texts])


def says(texts: list[str]) -> Callable[[Asked], Reply]:
    # The model answers with these statements, citing nothing.
    return lambda asked: spoken_reply(plain(texts))


def conversation_path(item_id: str) -> str:
    return f"/api/items/{item_id}/conversation"


def asking(client: httpx2.Client, item_id: str, question: str) -> Asking:
    with client.stream("POST", conversation_path(item_id), json={"text": question}) as response:
        lines = [line for line in response.iter_lines() if line.strip()]
    streamed = response.status_code == HTTPStatus.OK
    events = [json.loads(line) for line in lines] if streamed else []
    return Asking(status=response.status_code, events=events, body="\n".join(lines))


def turns(app: App, item_id: str) -> list[dict[str, Any]]:
    answered = app.client.get(conversation_path(item_id))
    assert answered.status_code == HTTPStatus.OK, f"a conversation was not served: {answered.text}"
    found: list[dict[str, Any]] = answered.json()["turns"]
    return found


def answered_turn(questioned: Asking) -> dict[str, Any]:
    assert questioned.status == HTTPStatus.OK, f"a question was refused: {questioned.body}"
    assert questioned.events and questioned.events[-1]["kind"] == "answered", (
        f"a question the model answered did not end with the answer: {questioned.events}"
    )
    turn: dict[str, Any] = questioned.events[-1]["turn"]
    return turn


def statement_texts(turn: dict[str, Any]) -> list[str]:
    return [statement["text"] for statement in turn["answer"]["statements"]]


@pytest.fixture
def ledger(app: App) -> Ledger:
    item_id = app.read("/ledger")
    document = app.client.get(f"/api/items/{item_id}/document").json()
    return Ledger(
        item_id=item_id,
        document_id=document["id"],
        blocks=[block["text"] for block in document["blocks"]],
    )


@contextmanager
def held(app: App, reply: Reply) -> Iterator[threading.Event]:
    # The model writes the first piece of the reply and waits until the test opens the gate,
    # which it does at the latest when the context ends.
    gate = threading.Event()
    app.model.reset(lambda asked: reply.model_copy(update={"gate": gate}))
    try:
        yield gate
    finally:
        gate.set()


def lines_of(response: httpx2.Response) -> Iterator[dict[str, Any]]:
    return (json.loads(line) for line in response.iter_lines() if line.strip())


def first_line(lines: Iterator[dict[str, Any]]) -> dict[str, Any]:
    first = next(lines, None)
    if first is None:
        pytest.fail("a question's response ended before its first line")
    return first


# R3: the answer is grounded.


def quotes_of(ledger: Ledger) -> st.SearchStrategy[Citation]:
    # A quote copied from one of the article's own paragraphs, under that paragraph's number.
    numbers = [number for number, text in enumerate(ledger.blocks) if text in LEDGER]

    def quote(number: int, start: int, length: int) -> Citation:
        text = ledger.blocks[number]
        words = text.split() if " " in text else list(text)
        first = start % (len(words) - 3)
        span = words[first : first + 3 + length % (len(words) - first - 2)]
        return Citation(block_number=number, quote=(" " if " " in text else "").join(span))

    return st.builds(quote, st.sampled_from(numbers), st.integers(0, 99), st.integers(0, 99))


def made_up_quotes(ledger: Ledger) -> st.SearchStrategy[Citation]:
    return st.builds(
        Citation,
        block_number=st.integers(0, len(ledger.blocks) - 1),
        quote=st.sampled_from(["The ferry sank in the summer.", "港は閉鎖された。"]),
    )


def test_the_model_is_given_the_document_as_a_digest_is(app: App, ledger: Ledger) -> None:
    app.client.put(f"/api/items/{ledger.item_id}/digest")
    [digest_asked] = app.model.asked()
    app.model.reset(says(["The ferry left at nine."]))
    answered_turn(asking(app.client, ledger.item_id, QUESTION))
    [answer_asked] = app.model.asked()
    given_blocks = numbered_blocks_asked(answer_asked)
    assert given_blocks == numbered_blocks_asked(digest_asked), (
        "the model was not given the item's document as numbered blocks as a digest's prompt "
        "gives them"
    )
    assert [number for number, _ in given_blocks] == list(range(len(ledger.blocks))), (
        "the model was not given every block of the item's document"
    )
    assert QUESTION in answer_asked.text(), "the model was not given the question"


@given(data=st.data())
@EXAMPLES
def test_citations_become_anchors_only_where_the_quote_is_in_the_document(
    app: App, ledger: Ledger, data: st.DataObject
) -> None:
    found = data.draw(st.lists(quotes_of(ledger), min_size=1, max_size=3), "found")
    made_up = data.draw(st.lists(made_up_quotes(ledger), min_size=1, max_size=2), "made up")
    spoken = Spoken(
        statements=[
            Said(text="What the ledger says.", citations=found),
            Said(text="What it never says.", citations=made_up),
            Said(text="The ledger does not say who built the ferry.", citations=[]),
        ]
    )
    app.model.reset(lambda asked: spoken_reply(spoken))
    answer = answered_turn(asking(app.client, ledger.item_id, QUESTION))["answer"]
    grounded, unfounded, uncovered = answer["statements"]
    assert grounded["anchors"], "a statement whose quotes are in the document has no anchor"
    for anchor in grounded["anchors"]:
        text = ledger.blocks[anchor["block_number"]]
        assert text[anchor["start"] : anchor["end"]] == anchor["quote"], (
            f"an anchor's range does not hold its quote: {anchor}"
        )
        assert anchor["document_id"] == ledger.document_id, "an anchor names another document"
    for citation in found:
        assert any(
            anchor["block_number"] == citation.block_number and anchor["quote"] in citation.quote
            for anchor in grounded["anchors"]
        ), f"a quote copied from its block did not become an anchor in that block: {citation}"
    assert unfounded["anchors"] == [], "a quote that is not in the document became an anchor"
    assert uncovered["anchors"] == [], "a statement without citations has an anchor"
    assert answer["sources"] == [
        {"document": ledger.document_id, "item": ledger.item_id, "title": LEDGER_TITLE}
    ], "an answer with anchors does not name the document, item and title they lie in"


@given(texts=st.lists(wording, min_size=1, max_size=3))
@EXAMPLES
def test_an_answer_without_anchors_names_no_source(
    app: App, ledger: Ledger, texts: list[str]
) -> None:
    app.model.reset(says(texts))
    answer = answered_turn(asking(app.client, ledger.item_id, QUESTION))["answer"]
    assert statement_texts({"answer": answer}) == texts, "the answer's statements were changed"
    assert answer["sources"] == [], "an answer without anchors names a source"


# R4: earlier turns count.


@given(
    conversation=st.lists(
        st.tuples(wording, st.lists(wording, min_size=1, max_size=2)), min_size=2, max_size=4
    )
)
@EXAMPLES
def test_the_model_is_given_the_earlier_questions_and_their_statements_in_order(
    app: App, ledger: Ledger, conversation: list[tuple[str, list[str]]]
) -> None:
    # Each question and statement is told apart from every other by its number.
    numbered = [
        (f"{question} q{number}", [f"{text} s{number}.{at}" for at, text in enumerate(texts)])
        for number, (question, texts) in enumerate(conversation)
    ]
    app.client.delete(conversation_path(ledger.item_id))
    for question, texts in numbered:
        app.model.reset(says(texts))
        answered_turn(asking(app.client, ledger.item_id, question))
        given = app.model.asked()[-1].text()
        earlier = [part for asked_before, said in numbered for part in [asked_before, *said]]
        expected = [*earlier[: earlier.index(question)], question]
        places = [given.find(part) for part in expected]
        assert -1 not in places and places == sorted(places), (
            f"the question {question!r} was not given the earlier questions and their "
            f"statements in order: {list(zip(expected, places, strict=True))}"
        )
    assert [turn["question"] for turn in turns(app, ledger.item_id)] == [
        question for question, _ in numbered
    ], "GET does not return the turns in the order they were asked"


# R5: the endpoints of an item's conversation.


def test_a_conversation_nobody_started_is_empty(app: App, ledger: Ledger) -> None:
    assert turns(app, ledger.item_id) == [], "a conversation nobody started is not empty"


@pytest.mark.parametrize("item_path", ["/no-such-page", None])
def test_an_item_without_a_document_has_no_conversation(app: App, item_path: str | None) -> None:
    item_id = "0" * 64
    if item_path is not None:
        item_id = app.link(f"{SITE}{item_path}").json()["id"]
        assert app.settled(item_id)["reading"]["kind"] == "unread", "a missing page was read"
    path = conversation_path(item_id)
    refused = [
        app.client.get(path),
        app.client.post(path, json={"text": QUESTION}),
        app.client.delete(path),
    ]
    assert [response.status_code for response in refused] == [HTTPStatus.NOT_FOUND] * 3, (
        "an item without a document was not answered 404 on its conversation"
    )
    for response in refused:
        assert response.headers["content-type"] == api.JSON, (
            "a refusal of an item without a document began the lines of an answer"
        )
        assert isinstance(response.json()["detail"], str), "a refusal is not a Failure"
    assert app.model.asked() == [], "the model was asked about an item without a document"


@given(texts=st.lists(wording, min_size=1, max_size=4), at=cuts)
@EXAMPLES
def test_the_answer_is_sent_as_it_is_written_then_once_whole(
    app: App, ledger: Ledger, texts: list[str], at: list[int]
) -> None:
    app.model.reset(lambda asked: spoken_reply(plain(texts), at))
    asked = asking(app.client, ledger.item_id, QUESTION)
    turn = answered_turn(asked)
    drafts = [event["statements"] for event in asked.events[:-1]]
    assert all(event["kind"] == "drafted" for event in asked.events[:-1]), (
        f"an event other than Drafted came before the last: {asked.events}"
    )
    assert drafts, "no Drafted was sent while the answer was written"
    for draft in drafts:
        assert draft and len(draft) <= len(texts) and draft[:-1] == texts[: len(draft) - 1], (
            f"a Drafted holds statements the answer does not: {draft} of {texts}"
        )
        assert texts[len(draft) - 1].startswith(draft[-1]), (
            f"a Drafted statement is not the start of the statement written: {draft}"
        )
    grown = [len("".join(draft)) + len(draft) for draft in drafts]
    assert grown == sorted(grown) and len(set(map(json.dumps, drafts))) == len(drafts), (
        "a Drafted did not grow from the one before"
    )
    assert statement_texts(turn) == texts and turn["question"] == QUESTION, (
        "Answered does not hold the question and the statements the model wrote"
    )
    assert turns(app, ledger.item_id)[-1] == turn, "a GET does not return the turn Answered held"


def test_what_is_written_is_sent_before_the_model_has_finished(app: App, ledger: Ledger) -> None:
    text = plain(["The night ferry left at a quarter past nine.", "It ran late in fog."])
    written = text.model_dump_json()
    cut = written.index("past") + len("past")
    path = conversation_path(ledger.item_id)
    with (
        held(app, Reply(pieces=[written[:cut], written[cut:]])) as gate,
        app.served() as reader,
        reader.stream("POST", path, json={"text": QUESTION}) as response,
    ):
        lines = lines_of(response)
        first = first_line(lines)
        still_writing = not gate.is_set()
        gate.set()
        rest = list(lines)
    assert still_writing and first == {
        "kind": "drafted",
        "statements": ["The night ferry left at a quarter past"],
    }, f"what the model had written was not sent while it wrote: {first}"
    assert rest and rest[-1]["kind"] == "answered", "the answer did not end whole"


def failing_reply(failure: str) -> Reply | Rejection:
    written = plain(["The ferry left at nine."]).model_dump_json()
    half = written[: len(written) // 2]
    match failure:
        case "status_500":
            return Rejection(status=HTTPStatus.INTERNAL_SERVER_ERROR, body="the model is down")
        case "status_429":
            return Rejection(status=HTTPStatus.TOO_MANY_REQUESTS, body="slow down")
        case "refused":
            return Reply(pieces=["I cannot help with that."], refused=True)
        case "cut_short":
            return Reply(pieces=[half], finish=Finish.CUT_SHORT)
        case "filtered":
            return Reply(pieces=[half], finish=Finish.FILTERED)
        case "failed":
            return Reply(pieces=[half], finish=Finish.FAILED)
        case "unmetered":
            return Reply(pieces=[written], metered=False)
        case "unparsable":
            return Reply(pieces=["The ferry left at nine."])
        case _:
            return Reply(pieces=['{"answer": "The ferry left at nine."}'])


def interface_failure(wire_api: WireApi, failure: str) -> BaseException:
    # What the model's interface raises for the same reply, asked directly.
    try:
        endable_openai_model(name=STANDIN_MODEL, wire_api=wire_api).structured(
            "Answer.", QUESTION, Spoken
        )
    except Exception as raised:
        return raised
    pytest.fail(f"the model's interface took a reply that is {failure} for an answer")


@pytest.mark.parametrize("wire_api", list(WireApi))
@pytest.mark.parametrize("failure", MODEL_FAILURES)
def test_a_failure_of_the_model_ends_the_lines_unanswered_and_keeps_nothing(
    start_app: Callable[..., App], wire_api: WireApi, failure: str
) -> None:
    app = start_app(wire_api)
    item_id = app.read("/ledger")
    app.model.reset(says(["Kept before."]))
    kept = [answered_turn(asking(app.client, item_id, "What came first?"))]
    app.model.reset(lambda asked: failing_reply(failure))
    asked = asking(app.client, item_id, QUESTION)
    assert asked.status == HTTPStatus.OK, (
        f"a failure of the model was not told in the lines: {asked.body}"
    )
    kinds = [event["kind"] for event in asked.events]
    assert kinds and kinds[-1] == "unanswered" and set(kinds[:-1]) <= {"drafted"}, (
        f"a reply that is {failure} did not end the lines with exactly one Unanswered: {kinds}"
    )
    reason = asked.events[-1]["reason"]
    if failure in FORM_FAILURES:
        assert reason.strip(), "an answer the model wrote in another form has no reason"
    else:
        expected = failure_reason(interface_failure(wire_api, failure))
        assert reason == expected, (
            f"Unanswered is not worded as intake.failure_reason words the failure: {reason!r}"
        )
    assert turns(app, item_id) == kept, f"a reply that is {failure} left something kept"
    app.model.reset(says(["Kept after."]))
    assert statement_texts(answered_turn(asking(app.client, item_id, "Again?"))) == [
        "Kept after."
    ], "the conversation did not take a question after a failure of the model"


def test_a_forgotten_conversation_is_empty_and_not_given_to_the_model(
    app: App, ledger: Ledger
) -> None:
    app.model.reset(says(["Forgotten."]))
    answered_turn(asking(app.client, ledger.item_id, "A question to forget?"))
    forgotten = app.client.delete(conversation_path(ledger.item_id))
    assert forgotten.status_code == HTTPStatus.NO_CONTENT, f"DELETE failed: {forgotten.text}"
    assert turns(app, ledger.item_id) == [], "a forgotten conversation still has turns"
    answered_turn(asking(app.client, ledger.item_id, QUESTION))
    assert "A question to forget?" not in app.model.asked()[-1].text(), (
        "a forgotten question was given to the model"
    )


# R6: one question at a time.


def test_a_question_or_a_delete_while_answering_is_refused_and_not_queued(
    app: App, ledger: Ledger
) -> None:
    other = app.read("/reader")
    written = plain(["The ferry left at nine."]).model_dump_json()
    path = conversation_path(ledger.item_id)
    cut = written.index("left")
    with (
        held(app, Reply(pieces=[written[:cut], written[cut:]])) as gate,
        app.served() as reader,
        reader.stream("POST", path, json={"text": QUESTION}) as response,
    ):
        lines = lines_of(response)
        first_line(lines)
        second = reader.post(path, json={"text": "And the second?"})
        forgotten = reader.delete(path)
        asked_while = len(app.model.asked())
        beside = reader.get(conversation_path(other))
        gate.set()
        rest = list(lines)
    assert [second.status_code, forgotten.status_code] == [HTTPStatus.CONFLICT] * 2, (
        "a question or a DELETE while the conversation was answering was not refused with 409"
    )
    for refused in (second, forgotten):
        assert isinstance(refused.json()["detail"], str), "a refusal with 409 is not a Failure"
    assert asked_while == 1, "a question refused while answering was sent to the model"
    assert beside.status_code == HTTPStatus.OK, "another item's conversation was held"
    assert rest and rest[-1]["kind"] == "answered", "the answer under way did not end whole"
    assert [turn["question"] for turn in turns(app, ledger.item_id)] == [QUESTION], (
        "a refused question was kept, or the DELETE was done after all"
    )
    app.model.reset(says(["Next."]))
    answered_turn(asking(app.client, ledger.item_id, "And the second?"))


# R7: a reader who leaves.


def test_a_reader_who_leaves_keeps_nothing_and_the_next_question_is_taken_at_once(
    app: App, ledger: Ledger
) -> None:
    written = plain(["The ferry left at nine.", "It ran late in fog."]).model_dump_json()
    path = conversation_path(ledger.item_id)
    with held(app, Reply(pieces=[written[:30], written[30:]])), app.served() as reader:
        with reader.stream("POST", path, json={"text": QUESTION}) as response:
            assert first_line(lines_of(response))["kind"] == "drafted", (
                "no Drafted came before leaving"
            )
        left_at = time.monotonic()
        [cut] = app.model.requests()
        ended = cut.ended.wait(LEAVING_SECONDS)
        app.model.reset(says(["Taken."]))
        next_asked = asking(reader, ledger.item_id, "Then when?")
        taken_after = time.monotonic() - left_at
    assert ended and cut.left, (
        f"the model's request went on more than {LEAVING_SECONDS} s after the reader left"
    )
    assert next_asked.status == HTTPStatus.OK and taken_after < LEAVING_SECONDS, (
        f"the conversation did not take the next question at once after the reader left: "
        f"{next_asked.status} after {taken_after:.2f} s"
    )
    assert [turn["question"] for turn in turns(app, ledger.item_id)] == ["Then when?"], (
        "a reader who left in the middle of an answer left something of it kept"
    )


# R8: kept in the library.


def test_a_conversation_survives_a_restart(start_app: Callable[..., App]) -> None:
    first = start_app()
    item_id = first.read("/ledger")
    first.model.reset(says(["Kept across a restart."]))
    answered_turn(asking(first.client, item_id, QUESTION))
    before = turns(first, item_id)
    first.stop()
    again = start_app()
    assert turns(again, item_id) == before, "a conversation did not survive a restart"


def test_a_conversation_is_kept_by_item_and_by_owner(app: App, ledger: Ledger) -> None:
    other = app.read("/reader")
    app.model.reset(says(["Of the ledger."]))
    answered_turn(asking(app.client, ledger.item_id, QUESTION))
    assert turns(app, other) == [], "a conversation of one item shows in another's"
    api.app.dependency_overrides[api.current_owner] = lambda: "another-owner"
    try:
        foreign = app.client.get(conversation_path(ledger.item_id))
        own_item = app.read("/ledger")
        own = turns(app, own_item)
    finally:
        api.app.dependency_overrides.clear()
    assert foreign.status_code == HTTPStatus.NOT_FOUND, "another owner reached a conversation"
    assert own == [], "another owner's item of the same page shares its conversation"


def test_questionedleaves_the_item_its_document_and_digest_as_they_were(
    app: App, ledger: Ledger
) -> None:
    paths = [f"/api/items/{ledger.item_id}", f"/api/items/{ledger.item_id}/document"]
    digest = app.client.put(f"/api/items/{ledger.item_id}/digest").json()
    before = [app.client.get(path).json() for path in paths]
    app.model.reset(says(["Nothing changes."]))
    answered_turn(asking(app.client, ledger.item_id, QUESTION))
    after = [app.client.get(path).json() for path in paths]
    kept_digest = app.client.put(f"/api/items/{ledger.item_id}/digest").json()
    assert (after, kept_digest) == (before, digest), (
        "asking about an item changed its entry, document or digest"
    )


def test_an_item_read_again_keeps_its_conversation_and_its_anchors_documents(
    app: App,
) -> None:
    item_id = app.read("/revised")
    old_document = app.client.get(f"/api/items/{item_id}/document").json()
    quote = " ".join(LEDGER[0].split()[1:7])
    number = next(
        at for at, block in enumerate(old_document["blocks"]) if block["text"] == LEDGER[0]
    )
    cited = Spoken(
        statements=[
            Said(
                text="The ledger records the ferry.",
                citations=[Citation(block_number=number, quote=quote)],
            )
        ]
    )
    app.model.reset(lambda asked: spoken_reply(cited))
    turn = answered_turn(asking(app.client, item_id, QUESTION))
    assert turn["answer"]["statements"][0]["anchors"], "a quote of the document found no anchor"
    app.link(f"{SITE}/revised")
    reread = app.settled(item_id)
    assert reread["reading"]["document"] != old_document["id"], "the page was not read anew"
    kept = turns(app, item_id)
    assert kept == [turn], "an item read again lost its conversation"
    [anchor] = kept[0]["answer"]["statements"][0]["anchors"]
    assert anchor["document_id"] == old_document["id"], (
        "an earlier answer's anchor no longer names the document it was found in"
    )
    assert kept[0]["answer"]["sources"][0]["document"] == old_document["id"], (
        "an earlier answer's source no longer names the document it was found in"
    )
    app.model.reset(says(["Revised."]))
    answered_turn(asking(app.client, item_id, "What does it say now?"))
    assert REVISED[0] in app.model.asked()[-1].text(), (
        "a question after the item was read again was not asked of its new document"
    )
