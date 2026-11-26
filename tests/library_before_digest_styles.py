import json
import shutil
import sqlite3
from contextlib import closing
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from infonium import api
from infonium.library import LIBRARY_FILE, SNAPSHOT_DIRECTORY
from tests.model_standin import Asked, Rejection, Reply
from tests.notebook import marked_view, phrase_passage
from tests.service import LEDGER, MADE_UP_QUOTE, App, article, numbered_blocks_asked
from tests.translation_standin import asks_passages, is_survey, translated_whole, translating

# A library that a service of commit bdd3e97 kept, the last before digest styles, for the
# test that opens it in tests/test_digest_styles.py. Its folder holds the service's data folder
# under library (a folder named data is ignored by git), with the library file and the
# snapshots, and in answers.json what that service answered about them. Two pages are read: the
# ledger, whose digest, highlight, note and translation are kept, and the harbour page, which
# has a highlight and no digest. The folder was made by running this file alone,
# `uv run pytest tests/library_before_digest_styles.py`, at that commit: it is not collected
# otherwise, for its name is not that of a test module, a later service has no PUT of a digest
# to write the digest with, and the folder is made only where none is.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "library_before_digest_styles"
KEPT_DATA = FIXTURE / "library"
ANSWERS = FIXTURE / "answers.json"
# The folder of the test's own library that the service is started on.
DATA_FOLDER = "data"
WRITE_AHEAD_LOG = f"{LIBRARY_FILE}-wal"
# The digests the library file kept of a document, read as they were kept, by the table and
# columns of the time.
DIGEST_ROWS = "SELECT body FROM digest WHERE document_id = ?"
LEDGER_PAGE = "ledger"
LEDGER_TITLE = "Quillmoor ledger"
HARBOUR_PAGE = "harbour"
HARBOUR_TITLE = "Quillmoor harbour book"
HARBOUR = (
    "The harbour master of Quillmoor kept a second book for the cargo boats, in which every "
    "barrel of salt herring was counted twice.",
    "Its pages end in the spring of the fifth winter, when the cargo trade moved to the new "
    "quay at Ostrel.",
)
HIGHLIGHT_COMMENT = "四十一个夜晚\N{FULLWIDTH COMMA}大多起雾。"
NOTE_TITLE = "渡船为何晚点"
NOTE_MARKDOWN = "发船改到[八点半](passage:a)之后\N{FULLWIDTH COMMA}晚点少了。"
# The digest kept of the ledger, in the two sections the instructions of the time asked for,
# in the language of digests. Each statement cites the passages given, the last of them one
# that is nowhere in the ledger.
DIGEST_SECTIONS = (
    (
        "摘要",
        (
            (
                "Quillmoor 的港口账簿记下了冬季每个工作日夜渡的发船时间。",
                ("the night ferry left at a quarter past nine",),
            ),
            (
                "守账人 Odalys Brenn 用绿墨记发船\N{FULLWIDTH COMMA}用红墨记延误。",
                ("wrote each departure in green ink and each delay in red",),
            ),
        ),
    ),
    (
        "要点",
        (
            (
                "第三个冬季渡船有四十一个夜晚晚点\N{FULLWIDTH COMMA}多在东防波堤起雾的几周。",
                (
                    "the ferry ran late on forty-one nights",
                    "when fog lay over the eastern breakwater",
                ),
            ),
            (
                "镇议会把发船提前到八点半\N{FULLWIDTH COMMA}次年冬季晚点降到九次。",
                ("cut the late crossings to nine", MADE_UP_QUOTE),
            ),
            ("账簿至今保存在镇图书馆。", ("港の帳簿は町の図書館に今も保管されている",)),
        ),
    ),
)


class ItemAnswers(BaseModel):
    # What the service answered about one item: its entry, its document, its revisions, its
    # highlights and its translation; and the digests the library file kept of its document,
    # as it kept them.
    entry: dict[str, Any]
    document: dict[str, Any]
    revisions: dict[str, Any]
    highlights: list[dict[str, Any]]
    translation: dict[str, Any]
    digests: list[dict[str, Any]]


class LibraryAnswers(BaseModel):
    # What the service answered about the library it kept, in the language of digests it
    # translated into: the list of items, each item by its id, the highlights of the library,
    # and its notes, listed and each by its id.
    language: str
    items: list[dict[str, Any]]
    item_answers: dict[str, ItemAnswers]
    highlights: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    note_views: dict[str, dict[str, Any]]


def ledger_digest(asked: Asked) -> Reply:
    # Each quote is cited under the number of the block that holds it, or, where none holds
    # it, of the first block.
    blocks = numbered_blocks_asked(asked)

    def citation(quote: str) -> dict[str, Any]:
        holders = [number for number, text in blocks if quote in text]
        return {"block_number": (holders or [blocks[0][0]])[0], "quote": quote}

    sections = [
        {
            "title": title,
            "statements": [
                {"text": text, "citations": [citation(quote) for quote in quotes]}
                for text, quotes in statements
            ],
        }
        for title, statements in DIGEST_SECTIONS
    ]
    return Reply(pieces=[json.dumps({"sections": sections}, ensure_ascii=False)])


def answering(asked: Asked) -> Reply | Rejection:
    # Translates the ledger, and writes its digest.
    if is_survey(asked) or asks_passages(asked):
        return translating()(asked)
    return ledger_digest(asked)


def answered(app: App, path: str) -> Any:
    response = app.client.get(path)
    assert response.status_code == HTTPStatus.OK, f"GET {path} failed: {response.text}"
    return response.json()


def answered_of_item(app: App, item: str) -> dict[str, Any]:
    # What the service answers about an item, each part under its name in ItemAnswers.
    return {
        "entry": answered(app, f"/api/items/{item}"),
        "document": answered(app, f"/api/items/{item}/document"),
        "revisions": answered(app, f"/api/items/{item}/revisions"),
        "highlights": answered(app, f"/api/items/{item}/highlights"),
        "translation": answered(app, f"/api/items/{item}/translation"),
    }


def digest_rows(library_file: Path, document_id: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(library_file)) as connection:
        return [json.loads(body) for (body,) in connection.execute(DIGEST_ROWS, (document_id,))]


def test_make_the_library_kept_before_digest_styles(app: App, tmp_path: Path) -> None:
    app.model.reset(answering)
    ledger = app.read_page(LEDGER_PAGE, article(LEDGER_TITLE, LEDGER))
    harbour = app.read_page(HARBOUR_PAGE, article(HARBOUR_TITLE, HARBOUR))
    written = app.client.put(f"/api/items/{ledger}/digest")
    assert written.status_code == HTTPStatus.OK, f"the ledger's digest was not written: {written}"
    marked_view(
        app,
        ledger,
        phrase_passage(app, ledger, [(LEDGER[2], "forty-one nights")]),
        HIGHLIGHT_COMMENT,
    )
    marked_view(app, harbour, phrase_passage(app, harbour, [(HARBOUR[1], "the new quay")]))
    noted = app.client.post(
        "/api/notes",
        json={
            "edition": 1,
            "title": NOTE_TITLE,
            "markdown": NOTE_MARKDOWN,
            "passages": [
                {
                    "id": "a",
                    "passage": phrase_passage(app, ledger, [(LEDGER[3], "half past eight")]),
                }
            ],
        },
    )
    assert noted.status_code == HTTPStatus.OK, f"the note was not kept: {noted.text}"
    translated_whole(app, ledger)
    of_items = {item: answered_of_item(app, item) for item in (ledger, harbour)}
    items = answered(app, "/api/items")
    highlights = answered(app, "/api/highlights")
    notes = answered(app, "/api/notes")
    note_views = {note["id"]: answered(app, f"/api/notes/{note['id']}") for note in notes}
    app.stop()
    app.store.engine.dispose()
    data = tmp_path / DATA_FOLDER
    answers = LibraryAnswers(
        language=api.settings.language,
        items=items,
        item_answers={
            item: ItemAnswers(
                **answered_parts,
                digests=digest_rows(
                    data / LIBRARY_FILE, answered_parts["entry"]["reading"]["document"]
                ),
            )
            for item, answered_parts in of_items.items()
        },
        highlights=highlights,
        notes=notes,
        note_views=note_views,
    )
    assert [len(answers.item_answers[item].digests) for item in (ledger, harbour)] == [1, 0], (
        "the library does not keep a digest of the ledger alone"
    )
    assert not (data / WRITE_AHEAD_LOG).exists(), "the library's log was left beside its file"
    KEPT_DATA.mkdir(parents=True)
    shutil.copyfile(data / LIBRARY_FILE, KEPT_DATA / LIBRARY_FILE)
    shutil.copytree(data / SNAPSHOT_DIRECTORY, KEPT_DATA / SNAPSHOT_DIRECTORY)
    ANSWERS.write_text(answers.model_dump_json(indent=2) + "\n", encoding="utf-8")
