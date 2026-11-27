import itertools
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import BaseModel

from infonium.document import Block, Document, Paragraph, document_of
from infonium.language_model import FORM_TOLD, JSON_OBJECT
from infonium.library import LOCAL_OWNER, Item, LibraryStore, Link, Read, Settled, item_identity
from tests.model_standin import TRANSLATION_MODEL, Asked, Rejection, Reply, Responder
from tests.service import SETTLE_SECONDS, SITE, App, digest_reply

# What the translating model is asked, read as the part writes it: the first request of a
# translation asks for a survey of the document in a form of its own name; every other asks
# for passages numbered across the blocks they belong to, under a heading of their own, and
# is answered with each block's passages by their numbers.
SURVEY_FORM = "DocumentSurvey"
# Where a request to GLM's endpoint, which holds a reply to JSON alone, names the form it asks
# for: in its instructions.
TOLD_FORM_NAME = re.compile(re.escape(FORM_TOLD.split("{name}")[0]) + r"(\w+)")
PASSAGES_HEADING = "Passages to translate:"
BLOCK_HEADING = re.compile(r"^Block (\d+):$")
PASSAGE_LINE = re.compile(r"^\[(\d+)\] (.*)$")
# Where the passages of a request end: the text that follows them, if any, comes after an
# empty line.
SECTION_END = "\n\n"
# A tag the part writes around the words of a mark: a name, the mark's number, and the end
# tag the same with a slash.
TAG = re.compile(r"</?([a-z]+)(\d+)>")
# What the stand-in puts before each passage it translates by default: one character, so
# that no answer is out of proportion to its passage, of a script the made-up sources do not
# use.
TRANSLATED = "译"
SNAPSHOT = "made-up-snapshot"
EXTRACTOR = "made-up/1"
WEB_PAGE = "text/html"
# The media type of a recording's snapshot.
RECORDING = "audio/mp4"
TITLE = "The harbour of Quillmoor"
# Words of made-up sources in several scripts, every one of them holding letters.
WORDS = (
    "harbour",
    "ledger",
    "quorvex",
    "zindlap",
    "évier",
    "straße",
    "гавань",
    "λιμάνι",
    "港の帳簿",
    "ميناء",
    "항구",
)
# How often a request of a translation is tried.
TRIES = 3
# How long a test lets the translator go on before it judges that something did not happen.
QUIET_SECONDS = 0.3
# How often a test looks at a translation it waits for: a translation of a made-up document
# takes some tens of milliseconds, and the properties wait for hundreds of them.
TRANSLATION_POLL_SECONDS = 0.01
# A paragraph of a long document: long enough that a request holds a few of them.
LONG_PARAGRAPH_WORDS = 80
names = itertools.count()


class Passage(BaseModel):
    # One passage of a request: the block it is listed under, its number, and its text with
    # the part's tags in it.
    block: int
    number: int
    text: str


def form_name(asked: Asked) -> str:
    # The name of the form of reply a request asks for, in either wire API, or in the
    # instructions of a request that asks for JSON alone.
    if "text" in asked.body:
        return str(asked.body["text"]["format"]["name"])
    response_format = asked.body.get("response_format", {})
    if response_format.get("type") == JSON_OBJECT:
        told = TOLD_FORM_NAME.search(asked.text())
        return told[1] if told else ""
    return str(response_format.get("json_schema", {}).get("name", ""))


def is_survey(asked: Asked) -> bool:
    return form_name(asked) == SURVEY_FORM


def asks_passages(asked: Asked) -> bool:
    return PASSAGES_HEADING in asked.text()


def passages_asked(asked: Asked) -> list[Passage]:
    section = asked.text().split(PASSAGES_HEADING, 1)[1].lstrip("\n").split(SECTION_END)[0]
    passages: list[Passage] = []
    block = -1
    for line in section.splitlines():
        heading, passage = BLOCK_HEADING.match(line), PASSAGE_LINE.match(line)
        if heading:
            block = int(heading[1])
        elif passage:
            passages.append(Passage(block=block, number=int(passage[1]), text=passage[2]))
        else:
            raise ValueError(f"a line of the passages asked is of no known form: {line!r}")
    return passages


def plain(text: str) -> str:
    # A passage's text without the part's tags: what a reader reads of it.
    return TAG.sub("", text)


def survey_reply(
    matches_asked_language: bool = False, terms: dict[str, str] | None = None
) -> Reply:
    survey = {
        "language": "made-up",
        "matches_asked_language": matches_asked_language,
        "terms": [
            {"source": source, "translation": translation}
            for source, translation in (terms or {}).items()
        ],
    }
    return Reply(pieces=[json.dumps(survey)])


type Wording = Callable[[Passage], str]


def marked(passage: Passage) -> str:
    # The default translation: the passage as it was sent, tags and all, after one character.
    return f"{TRANSLATED}{passage.text}"


def passages_reply(passages: list[Passage], wording: Wording) -> Reply:
    blocks: dict[int, list[dict[str, object]]] = {}
    for passage in passages:
        blocks.setdefault(passage.block, []).append(
            {"number": passage.number, "translation": wording(passage)}
        )
    reply = {
        "blocks": [
            {"block": block, "translations": translations} for block, translations in blocks.items()
        ]
    }
    return Reply(pieces=[json.dumps(reply, ensure_ascii=False)])


def translating(wording: Wording = marked, survey: Reply | None = None) -> Responder:
    # The stand-in as a translating model that surveys a document as not written in the
    # asked language, and answers every passage by the wording given; a request for a digest
    # is answered as one.
    def respond(asked: Asked) -> Reply | Rejection:
        if is_survey(asked):
            return survey or survey_reply()
        if asks_passages(asked):
            return passages_reply(passages_asked(asked), wording)
        return digest_reply(asked)

    return respond


def by_translating_model(asked: Asked) -> bool:
    return bool(asked.body["model"] == TRANSLATION_MODEL)


def kept_document(
    store: LibraryStore,
    name: str,
    title: str,
    blocks: list[Block],
    media_type: str = WEB_PAGE,
    owner: str = LOCAL_OWNER,
) -> Document:
    # A made-up document kept in the library as read from a page of the stand-in site, so
    # that a test gives the translator exactly the blocks it judges. The item is named by the
    # page's address, as the service names it.
    url = f"{SITE}/made-up/{name}"
    item_id = item_identity(owner, url)
    document = document_of(item_id, EXTRACTOR, SNAPSHOT, media_type, blocks)
    reading = Read(title=title, document=document.id, attempt=Settled())
    with store.library(owner) as library:
        library.keep_item(
            Item(id=item_id, origin=Link(url=url), added=datetime.now(UTC), reading=reading)
        )
        library.keep_document(document, datetime.now(UTC))
    return document


class Ledgered(BaseModel):
    # A document kept for a test, its item, and its blocks.
    item_id: str
    document_id: str
    blocks: list[Block]


def kept(app: App, blocks: list[Block], media_type: str = WEB_PAGE) -> Ledgered:
    document = kept_document(app.store, f"document-{next(names)}", TITLE, blocks, media_type)
    return Ledgered(item_id=document.item_id, document_id=document.id, blocks=blocks)


def translation_path(item_id: str) -> str:
    return f"/api/items/{item_id}/translation"


def block_path(item_id: str, block_number: int) -> str:
    return f"{translation_path(item_id)}/blocks/{block_number}"


def translation(app: App, item_id: str) -> dict[str, Any]:
    served = app.client.get(translation_path(item_id))
    assert served.status_code == HTTPStatus.OK, f"GET of a translation failed: {served.text}"
    view: dict[str, Any] = served.json()
    return view


def asked_for(app: App, item_id: str) -> dict[str, Any]:
    put = app.client.put(translation_path(item_id))
    assert put.status_code == HTTPStatus.OK, f"PUT of a translation failed: {put.text}"
    view: dict[str, Any] = put.json()
    return view


def settled(app: App, item_id: str) -> dict[str, Any]:
    # The translation once it is no longer under way.
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        view = translation(app, item_id)
        if view["progress"]["kind"] != "translating":
            return view
        if time.monotonic() > deadline:
            pytest.fail(f"a translation did not end in time: {view['progress']}")
        time.sleep(TRANSLATION_POLL_SECONDS)


def translated_whole(app: App, item_id: str) -> dict[str, Any]:
    asked_for(app, item_id)
    view = settled(app, item_id)
    assert view["progress"]["kind"] == "translated", (
        f"a translation the model answered in full did not end translated: {view['progress']}"
    )
    return view


def by_number(view: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {block["block_number"]: block for block in view["blocks"]}


def passage_requests(app: App) -> list[Asked]:
    return [asked for asked in app.model.asked() if asks_passages(asked)]


def passages_sent(app: App) -> Counter[str]:
    # How often each passage was sent to the model, by its text as a reader reads it.
    return Counter(
        plain(passage.text) for asked in passage_requests(app) for passage in passages_asked(asked)
    )


def surveys(app: App) -> list[Asked]:
    return [asked for asked in app.model.asked() if is_survey(asked)]


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + SETTLE_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"the test waited in vain for {what}")
        time.sleep(TRANSLATION_POLL_SECONDS)


def quiet(app: App) -> None:
    # Waits until the model has had no request for a while.
    while True:
        before = len(app.model.asked())
        time.sleep(QUIET_SECONDS)
        if len(app.model.asked()) == before:
            return


def long_paragraphs(count: int, extra: Callable[[int], str] = lambda number: "") -> list[Block]:
    # Paragraphs of a long document, each told apart from every other by its first words and
    # its last, and each with what the function given puts after its first words. Each
    # document's paragraphs name it, so that a request that a translation of an earlier test
    # still sends to the stand-in is never taken for one of this document.
    filler = " ".join(WORDS[number % 4] for number in range(LONG_PARAGRAPH_WORDS))
    document = next(names)
    return [
        Paragraph(
            text=f"Paragraph {number} begins {extra(number)}of document {document} {filler} "
            f"ends {number}.",
            marks=[],
        )
        for number in range(count)
    ]


def holding(
    responder: Responder, gate: threading.Event, held: Callable[[Asked], bool]
) -> Responder:
    # The responder's replies, those to the requests chosen held after their first piece until
    # the gate opens.
    def respond(asked: Asked) -> Reply | Rejection:
        answer = responder(asked)
        if held(asked) and isinstance(answer, Reply):
            return answer.model_copy(update={"gate": gate})
        return answer

    return respond


def first_replies(times: int, wrong: Responder, then: Responder) -> Responder:
    # The first requests for passages answered wrongly, every later one as the other says.
    answered = itertools.count()

    def respond(asked: Asked) -> Reply | Rejection:
        if asks_passages(asked) and next(answered) < times:
            return wrong(asked)
        return then(asked)

    return respond
