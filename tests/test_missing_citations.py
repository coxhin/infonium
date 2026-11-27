import json
from collections.abc import Callable, Sequence
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr

from infonium import api
from infonium.digest import SectionForm
from infonium.language_model import FORM_TOLD, SYSTEM_ROLE, WireApi
from infonium.library import LOCAL_OWNER
from tests.conversation import asking, kept_turns
from tests.model_standin import (
    CHAT_PATH,
    GLM_CHAT_PATH,
    GLM_KEY,
    RESPONSES_PATH,
    Asked,
    Reply,
    pieces_of,
)
from tests.service import LEDGER, OVERVIEW, App, keep_sources
from tests.test_chat import (
    LEDGER_TITLE,
    Citation,
    Ledger,
    conversation_path,
    cuts,
    made_up_quotes,
    quotes_of,
    wording,
)
from tests.test_digest_styles import digest_path, digests_path
from tests.test_library_chat import CONVERSATION, handles_by_title, scripted, searching

# The judgments of a statement that a model's reply gives without its citations key, R1 to R5:
# in an answer about an item, an answer about the library and a digest, each written by the
# default model, which its API holds to the reply's schema on either wire API, and by a GLM
# model, which is only told the schema in its instructions.
EXAMPLES = settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
QUESTION = "Who built the night ferry?"
SEARCHED = "ferry"
SECTION_TITLE = "要点"
# The keys of a statement as a model writes it and as infonium keeps it, of the statements of
# an answer and the sections of a digest, of an anchor's block and quote, and of when a reply
# was kept, which two replies kept one after the other never share.
TEXT = "text"
CITATIONS = "citations"
ANCHORS = "anchors"
LOST_CITATIONS = "lost_citations"
STATEMENTS = "statements"
SECTIONS = "sections"
BLOCK_NUMBER = "block_number"
QUOTE = "quote"
WRITTEN = "written"
# The words of a JSON schema that say which properties an object requires, and a property's
# default.
PROPERTIES = "properties"
REQUIRED = "required"
DEFAULT = "default"
# What the instructions of a GLM model say right before the schema of the reply's form.
SCHEMA_INTRODUCED = FORM_TOLD.split("{schema}")[0].split("{name}")[1]


class Writing(StrEnum):
    # The replies whose statements cite their sources.
    ITEM_ANSWER = "an answer about an item"
    LIBRARY_ANSWER = "an answer about the library"
    DIGEST = "a digest"


class Way(StrEnum):
    # The model that writes a reply, and how it is held to the reply's form.
    RESPONSES = "the default model held to the schema by the Responses API"
    CHAT_COMPLETIONS = "the default model held to the schema by the Chat Completions API"
    GLM = "a GLM model told the schema in its instructions"


class Route(BaseModel):
    # The wire API the service's own models speak, the query that chooses the model of a way,
    # and where the requests of its replies arrive.
    wire_api: WireApi
    query: dict[str, str]
    path: str


ROUTES = {
    Way.RESPONSES: Route(wire_api=WireApi.RESPONSES, query={}, path=RESPONSES_PATH),
    Way.CHAT_COMPLETIONS: Route(wire_api=WireApi.CHAT_COMPLETIONS, query={}, path=CHAT_PATH),
    Way.GLM: Route(
        wire_api=WireApi.RESPONSES,
        query={"model": api.settings.glm_models[0]},
        path=GLM_CHAT_PATH,
    ),
}


class Carrying(BaseModel):
    # A statement that the stand-in writes with its citations, none or more.
    text: str
    citations: list[Citation]


class Lacking(BaseModel):
    # A statement that the stand-in writes without the citations key.
    text: str


type Planned = Carrying | Lacking


lacking = st.builds(Lacking, text=wording)


def citing(ledger: Ledger) -> st.SearchStrategy[Carrying]:
    # A statement that carries up to three citations, each of a quote copied from the block it
    # names or of one written nowhere in the ledger.
    citations = st.one_of(quotes_of(ledger), made_up_quotes(ledger))
    return st.builds(Carrying, text=wording, citations=st.lists(citations, max_size=3))


def grounding(ledger: Ledger) -> st.SearchStrategy[Carrying]:
    # A statement that cites one to three quotes copied from the blocks they name.
    return st.builds(
        Carrying, text=wording, citations=st.lists(quotes_of(ledger), min_size=1, max_size=3)
    )


def leaving_out(ledger: Ledger) -> st.SearchStrategy[list[Planned]]:
    # The statements of a reply in any order: one or two that lack the citations key, one or two
    # whose quotes are found in the ledger, and up to two more of either kind.
    return st.tuples(
        st.lists(lacking, min_size=1, max_size=2),
        st.lists(grounding(ledger), min_size=1, max_size=2),
        st.lists(st.one_of(lacking, citing(ledger)), max_size=2),
    ).flatmap(lambda drawn: st.permutations([*drawn[0], *drawn[1], *drawn[2]]))


def statement_written(planned: Planned, naming: dict[str, str]) -> dict[str, Any]:
    # A statement as the stand-in writes it: with the citations it carries, each naming its
    # block, its quote and what naming adds, or without the key.
    match planned:
        case Carrying():
            cited = [{**naming, **citation.model_dump()} for citation in planned.citations]
            return {TEXT: planned.text, CITATIONS: cited}
        case _:
            return {TEXT: planned.text}


def with_empty_citations(planned: Planned) -> Carrying:
    # The statement with its missing citations key written as [].
    match planned:
        case Carrying():
            return planned
        case _:
            return Carrying(text=planned.text, citations=[])


def reply_body(writing: Writing, statements: list[dict[str, Any]]) -> dict[str, Any]:
    # A digest's statements stand in a section of its draft, an answer's in the answer.
    match writing:
        case Writing.DIGEST:
            section = {"title": SECTION_TITLE, "form": SectionForm.LIST, STATEMENTS: statements}
            return {SECTIONS: [section]}
        case _:
            return {STATEMENTS: statements}


def replying(
    writing: Writing, planned: Sequence[Planned], at: list[int]
) -> Callable[[Asked], Reply]:
    # The stand-in writes the statements planned as its reply, cut into pieces at the places
    # given; in an answer about the library each citation names the ledger by the handle the
    # outline gives it.
    def reply(asked: Asked) -> Reply:
        naming = (
            {"document": handles_by_title(asked)[LEDGER_TITLE]}
            if writing == Writing.LIBRARY_ANSWER
            else {}
        )
        body = reply_body(writing, [statement_written(each, naming) for each in planned])
        return Reply(pieces=pieces_of(json.dumps(body, ensure_ascii=False), at))

    return reply


def described(writing: Writing, way: Way, planned: Sequence[Planned]) -> str:
    lacks = sum(isinstance(each, Lacking) for each in planned)
    return (
        f"{writing} by {way} (the reply lacks the citations key in {lacks} of its "
        f"{len(planned)} statements)"
    )


@pytest.fixture
def served(start_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, way: Way) -> App:
    # The service, its own models speaking the wire API of the way, and GLM's models reached
    # with GLM's key.
    app = start_app(ROUTES[way].wire_api)
    monkeypatch.setattr(api.settings, "glm_api_key", SecretStr(GLM_KEY))
    return app


@pytest.fixture
def ledger(served: App) -> Ledger:
    # An item whose document holds the ledger's paragraphs, one block each.
    [source] = keep_sources(served.store, LOCAL_OWNER, [(LEDGER_TITLE, list(LEDGER))])
    return Ledger(item_id=source.item, document_id=source.document, blocks=source.blocks)


def answer_kept(app: App, path: str, query: dict[str, str], what: str) -> dict[str, Any]:
    questioned = asking(app.client, str(httpx2.URL(path, params=query)), QUESTION)
    last = questioned.events[-1] if questioned.events else {}
    assert questioned.status == HTTPStatus.OK and last.get("kind") == "answered", (
        f"{what} was not answered: {questioned.body[-500:]}"
    )
    turn: dict[str, Any] = last["turn"]
    kept = kept_turns(app.client, path)
    assert kept[-1:] == [turn], (
        f"{what} was answered, but the answer is not kept as the conversation's last turn: "
        f"{kept[-1:]}"
    )
    answer: dict[str, Any] = turn["answer"]
    return answer


def digest_kept(app: App, item_id: str, query: dict[str, str], what: str) -> dict[str, Any]:
    response = app.client.post(digest_path(item_id), json=OVERVIEW, params=query)
    assert response.status_code == HTTPStatus.OK, (
        f"{what} was not written: {response.status_code} {response.text[:500]}"
    )
    digest: dict[str, Any] = response.json()
    kept = app.client.get(digests_path(item_id))
    assert kept.status_code == HTTPStatus.OK and kept.json() == [digest], (
        f"{what} was written, but it is not the digest kept of its item: {kept.text[:500]}"
    )
    return digest


def kept_reply(app: App, writing: Writing, way: Way, ledger: Ledger, what: str) -> dict[str, Any]:
    # What is kept of the reply the stand-in writes, which the test needs answered or written:
    # the answer of the conversation's last turn, or the item's digest.
    route = ROUTES[way]
    match writing:
        case Writing.DIGEST:
            kept = digest_kept(app, ledger.item_id, route.query, what)
        case Writing.ITEM_ANSWER:
            kept = answer_kept(app, conversation_path(ledger.item_id), route.query, what)
        case _:
            kept = answer_kept(app, CONVERSATION, route.query, what)
    paths = [asked.path for asked in app.model.asked()]
    assert paths and set(paths) == {route.path}, (
        f"the requests of {what} were sent to {paths}, not to {route.path}"
    )
    return kept


def statements_of(writing: Writing, kept: dict[str, Any]) -> list[dict[str, Any]]:
    match writing:
        case Writing.DIGEST:
            return [statement for section in kept[SECTIONS] for statement in section[STATEMENTS]]
        case _:
            statements: list[dict[str, Any]] = kept[STATEMENTS]
            return statements


# R1 to R3: a statement that lacks the key is kept as one that cites nothing, in an answer about
# an item (R1), in an answer about the library (R2) and in a digest (R3), and the other
# statements of the reply keep their citations as usual.


def found_at(citation: Citation, anchor: dict[str, Any]) -> bool:
    # The anchor lies in the block the citation names and holds the citation's quote, or a part.
    return anchor[BLOCK_NUMBER] == citation.block_number and anchor[QUOTE] in citation.quote


def judge_citations_kept_as_usual(
    statement: dict[str, Any], planned: Carrying, ledger: Ledger, what: str
) -> None:
    # A quote copied from the block a citation names becomes an anchor in that block, and every
    # other citation is counted lost.
    found = [
        citation
        for citation in planned.citations
        if citation.quote in ledger.blocks[citation.block_number]
    ]
    for citation in found:
        assert any(found_at(citation, anchor) for anchor in statement[ANCHORS]), (
            f"in {what}, a quote that a statement carrying its citations copied from the block it "
            f"names did not become an anchor in that block: {citation} in {statement}"
        )
    for anchor in statement[ANCHORS]:
        assert any(found_at(citation, anchor) for citation in found), (
            f"in {what}, a statement carrying its citations has an anchor that none of its quotes "
            f"found in the ledger gives: {anchor}"
        )
        block = ledger.blocks[anchor[BLOCK_NUMBER]]
        assert (
            anchor["document_id"] == ledger.document_id
            and block[anchor["start"] : anchor["end"]] == anchor[QUOTE]
        ), (
            f"in {what}, an anchor of a statement carrying its citations does not hold its quote "
            f"in the ledger's document: {anchor}"
        )
    lost = len(planned.citations) - len(found)
    assert statement.get(LOST_CITATIONS, 0) == lost, (
        f"in {what}, a statement carrying its citations counts "
        f"{statement.get(LOST_CITATIONS, 0)} lost, not the {lost} whose quotes are written "
        "nowhere in the ledger"
    )


@pytest.mark.parametrize("way", list(Way))
@pytest.mark.parametrize("writing", list(Writing))
@given(data=st.data())
@EXAMPLES
def test_a_statement_that_lacks_its_citations_key_is_kept_with_its_text_citing_nothing(
    served: App, ledger: Ledger, writing: Writing, way: Way, data: st.DataObject
) -> None:
    planned = data.draw(leaving_out(ledger), "reply")
    served.model.reset(replying(writing, planned, data.draw(cuts, "cuts")))
    what = described(writing, way, planned)
    statements = statements_of(writing, kept_reply(served, writing, way, ledger, what))
    assert [statement.get(TEXT) for statement in statements] == [each.text for each in planned], (
        f"{what} is not kept with the statements of its reply, in their order and with their "
        f"texts: {statements}"
    )
    for statement, each in zip(statements, planned, strict=True):
        match each:
            case Carrying():
                judge_citations_kept_as_usual(statement, each, ledger, what)
            case _:
                assert (statement.get(ANCHORS), statement.get(LOST_CITATIONS, 0)) == ([], 0), (
                    f"in {what}, a statement that lacks the key is not kept as one that cites "
                    f"nothing, with no anchors and no lost citations: {statement}"
                )


# R4: the schema given for each reply still requires every statement's citations.


def schema_given(asked: Asked, way: Way) -> object:
    # The schema of the reply's form as a request gives it: in the request's body, where the API
    # holds the model to it, or at the end of the instructions, where a GLM model is told it.
    match way:
        case Way.RESPONSES:
            return asked.body.get("text", {}).get("format", {}).get("schema")
        case Way.CHAT_COMPLETIONS:
            return asked.body.get("response_format", {}).get("json_schema", {}).get("schema")
        case _:
            instructions = "\n".join(text for role, text in asked.messages() if role == SYSTEM_ROLE)
            _, introduced, told = instructions.rpartition(SCHEMA_INTRODUCED)
            if not introduced:
                pytest.fail(
                    f"a request to a GLM model is not told the reply's schema: {asked.path}"
                )
            try:
                return json.loads(told)
            except ValueError:
                pytest.fail(f"a GLM model is told a schema that is not JSON: {told[:300]!r}")


def statement_schemas(schema: object) -> list[dict[str, Any]]:
    # The objects a schema describes whose properties are a statement's text and citations.
    match schema:
        case dict():
            properties = schema.get(PROPERTIES)
            named = properties.keys() if isinstance(properties, dict) else set()
            own = [schema] if {TEXT, CITATIONS} <= named else []
            return own + [found for value in schema.values() for found in statement_schemas(value)]
        case list():
            return [found for value in schema for found in statement_schemas(value)]
        case _:
            return []


@pytest.mark.parametrize("way", list(Way))
@pytest.mark.parametrize("writing", list(Writing))
def test_the_schema_given_for_a_reply_requires_every_statements_citations_without_a_default(
    served: App, ledger: Ledger, writing: Writing, way: Way
) -> None:
    # A reply about the library searches first, so that its later request is judged too.
    planned = [
        Carrying(
            text="The ferry left at a quarter past nine.",
            citations=[Citation(block_number=0, quote="left at a quarter past nine")],
        ),
        Carrying(text="The ledger does not say who built the ferry.", citations=[]),
    ]
    answering = replying(writing, planned, [])
    replies = (
        [lambda asked: searching(SEARCHED), answering]
        if writing == Writing.LIBRARY_ANSWER
        else [answering]
    )
    served.model.reset(scripted(replies))
    what = described(writing, way, planned)
    kept_reply(served, writing, way, ledger, what)
    requests = served.model.asked()
    assert len(requests) == len(replies), (
        f"{what} took {len(requests)} requests, not the {len(replies)} the stand-in answered"
    )
    for asked in requests:
        schema = schema_given(asked, way)
        statements = statement_schemas(schema)
        shown = json.dumps(schema, ensure_ascii=False)[:500]
        assert statements, (
            f"request {asked.number} of {what} gives no schema of a statement with its text and "
            f"citations: {shown}"
        )
        for statement in statements:
            assert CITATIONS in statement.get(REQUIRED, []), (
                f"in request {asked.number} of {what}, the schema given does not list citations "
                f"among the required properties of a statement: {statement}"
            )
            assert DEFAULT not in statement[PROPERTIES][CITATIONS], (
                f"in request {asked.number} of {what}, the schema given carries a default for a "
                f"statement's citations: {statement[PROPERTIES][CITATIONS]}"
            )


# R5: a reply is kept as the same reply with each missing citations key written as [].


@pytest.mark.parametrize("way", list(Way))
@pytest.mark.parametrize("writing", list(Writing))
@given(data=st.data())
@EXAMPLES
def test_a_reply_is_kept_as_the_same_reply_with_each_missing_citations_key_written_as_empty(
    served: App, ledger: Ledger, writing: Writing, way: Way, data: st.DataObject
) -> None:
    planned = data.draw(
        st.lists(st.one_of(lacking, citing(ledger)), min_size=1, max_size=5), "reply"
    )
    at = data.draw(cuts, "cuts")
    # What is kept of the reply as drawn, then of the same reply with each missing key written.
    kept: list[dict[str, Any]] = []
    for statements in (planned, [with_empty_citations(each) for each in planned]):
        served.model.reset(replying(writing, statements, at))
        reply_kept = kept_reply(served, writing, way, ledger, described(writing, way, statements))
        kept.append({name: value for name, value in reply_kept.items() if name != WRITTEN})
    assert kept[0] == kept[1], (
        f"{described(writing, way, planned)} is kept otherwise than the same reply with each "
        f"missing key written as []: {kept[0]} against {kept[1]}"
    )
