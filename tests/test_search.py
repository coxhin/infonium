import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from uuid import uuid4

import openai
import pytest
import sqlite_vec
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr

from infonium import library, search
from infonium.document import HTML_MEDIA_TYPE, Block, Heading, Paragraph, document_of
from infonium.embedding import EmbeddingEngine
from infonium.library import (
    LIBRARY_FILE,
    LOCAL_OWNER,
    Item,
    LibraryStore,
    Link,
    Read,
    Settled,
    item_identity,
)
from infonium.search import BlockIndex, FoundBlock, FoundBy
from tests.embedding_standin import (
    CONCEPT_WORDS,
    EMBEDDING_BASE_URL,
    EMBEDDING_KEY,
    EMBEDDING_MODEL,
    OTHER_EMBEDDING_MODEL,
    UNSPACED,
    Concept,
    EmbeddingAsked,
    EmbeddingStandin,
)
from tests.service import SITE, App, article

OTHER_OWNER = "another-reader"
# The index's rounds, lowered from ten seconds so that a test waits a fraction of one for the
# work a round does; and how long a test waits for that work at most.
ROUND_SECONDS = 0.1
APART_SECONDS = 3600.0
WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05
# How long a search waits for the engine to embed its query, lowered where a test waits it out.
QUERY_WAIT_SECONDS = 1.0
# The patience of a writer to the library's file, lowered from 30 seconds so that a test can
# keep the file busy for longer than it.
LOWERED_PATIENCE = "PRAGMA busy_timeout = 100"
BUSY_SECONDS = 1.0
FOUND_BY_WORDS = frozenset({FoundBy.ALL_WORDS, FoundBy.ALL_WORDS_AND_MEANING})
FOUND_BY_MEANING = frozenset({FoundBy.MEANING, FoundBy.ALL_WORDS_AND_MEANING})
# The index's own tables are told from the library's by their names; FTS5 keeps a virtual
# table's rows in tables named after it.
INDEX_TABLES = "search%"
FTS_SHADOWS = ("_data", "_idx", "_content", "_docsize", "_config")
ENGINE_AWAY = ("refused", "429", "500", "502", "503")
ENGINE_FAULTS = (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND)
# A limit above the number of blocks of any library a test makes.
EVERY_BLOCK = 10_000
EXAMPLES = settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
# The examples of a property that waits for the engine to give vectors.
FEW_EXAMPLES = settings(
    max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture]
)

# Words of a sentence that names one concept, in five languages, the concept's word in its
# place; a question in Chinese and in English about each concept; the order of the languages
# is that of CONCEPT_WORDS.
LANGUAGES = ("en", "fr", "es", "fi", "zh", "ja")
SENTENCES = {
    "en": "Every morning the village talks first about the {}.",
    "fr": "Chaque matin, on parle d'abord de {} au village.",
    "es": "Cada mañana se habla primero de {} en el pueblo.",
    "fi": "Joka aamu kylässä puhutaan ensin asiasta {}.",
    "ja": "毎朝、村ではまず{}の話をします。",
}
CHINESE_QUESTION = "{}什么时候最重要呢"
ENGLISH_QUESTION = "What does one say of the {}?"
NEUTRAL = (
    "Nothing much happened in the square during the long afternoon.",
    "Les enfants ont joué près du puits jusqu'au soir.",
    "Los vecinos pintaron la puerta de azul otra vez.",
)
# Words that name no concept, for blocks whose words a test chooses.
VOCABULARY = ("amber", "birch", "cobalt", "dune", "ember", "fjord", "garnet", "heron", "indigo")
SYNTAX = ('"', "*", "^", ":", "(", ")", "-", "+", "{", "}", "NEAR", "AND", "OR", "NOT", "'", "\\")
WORD_PATTERN = re.compile(r"\w+")
# Where a strategy puts the example's own word, which the example draws itself.
OWN = "ownword"
# Letters of scripts written with spaces between words, and of those written without.
SPACED_SCRIPTS = {
    "latin": "abcdefghijklmnopqrstuvwxyzàéèçñäöüß",
    "cyrillic": "абвгдежзийклмнопрстуфхцчшщыэюя",
    "greek": "αβγδεζηθικλμνξοπρστυφχψω",
    "arabic": "ابتثجحخدذرزسشصضطظعغفقكلمنهوي",
    "hebrew": "אבגדהוזחטיכלמנסעפצקרשת",
    "devanagari": "कखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह",
    "georgian": "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ",
    "armenian": "աբգդեզէըթժիլխծկհձղճմյնշոչպջռսվտրցւփքօֆ",
}
UNSPACED_SAMPLE = "日本語港帳簿町図書館保管渡船雨風山川海空花鳥月星ひらがなカタカナ"
SEPARATORS = (" ", ", ", "; ", " — ", ". ")
HANGUL_STEMS = st.text(
    alphabet=st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3), min_size=2, max_size=4
)
PARTICLES = ("은", "는", "이", "가", "을", "를", "에", "에서", "의", "도", "로")


class Kept(BaseModel):
    # A document kept for an item as a reading that settled keeps it.
    item: str
    document: str
    title: str
    blocks: list[str]

    def place(self, number: int) -> tuple[str, int]:
        return (self.document, number)

    def place_of(self, text: str) -> tuple[str, int]:
        if text not in self.blocks:
            pytest.fail(f"a paragraph that a test needs was not read as a block: {text!r}")
        return self.place(self.blocks.index(text))


@pytest.fixture(autouse=True)
def quick_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "CATCH_UP_SECONDS", ROUND_SECONDS)


def paragraphs(texts: list[str]) -> list[Block]:
    return [Paragraph(text=text, marks=[]) for text in texts]


def kept_document(
    store: LibraryStore, owner: str, url: str, title: str, blocks: list[Block]
) -> Kept:
    item_id = item_identity(owner, url)
    document = document_of(
        item_id=item_id,
        extractor="test",
        snapshot="0" * 64,
        media_type=HTML_MEDIA_TYPE,
        blocks=blocks,
    )
    now = datetime.now(UTC)
    reading = Read(title=title, document=document.id, attempt=Settled())
    with store.library(owner) as kept:
        kept.keep_item(Item(id=item_id, origin=Link(url=url), added=now, reading=reading))
        kept.keep_document(document, now)
    return Kept(
        item=item_id, document=document.id, title=title, blocks=[block.text for block in blocks]
    )


def read_page(app: App, name: str, title: str, texts: list[str]) -> Kept:
    # A made-up page of the stand-in site, read by the service as a reader reads it, again if
    # it was read before.
    app.site.pages[name] = article(title, tuple(texts))
    item_id = app.read(f"/page/{name}")
    entry = app.client.get(f"/api/items/{item_id}").json()
    document = app.client.get(f"/api/items/{item_id}/document").json()
    return Kept(
        item=item_id,
        document=document["id"],
        title=entry["reading"]["title"],
        blocks=[block["text"] for block in document["blocks"]],
    )


@contextmanager
def own_index() -> Iterator[tuple[LibraryStore, BlockIndex]]:
    # A library and its index of their own, at work as the service sets them to work, for
    # one example of a property.
    with tempfile.TemporaryDirectory(prefix="infonium-") as folder:
        store = LibraryStore(Path(folder))
        engine = EmbeddingEngine(
            base_url=EMBEDDING_BASE_URL, api_key=SecretStr(EMBEDDING_KEY), model=EMBEDDING_MODEL
        )
        index = BlockIndex(store=store, engine=engine)
        index.resume()
        try:
            yield store, index
        finally:
            index.close()
            store.lock.release()


@pytest.fixture
def library_alone(embedder: EmbeddingStandin) -> Iterator[tuple[LibraryStore, BlockIndex]]:
    # A library and its index without the service, for the examples of a property to fill.
    with own_index() as kept:
        yield kept


def example_url() -> str:
    return f"{SITE}/example/{uuid4().hex}"


def words_of(text: str) -> list[str]:
    return WORD_PATTERN.findall(text.casefold())


def holds_every_word(text: str, query: str) -> bool:
    return set(words_of(query)) <= set(words_of(text))


def holds_some_word(text: str, query: str) -> bool:
    return bool(set(words_of(query)) & set(words_of(text)))


def places(found: list[FoundBlock]) -> list[tuple[str, int]]:
    return [(block.document, block.block_number) for block in found]


def places_found_by(found: list[FoundBlock], ways: frozenset[FoundBy]) -> list[tuple[str, int]]:
    return places([block for block in found if block.found_by in ways])


def shown(found: list[FoundBlock]) -> str:
    return "; ".join(f"{block.found_by}: {block.text[:60]!r}" for block in found)


def wait_until(check: Callable[[], bool], failure: Callable[[], str]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not check():
        if time.monotonic() > deadline:
            pytest.fail(failure())
        time.sleep(POLL_SECONDS)


def concept_word(concept: Concept, language: str) -> str:
    return CONCEPT_WORDS[concept][LANGUAGES.index(language)]


def keep_village(store: LibraryStore, owner: str = LOCAL_OWNER) -> dict[Concept, list[Kept]]:
    # For each concept, a document in each of five languages whose only block names it; and a
    # document that names none.
    village = {
        concept: [
            kept_document(
                store,
                owner,
                f"{SITE}/village/{concept}/{language}",
                f"Village notes {number}",
                paragraphs([sentence.format(concept_word(concept, language))]),
            )
            for number, (language, sentence) in enumerate(SENTENCES.items())
        ]
        for concept in Concept
    }
    kept_document(
        store, owner, f"{SITE}/village/square", "Village square", paragraphs(list(NEUTRAL))
    )
    return village


def chinese_question(concept: Concept) -> str:
    return CHINESE_QUESTION.format(concept_word(concept, "zh"))


def found_by_meaning_first(
    index: BlockIndex, query: str, expected: list[tuple[str, int]], owner: str = LOCAL_OWNER
) -> bool:
    found = index.search(owner, query, len(expected))
    return set(places_found_by(found, FOUND_BY_MEANING)) == set(expected)


def wait_for_meaning(
    index: BlockIndex, query: str, expected: list[tuple[str, int]], owner: str = LOCAL_OWNER
) -> None:
    wait_until(
        lambda: found_by_meaning_first(index, query, expected, owner),
        lambda: (
            f"the blocks that answer {query!r} were not found first by their meaning: "
            f"{shown(index.search(owner, query, len(expected)))}"
        ),
    )


def village_places(village: dict[Concept, list[Kept]], concept: Concept) -> list[tuple[str, int]]:
    return [kept.place(0) for kept in village[concept]]


def judge_ranking(found: list[FoundBlock], query: str, limit: int, holders: int) -> None:
    # What every ranking holds to: at most the limit, each block once, the blocks that hold
    # every word of the query above all others, and each block saying truly how it was found.
    assert len(found) <= limit, f"a search for {limit} blocks gave {len(found)}"
    assert len(set(places(found))) == len(found), f"a block was found twice: {shown(found)}"
    holding = [holds_every_word(block.text, query) for block in found]
    assert sum(holding) == min(holders, limit), (
        f"of the {holders} blocks that hold every word of {query!r}, {sum(holding)} were found "
        f"among {len(found)}: {shown(found)}"
    )
    assert holding == sorted(holding, reverse=True), (
        f"a block that does not hold every word of {query!r} stood above one that does: "
        f"{shown(found)}"
    )
    for block in found:
        assert (block.found_by in FOUND_BY_WORDS) == holds_every_word(block.text, query), (
            f"a block said it was found by {block.found_by} for {query!r}: {block.text!r}"
        )
        assert block.found_by != FoundBy.SOME_WORDS or holds_some_word(block.text, query), (
            f"a block said it was found by some words of {query!r}, and holds none: {block.text!r}"
        )


# R1: by words, in every script, a query being plain words.


@st.composite
def spaced_blocks(draw: st.DrawFn) -> tuple[list[str], str, int]:
    # Blocks of words in one script, and a query of some words of one of them, in any order.
    letters = SPACED_SCRIPTS[draw(st.sampled_from(sorted(SPACED_SCRIPTS)))]
    word = st.text(alphabet=letters, min_size=2, max_size=8)
    blocks = draw(st.lists(st.lists(word, min_size=3, max_size=10), min_size=1, max_size=4))
    texts = [
        "".join(f"{text}{draw(st.sampled_from(SEPARATORS))}" for text in words).strip()
        for words in blocks
    ]
    number = draw(st.integers(min_value=0, max_value=len(blocks) - 1))
    asked = draw(st.lists(st.sampled_from(blocks[number]), min_size=1, max_size=3, unique=True))
    return texts, " ".join(asked), number


@given(drawn=spaced_blocks())
@EXAMPLES
def test_a_block_is_found_by_its_words_in_every_spaced_script(
    library_alone: tuple[LibraryStore, BlockIndex], drawn: tuple[list[str], str, int]
) -> None:
    store, index = library_alone
    texts, query, number = drawn
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Spaced", paragraphs(texts))
    found = index.search(LOCAL_OWNER, query, EVERY_BLOCK)
    assert kept.place(number) in places_found_by(found, FOUND_BY_WORDS), (
        f"a block was not found by its words {query!r}: {shown(found)}"
    )


@st.composite
def unspaced_blocks(draw: st.DrawFn) -> tuple[str, str, str]:
    # A run of Chinese and Japanese letters, two or more of them side by side as a query, and
    # a run that holds the same letters apart.
    run = draw(st.text(alphabet=UNSPACED_SAMPLE, min_size=4, max_size=20))
    start = draw(st.integers(min_value=0, max_value=len(run) - 2))
    end = draw(st.integers(min_value=start + 2, max_value=len(run)))
    query = run[start:end]
    apart = "、".join(query)
    return run, query, apart


@given(drawn=unspaced_blocks())
@EXAMPLES
def test_chinese_or_japanese_letters_side_by_side_find_the_blocks_that_hold_them(
    library_alone: tuple[LibraryStore, BlockIndex], drawn: tuple[str, str, str]
) -> None:
    store, index = library_alone
    run, query, apart = drawn
    blocks = paragraphs([f"{run}。", apart])
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Unspaced", blocks)
    found = index.search(LOCAL_OWNER, query, EVERY_BLOCK)
    assert kept.place(0) in places_found_by(found, FOUND_BY_WORDS), (
        f"a block was not found by the letters {query!r} it holds side by side: {shown(found)}"
    )
    assert kept.place(1) not in places_found_by(found, FOUND_BY_WORDS), (
        f"a block that holds the letters of {query!r} only apart was found by every word: "
        f"{shown(found)}"
    )


@given(
    stems=st.lists(HANGUL_STEMS, min_size=3, max_size=8, unique=True),
    particles=st.lists(st.sampled_from(PARTICLES), min_size=8, max_size=8),
    number=st.integers(min_value=0, max_value=2),
)
@example(stems=["서울", "한강", "도시"], particles=["은"] * 8, number=0)
@EXAMPLES
def test_a_korean_word_is_found_in_the_forms_it_is_used_in(
    library_alone: tuple[LibraryStore, BlockIndex],
    stems: list[str],
    particles: list[str],
    number: int,
) -> None:
    store, index = library_alone
    text = " ".join(stem + particle for stem, particle in zip(stems, particles, strict=False))
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Korean", paragraphs([text]))
    found = index.search(LOCAL_OWNER, stems[number], EVERY_BLOCK)
    assert kept.place(0) in places_found_by(found, FOUND_BY_WORDS), (
        f"the Korean word {stems[number]!r} was not found in the form it is used in {text!r}"
    )


@given(
    asked=st.lists(st.sampled_from(VOCABULARY), min_size=2, max_size=3, unique=True),
    syntax=st.lists(st.sampled_from(SYNTAX), min_size=1, max_size=3),
)
@FEW_EXAMPLES
def test_no_character_of_a_query_is_search_syntax(
    embedder: EmbeddingStandin, asked: list[str], syntax: list[str]
) -> None:
    # Each block holds the query's words but one, or begins one of them; a block that held
    # every word only by a reading of the query as syntax would be found by every word.
    query = f" {syntax[0]} ".join(asked) + "".join(syntax[1:])
    texts = [
        *(" ".join(word for word in asked if word != left) + " stays here" for left in asked),
        " ".join(f"{word}s" for word in asked) + " stays here",
    ]
    whole = " ".join(words_of(query))
    with own_index() as (store, index):
        kept = kept_document(
            store, LOCAL_OWNER, f"{SITE}/syntax", "Syntax", paragraphs([*texts, whole])
        )
        found = index.search(LOCAL_OWNER, query, len(texts) + 1)
    judge_ranking(found, query, len(texts) + 1, holders=1)
    assert places_found_by(found, FOUND_BY_WORDS) == [kept.place(len(texts))], (
        f"the query {query!r} was not taken as plain words: {shown(found)}"
    )


# Any text but the NUL character, which a judgment of its own holds the search to.
NUL = "\x00"
PLAIN_TEXT = st.text(alphabet=st.characters(exclude_characters=NUL), min_size=1)
QUERIES = st.one_of(
    PLAIN_TEXT,
    st.text(alphabet=st.characters(categories=["P", "S", "Z"]), min_size=1),
    st.lists(
        st.one_of(
            st.sampled_from(SYNTAX), st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF)
        ),
        min_size=1,
    ).map("".join),
    st.lists(st.sampled_from(VOCABULARY + SYNTAX), min_size=200, max_size=900).map(" ".join),
)


@pytest.fixture
def village(app: App, embedder: EmbeddingStandin) -> dict[Concept, list[Kept]]:
    # The village's documents in the service's library, each found by its meaning.
    embedder.bring_up()
    kept = keep_village(app.store)
    for concept in Concept:
        wait_for_meaning(app.index, chinese_question(concept), village_places(kept, concept))
    return kept


@given(query=QUERIES)
@example(query="\ud800")
@example(query="?!…")
@EXAMPLES
def test_any_query_is_answered_and_leaves_the_index_at_work(
    app: App, village: dict[Concept, list[Kept]], query: str
) -> None:
    # A query that the engine refused would end the index's work, and every later search with
    # it.
    app.index.search(LOCAL_OWNER, query, 10)
    question = chinese_question(Concept.FERRY)
    assert found_by_meaning_first(app.index, question, village_places(village, Concept.FERRY)), (
        f"after the query {query!r} the library was no longer searched by meaning"
    )


@pytest.mark.parametrize("query", [f"afternoon{NUL}", f"{NUL}afternoon", f"square afternoon{NUL}"])
def test_a_query_holding_the_nul_character_fails_nothing(
    library_alone: tuple[LibraryStore, BlockIndex], query: str
) -> None:
    store, index = library_alone
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Village", paragraphs(list(NEUTRAL)))
    found = index.search(LOCAL_OWNER, query, EVERY_BLOCK)
    assert places_found_by(found, FOUND_BY_WORDS) == [kept.place(0)], (
        f"the block that holds every word of {query!r} was not found by them: {shown(found)}"
    )


# R2: by meaning, across languages.


def test_a_question_finds_its_answer_in_every_language_by_meaning(
    app: App, village: dict[Concept, list[Kept]]
) -> None:
    for concept in Concept:
        expected = village_places(village, concept)
        for question in (chinese_question(concept), ENGLISH_QUESTION.format(concept.value)):
            found = app.index.search(LOCAL_OWNER, question, len(expected))
            assert set(places(found)) == set(expected), (
                f"the question {question!r} did not find first the passages in five languages "
                f"that answer it: {shown(found)}"
            )
        chinese = app.index.search(LOCAL_OWNER, chinese_question(concept), len(expected))
        assert {block.found_by for block in chinese} == {FoundBy.MEANING}, (
            f"passages that share no word with a question in Chinese did not say they were "
            f"found by meaning: {shown(chinese)}"
        )


# R3 and R8: the engine away, failing, and back.


def make_away(embedder: EmbeddingStandin, away: str) -> None:
    # The engine refuses connections, answers with a status of an engine that is away, or holds
    # every request.
    match away:
        case "refused":
            embedder.take_away()
        case "slow":
            embedder.bring_up()
            embedder.hold("")
        case status:
            embedder.bring_up()
            embedder.fail_with(HTTPStatus(int(status)))


HARBOUR = [
    "The quillmoor lautta lähtee satamasta joka aamu ennen auringonnousua.",
    "Le traversier de quillmoor attend les retardataires jusqu'à neuf heures.",
]


@pytest.mark.parametrize("away", [*ENGINE_AWAY, "slow"])
def test_without_the_engine_a_reading_settles_and_is_found_by_its_words(
    app: App,
    embedder: EmbeddingStandin,
    village: dict[Concept, list[Kept]],
    monkeypatch: pytest.MonkeyPatch,
    away: str,
) -> None:
    # The engine goes away from a library whose blocks have vectors, so that every search
    # would ask it for the vector of its query.
    monkeypatch.setattr(search, "QUERY_TIMEOUT_SECONDS", QUERY_WAIT_SECONDS)
    make_away(embedder, away)
    kept = read_page(app, "harbour", "Harbour notes", HARBOUR)
    found = app.index.search(LOCAL_OWNER, "quillmoor traversier", len(kept.blocks))
    assert places_found_by(found, FOUND_BY_WORDS) == [kept.place_of(HARBOUR[1])], (
        f"with the engine {away}, a block was not found by its words: {shown(found)}"
    )
    assert not places_found_by(found, FOUND_BY_MEANING), (
        f"with the engine {away}, a block said it was found by meaning: {shown(found)}"
    )


@pytest.mark.parametrize("away", ENGINE_AWAY)
def test_blocks_read_while_the_engine_is_away_get_their_vectors_once_it_is_back(
    app: App, embedder: EmbeddingStandin, village: dict[Concept, list[Kept]], away: str
) -> None:
    make_away(embedder, away)
    kept = read_page(app, "harbour", "Harbour notes", HARBOUR)
    question = chinese_question(Concept.FERRY)
    found = app.index.search(LOCAL_OWNER, question, len(kept.blocks))
    assert not places_found_by(found, FOUND_BY_MEANING), (
        f"with the engine {away}, a block said it was found by meaning: {shown(found)}"
    )
    embedder.fail_with(None)
    embedder.bring_up()
    harbour = [kept.place_of(text) for text in HARBOUR]
    wait_for_meaning(app.index, question, [*village_places(village, Concept.FERRY), *harbour])


def search_failure(index: BlockIndex, query: str, status: HTTPStatus) -> openai.APIStatusError:
    # A search after the index has failed raises the index's own failure, which names no
    # engine; the engine's answer is its cause.
    try:
        found = index.search(LOCAL_OWNER, query, 5)
    except search.IndexFailed as failure:
        cause = failure.__cause__
        if not isinstance(cause, openai.APIStatusError):
            pytest.fail(
                f"after the engine answered {status.value}, the index's failure has another "
                f"cause than the engine's answer: {cause!r}"
            )
        return cause
    pytest.fail(
        f"after the engine answered {status.value}, a search answered as if the library held "
        f"no more than {shown(found)}"
    )


@pytest.mark.parametrize("status", ENGINE_FAULTS)
def test_an_engine_that_fails_otherwise_ends_the_index_and_every_later_search_says_so(
    app: App, embedder: EmbeddingStandin, status: HTTPStatus
) -> None:
    embedder.bring_up()
    embedder.fail_with(status)
    forms = village_forms(keep_village(app.store))
    wait_until(
        lambda: any(
            request.status == status and forms & set(request.texts) for request in embedder.asked()
        ),
        lambda: "the index did not ask the engine for the vectors of a block",
    )
    embedder.fail_with(None)
    for question in (chinese_question(Concept.FERRY), "village", "village"):
        failure = search_failure(app.index, question, status)
        assert failure.status_code == status, (
            f"after the engine answered {status.value}, a search raised another failure: {failure}"
        )


def test_a_busy_library_file_is_waited_out_and_the_index_works_on(
    start_app: Callable[..., App],
    embedder: EmbeddingStandin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(library, "AWAIT_WRITER", LOWERED_PATIENCE)
    app = start_app()
    village = keep_village(app.store)
    app.index.search(LOCAL_OWNER, "village", 1)
    busy = sqlite3.connect(tmp_path / "data" / LIBRARY_FILE, isolation_level=None)
    busy.execute("BEGIN EXCLUSIVE")
    embedder.bring_up()
    time.sleep(BUSY_SECONDS)
    asked_while_busy = embedder.embedded()
    busy.execute("ROLLBACK")
    busy.close()
    assert asked_while_busy, "the index asked the engine nothing while the library was busy"
    for concept in Concept:
        wait_for_meaning(app.index, chinese_question(concept), village_places(village, concept))


# R4: one ranking.

RANKED_WORDS = (*VOCABULARY, *(concept.value for concept in Concept))


@given(
    blocks=st.lists(
        st.tuples(st.lists(st.sampled_from(RANKED_WORDS), min_size=3, max_size=8), st.booleans()),
        min_size=2,
        max_size=8,
    ),
    asked=st.lists(st.sampled_from(RANKED_WORDS), min_size=1, max_size=3, unique=True),
)
@FEW_EXAMPLES
def test_blocks_that_hold_every_word_stand_above_all_others_in_one_ranking(
    library_alone: tuple[LibraryStore, BlockIndex],
    embedder: EmbeddingStandin,
    blocks: list[tuple[list[str], bool]],
    asked: list[str],
) -> None:
    # A word of the example's own goes into the query and into some of its blocks, so that no
    # block of another example holds every word of the query. The ranking is judged while the
    # blocks get their vectors and after.
    store, index = library_alone
    embedder.bring_up()
    own = f"zq{uuid4().hex[:10]}"
    texts = [" ".join([*words, own] if marked else words) for words, marked in blocks]
    query = " ".join([own, *asked])
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Worded", paragraphs(texts))
    holders = sum(holds_every_word(text, query) for text in texts)
    limit = len(texts) + 2
    forms = {f"{kept.title}\n{text}" for text in texts}
    deadline = time.monotonic() + WAIT_SECONDS
    while not forms <= set(embedder.embedded()) and time.monotonic() < deadline:
        judge_ranking(index.search(LOCAL_OWNER, query, limit), query, limit, holders)
        time.sleep(POLL_SECONDS)
    time.sleep(2 * ROUND_SECONDS)
    found = index.search(LOCAL_OWNER, query, limit)
    judge_ranking(found, query, limit, holders)
    assert places_found_by(found, FOUND_BY_MEANING), (
        f"once the blocks had their vectors, none was found by meaning: {shown(found)}"
    )


BRIEF_UNSPACED = ("渡轮", "雪", "茶", "时钟", "フェ", "ゆき")


BLOCK_KINDS = ("heading", "paragraph", "unspaced")


def word_count(text: str) -> int:
    # Each Chinese or Japanese letter counts as a word, as does each run of other letters.
    return len(UNSPACED.findall(text)) + len(WORD_PATTERN.findall(UNSPACED.sub(" ", text)))


def is_brief(block: Block) -> bool:
    return isinstance(block, Heading) or word_count(block.text) < 3


@st.composite
def headed_blocks(draw: st.DrawFn) -> list[Block]:
    # Headings of any length, blocks of one or two words, and blocks of three or more, among
    # their words those that name concepts and the example's own word, which a test puts in
    # place of OWN.
    worded = st.lists(st.sampled_from([*RANKED_WORDS, OWN]), min_size=1, max_size=8)
    drawn = draw(st.lists(st.tuples(worded, st.sampled_from(BLOCK_KINDS)), min_size=2, max_size=10))
    blocks: list[Block] = []
    for words, kind in drawn:
        match kind:
            case "heading":
                level = draw(st.integers(min_value=1, max_value=3))
                blocks.append(Heading(level=level, text=" ".join(words), marks=[]))
            case "unspaced":
                own = [OWN] if OWN in words else []
                text = " ".join([draw(st.sampled_from(BRIEF_UNSPACED)), *own])
                blocks.append(Paragraph(text=text, marks=[]))
            case _:
                blocks.append(Paragraph(text=" ".join(words), marks=[]))
    return blocks


def judge_brief(
    found: list[FoundBlock],
    query: str,
    briefs: set[tuple[str, int]],
    holders: set[tuple[str, int]],
) -> None:
    for block in found:
        place = (block.document, block.block_number)
        assert place not in briefs or block.found_by == FoundBy.ALL_WORDS, (
            f"a heading or a block of fewer than three words was found by {block.found_by} for "
            f"{query!r}: {block.text!r}"
        )
    assert holders <= set(places(found)), (
        f"a heading or a block of fewer than three words that holds every word of {query!r} "
        f"was not found: {shown(found)}"
    )


@given(drawn=headed_blocks(), asked=st.sampled_from(RANKED_WORDS))
@FEW_EXAMPLES
def test_a_heading_or_a_brief_block_is_found_only_by_every_word(
    library_alone: tuple[LibraryStore, BlockIndex],
    embedder: EmbeddingStandin,
    drawn: list[Block],
    asked: str,
) -> None:
    # Judged while the other blocks get their vectors and after, for a query of words and for
    # questions of meaning alone.
    store, index = library_alone
    embedder.bring_up()
    own = f"zq{uuid4().hex[:10]}"
    blocks = [block.model_copy(update={"text": block.text.replace(OWN, own)}) for block in drawn]
    query = f"{own} {asked}"
    kept = kept_document(store, LOCAL_OWNER, example_url(), "Headed", blocks)
    briefs = {kept.place(number) for number, block in enumerate(blocks) if is_brief(block)}
    holders = {place for place in briefs if holds_every_word(kept.blocks[place[1]], query)}
    limit = len(blocks) + 5
    forms = {f"{kept.title}\n{block.text}" for block in blocks if not is_brief(block)}
    deadline = time.monotonic() + WAIT_SECONDS
    while not forms <= set(embedder.embedded()) and time.monotonic() < deadline:
        judge_brief(index.search(LOCAL_OWNER, query, limit), query, briefs, holders)
        time.sleep(POLL_SECONDS)
    time.sleep(2 * ROUND_SECONDS)
    judge_brief(index.search(LOCAL_OWNER, query, limit), query, briefs, holders)
    for question in map(chinese_question, Concept):
        judge_brief(index.search(LOCAL_OWNER, question, limit), question, briefs, set())


# R5: whose blocks.


@pytest.fixture
def two_villages(app: App, embedder: EmbeddingStandin) -> dict[str, dict[Concept, list[Kept]]]:
    # The same village in the libraries of two owners, each found by its meaning.
    embedder.bring_up()
    villages = {owner: keep_village(app.store, owner) for owner in (LOCAL_OWNER, OTHER_OWNER)}
    for owner, village in villages.items():
        for concept in Concept:
            expected = village_places(village, concept)
            wait_for_meaning(app.index, chinese_question(concept), expected, owner)
    return villages


@given(
    query=st.one_of(
        st.sampled_from([*map(chinese_question, Concept), *VOCABULARY, "village", "Village notes"]),
        PLAIN_TEXT,
    )
)
@EXAMPLES
def test_a_search_finds_only_blocks_of_the_owners_library(
    app: App, two_villages: dict[str, dict[Concept, list[Kept]]], query: str
) -> None:
    for owner in two_villages:
        with app.store.library(owner) as kept:
            own_items = {item.id for item in kept.items()}
        found = app.index.search(owner, query, EVERY_BLOCK)
        assert {block.item for block in found} <= own_items, (
            f"a search of {owner}'s library for {query!r} found another's blocks: {shown(found)}"
        )


FERRY_CROSSING = [
    "Every morning the ferry leaves quillmoor before the sun rises.",
    "The ferry of quillmoor waits for late travellers until nine.",
]
SNOW_CROSSING = [
    "Every morning the snow covers marrowby before the sun rises.",
    "The snow of marrowby stays on the roofs until the spring.",
]


def judge_current_document(app: App, first: Kept, second: Kept) -> None:
    for query in ("quillmoor", "Filler paragraph", chinese_question(Concept.FERRY)):
        found = app.index.search(LOCAL_OWNER, query, EVERY_BLOCK)
        documents = {block.document for block in found if block.item == first.item}
        assert documents <= {second.document}, (
            f"a search for {query!r} found a document that a later reading replaced: {shown(found)}"
        )
    found = app.index.search(LOCAL_OWNER, "Filler paragraph", EVERY_BLOCK)
    assert second.document in {block.document for block in found}, (
        f"the document of the later reading was not found by its words: {shown(found)}"
    )


def test_a_document_that_a_later_reading_replaced_is_not_found(
    app: App, embedder: EmbeddingStandin
) -> None:
    embedder.bring_up()
    first = read_page(app, "crossing", "Crossing notes", FERRY_CROSSING)
    question = chinese_question(Concept.FERRY)
    wait_for_meaning(app.index, question, [first.place_of(text) for text in FERRY_CROSSING])
    second = read_page(app, "crossing", "Crossing notes", SNOW_CROSSING)
    assert (second.item, second.document != first.document) == (first.item, True), (
        "the page read again did not give its item another document"
    )
    judge_current_document(app, first, second)
    snow = [second.place_of(text) for text in SNOW_CROSSING]
    wait_for_meaning(app.index, chinese_question(Concept.SNOW), snow)
    judge_current_document(app, first, second)


# R6: when a block can be found.


@pytest.mark.parametrize("engine", ["away", "up", "slow"])
def test_a_reading_is_found_by_its_words_at_the_first_search_after_it_settled(
    app: App, embedder: EmbeddingStandin, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    # The index's rounds are put back far apart, so that nothing but the search itself can
    # bring the reading into the index.
    monkeypatch.setattr(search, "CATCH_UP_SECONDS", APART_SECONDS)
    time.sleep(2 * ROUND_SECONDS)
    if engine != "away":
        embedder.bring_up()
    if engine == "slow":
        embedder.hold("satamasta")
    kept = read_page(app, "harbour", "Harbour notes", HARBOUR)
    found = app.index.search(LOCAL_OWNER, "quillmoor", len(kept.blocks))
    assert set(places_found_by(found, FOUND_BY_WORDS)) == {
        kept.place_of(text) for text in HARBOUR
    }, (
        f"with the engine {engine}, a reading that settled was not found by its words at the "
        f"first search: {shown(found)}"
    )


ZORBEL = "The zorbel crossing was counted twice each winter by the keeper."


def test_a_block_without_a_vector_competes_by_some_of_its_words(
    app: App, embedder: EmbeddingStandin, village: dict[Concept, list[Kept]]
) -> None:
    embedder.hold("keeper")
    kept = read_page(app, "zorbel", "Crossing notes", [ZORBEL])
    query = f"zorbel {concept_word(Concept.FERRY, 'en')} lighthouse"
    found = app.index.search(LOCAL_OWNER, query, len(village_places(village, Concept.FERRY)) + 5)
    assert places_found_by(found, FOUND_BY_MEANING), (
        f"the blocks with vectors were not found by meaning for {query!r}: {shown(found)}"
    )
    assert kept.place_of(ZORBEL) in places_found_by(found, frozenset({FoundBy.SOME_WORDS})), (
        f"a block without a vector that holds some words of {query!r} was not found by them "
        f"among blocks found by meaning: {shown(found)}"
    )


# R7: a library that was there before the index.


def village_forms(village: dict[Concept, list[Kept]]) -> set[str]:
    # What the engine is given of the village's blocks: each under the title of its item.
    return {f"{kept.title}\n{kept.blocks[0]}" for kepts in village.values() for kept in kepts}


def test_a_library_made_before_the_index_is_indexed_when_the_service_starts(
    start_app: Callable[..., App], embedder: EmbeddingStandin, tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "data")
    village = keep_village(store)
    store.lock.release()
    embedder.bring_up()
    app = start_app()
    wait_until(
        lambda: village_forms(village) <= set(embedder.embedded()),
        lambda: (
            "the blocks of a library made before the index were not given to the engine "
            "without anyone asking"
        ),
    )
    for concept in Concept:
        wait_for_meaning(app.index, chinese_question(concept), village_places(village, concept))


# R10: kept in the library's file, the index's own to rebuild.


def test_a_vector_is_kept_only_for_the_text_it_was_made_of_when_an_item_is_read_again(
    app: App, embedder: EmbeddingStandin, village: dict[Concept, list[Kept]]
) -> None:
    embedder.hold("quillmoor")
    first = read_page(app, "crossing", "Crossing notes", FERRY_CROSSING)
    wait_until(
        lambda: any("quillmoor" in text for text in embedder.holding()),
        lambda: "the index did not ask the engine for the vectors of the first reading",
    )
    second = read_page(app, "crossing", "Crossing notes", SNOW_CROSSING)
    assert second.document != first.document, "the page read again gave the same document"
    embedder.let_go()
    snow = [*village_places(village, Concept.SNOW), *map(second.place_of, SNOW_CROSSING)]
    question = chinese_question(Concept.SNOW)
    wait_until(
        lambda: found_by_meaning_first(app.index, question, snow),
        lambda: (
            "a block of an item read again while the engine worked did not get the vector of "
            f"its own text: {shown(app.index.search(LOCAL_OWNER, question, len(snow)))}"
        ),
    )
    ferry = village_places(village, Concept.FERRY)
    found = app.index.search(LOCAL_OWNER, chinese_question(Concept.FERRY), len(ferry))
    assert set(places(found)) == set(ferry), (
        f"a block was found by the vector of a text it no longer holds: {shown(found)}"
    )


LANTERN = [
    f"The lantern of the {word} house burns {concept.value} at night."
    for word, concept in zip(VOCABULARY, [*Concept, *Concept, *Concept], strict=False)
]


def judge_every_block_once(kept: Kept, found: list[FoundBlock]) -> None:
    assert sorted(places(found)) == sorted(map(kept.place_of, LANTERN)), (
        f"a search for a word every block holds did not find each block once: {shown(found)}"
    )


def test_a_block_is_in_the_index_once_however_many_searches_race(app: App) -> None:
    kept = kept_document(app.store, LOCAL_OWNER, f"{SITE}/lantern", "Lanterns", paragraphs(LANTERN))
    with ThreadPoolExecutor(max_workers=16) as pool:
        raced = list(
            pool.map(lambda _: app.index.search(LOCAL_OWNER, "lantern", EVERY_BLOCK), range(16))
        )
    for found in [*raced, app.index.search(LOCAL_OWNER, "lantern", EVERY_BLOCK)]:
        judge_every_block_once(kept, found)


def index_tables(library_file: Path) -> list[str]:
    # The index's tables in the library's file, those that FTS5 keeps for a table of it left out.
    with sqlite3.connect(library_file) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?", (INDEX_TABLES,)
        ).fetchall()
    tables = [name for (name,) in names]
    return [
        table
        for table in tables
        if not any(table == f"{other}{shadow}" for other in tables for shadow in FTS_SHADOWS)
    ]


def damaged(library_file: Path, statement: str) -> None:
    with sqlite3.connect(library_file) as connection:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.execute(statement)


def test_an_index_thrown_away_in_whole_or_in_part_is_rebuilt_whole_at_start(
    start_app: Callable[..., App], embedder: EmbeddingStandin, tmp_path: Path
) -> None:
    embedder.bring_up()
    app = start_app()
    kept = kept_document(app.store, LOCAL_OWNER, f"{SITE}/lantern", "Lanterns", paragraphs(LANTERN))
    ferry = [kept.place_of(text) for text in LANTERN if Concept.FERRY.value in text]
    question = chinese_question(Concept.FERRY)
    wait_for_meaning(app.index, question, ferry)
    items = app.client.get("/api/items").json()
    library_file = tmp_path / "data" / LIBRARY_FILE
    tables = index_tables(library_file)
    assert tables, "the library's file holds no table of the index"
    damages = [
        *(f"DELETE FROM {table} WHERE rowid % 2 = 0" for table in tables),
        *(f"DROP TABLE {table}" for table in tables),
    ]
    for damage in damages:
        app.stop()
        damaged(library_file, damage)
        app = start_app()
        judge_every_block_once(kept, app.index.search(LOCAL_OWNER, "lantern", EVERY_BLOCK))
        wait_for_meaning(app.index, question, ferry)
        assert app.client.get("/api/items").json() == items, (
            f"throwing away the index ({damage}) lost what a reader made"
        )


def test_a_change_of_embedding_model_makes_every_vector_again(
    start_app: Callable[..., App], embedder: EmbeddingStandin
) -> None:
    embedder.bring_up()
    app = start_app()
    village = keep_village(app.store)
    for concept in Concept:
        wait_for_meaning(app.index, chinese_question(concept), village_places(village, concept))
    app.stop()
    embedder.hold("Village notes")
    app = start_app(embedding_model=OTHER_EMBEDDING_MODEL)
    found = app.index.search(LOCAL_OWNER, chinese_question(Concept.FERRY), EVERY_BLOCK)
    assert not places_found_by(found, FOUND_BY_MEANING), (
        f"a block was found by meaning by the vector of the earlier model: {shown(found)}"
    )
    embedder.let_go()
    wait_until(
        lambda: village_forms(village) <= set(embedder.embedded(OTHER_EMBEDDING_MODEL)),
        lambda: "the vectors were not made again when the embedding model changed",
    )
    for concept in Concept:
        wait_for_meaning(app.index, chinese_question(concept), village_places(village, concept))


@given(
    title=st.text(alphabet=st.characters(categories=["L", "N", "P", "Zs"]), max_size=60)
    .map(str.strip)
    .filter(bool),
    length=st.integers(min_value=9_000, max_value=12_000),
    letter=st.sampled_from(UNSPACED_SAMPLE),
)
@FEW_EXAMPLES
def test_a_block_is_given_to_the_engine_under_the_title_of_its_item(
    library_alone: tuple[LibraryStore, BlockIndex],
    embedder: EmbeddingStandin,
    title: str,
    length: int,
    letter: str,
) -> None:
    store, _ = library_alone
    embedder.bring_up()
    own = f"zq{uuid4().hex[:10]}"
    whole = f"The {own} clock strikes nine over the harbour."
    long = f"{own}{letter * length}"
    kept_document(store, LOCAL_OWNER, example_url(), title, paragraphs([whole, long]))

    def given() -> list[EmbeddingAsked]:
        return [asked for asked in embedder.asked() if any(own in text for text in asked.texts)]

    wait_until(
        lambda: sum(own in text for asked in given() for text in asked.texts) >= len([whole, long]),
        lambda: f"the blocks were not given to the engine: {given()}",
    )
    refused = [asked.status for asked in given() if asked.status != HTTPStatus.OK]
    assert not refused, f"the engine refused what the index gave it of the blocks: {refused}"
    texts = [text for asked in given() for text in asked.texts if own in text]
    assert f"{title}\n{whole}" in texts, f"a block was not given under its title: {texts}"
    cut = [text for text in texts if letter in text]
    whole_long = f"{title}\n{long}"
    assert cut and all(
        whole_long.startswith(text) and len(text) < len(whole_long) for text in cut
    ), f"a long block was not given under its title, cut to a bound: {[text[:80] for text in cut]}"
