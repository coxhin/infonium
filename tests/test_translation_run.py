import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

import pytest

from infonium import api
from infonium.document import (
    Block,
    Code,
    FormulaMark,
    Heading,
    Paragraph,
    Place,
    Table,
    TimePlace,
)
from infonium.intake import failure_reason
from infonium.language_model import WireApi, openai_model
from tests.model_standin import (
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STANDIN_MODEL,
    TRANSLATION_MODEL,
    Asked,
    Finish,
    Rejection,
    Reply,
    Responder,
)
from tests.service import LEDGER, REVISED, SITE, App
from tests.translation_standin import (
    RECORDING,
    TITLE,
    TRANSLATED,
    TRIES,
    Passage,
    asked_for,
    asks_passages,
    block_path,
    by_number,
    by_translating_model,
    first_replies,
    holding,
    kept,
    kept_document,
    long_paragraphs,
    marked,
    passage_requests,
    passages_asked,
    passages_reply,
    passages_sent,
    plain,
    quiet,
    settled,
    survey_reply,
    surveys,
    translated_whole,
    translating,
    translation,
    translation_path,
    wait_for,
)

# A refusal of the endpoint that the openai package does not try again by itself.
REFUSED_REQUEST = Rejection(
    status=HTTPStatus.BAD_REQUEST, body='{"error": {"message": "the request is refused"}}'
)
# A document long enough to take several requests, and one long enough that the requests it
# takes outnumber those a translation runs at once.
SEVERAL_REQUESTS_PARAGRAPHS = 40
MANY_REQUESTS_PARAGRAPHS = 150
# Terms of the survey, and the forms in which passages use them: as written, and inflected
# as Finnish inflects a name.
TERMS_USED = [("Quillmoor", "Quillmoor"), ("Phobos", "Phoboksen")]
TERM_TRANSLATION = "奎尔穆尔港"
# What the model that writes digests puts before a block it translates again.
AGAIN = "再"
NUMBER = re.compile(r"\d+")
# How long the run before a POST is given to go on, were it not ended.
QUIET_SECONDS_SHORT = 0.5


def paragraph_numbers(asked: Asked) -> list[int]:
    # The numbers of the long paragraphs whose passages a request carries; a request of
    # another kind carries none.
    passages = passages_asked(asked) if asks_passages(asked) else []
    return [
        int(plain(passage.text).split()[1])
        for passage in passages
        if plain(passage.text).startswith("Paragraph ")
    ]


def refused_reason(app: App) -> str:
    # How intake.failure_reason words the failure the model's interface raises for a refused
    # request.
    app.model.reset(lambda asked: REFUSED_REQUEST)
    try:
        openai_model(name=TRANSLATION_MODEL, wire_api=WireApi.RESPONSES).structured(
            "Translate.", "Nothing.", Paragraph
        )
    except Exception as raised:
        return failure_reason(raised)
    pytest.fail("the model's interface took a refused request for a reply")


# R5: requests.


def test_a_request_carries_several_passages_the_title_and_the_text_around_them(app: App) -> None:
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    app.model.reset(translating())
    document = kept(app, blocks)
    translated_whole(app, document.item_id)
    requests = passage_requests(app)
    assert len(requests) > 1, (
        f"a document of {len(blocks)} paragraphs of 80 words was sent in one request, past any "
        "bound on a request's size"
    )
    for asked in requests:
        carried = paragraph_numbers(asked)
        assert carried, "a request carries no paragraph of the document"
        assert TITLE in asked.text(), "a request does not carry the document's title"
        if min(carried) > 0:
            assert f"ends {min(carried) - 1}." in asked.text(), (
                f"the request for paragraphs {carried} does not carry the end of the paragraph "
                "before them"
            )
        if max(carried) < len(blocks) - 1:
            assert f"Paragraph {max(carried) + 1} begins" in asked.text(), (
                f"the request for paragraphs {carried} does not carry the start of the paragraph "
                "after them"
            )
    assert all(len(paragraph_numbers(asked)) > 1 for asked in requests[:-1]), (
        "a request other than the last carries one paragraph, not several"
    )


def test_requests_of_one_document_run_several_at_a_time_and_no_more_than_a_bound(
    app: App,
) -> None:
    gate = threading.Event()
    app.model.reset(holding(translating(), gate, asks_passages))
    document = kept(app, long_paragraphs(MANY_REQUESTS_PARAGRAPHS))
    asked_for(app, document.item_id)
    wait_for(lambda: len(passage_requests(app)) > 0, "the first request for passages")
    quiet(app)
    at_once = len(passage_requests(app))
    gate.set()
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated", (
        f"the translation did not end: {view['progress']}"
    )
    every = len(passage_requests(app))
    assert 1 < at_once < every, (
        f"of the {every} requests of a document, {at_once} ran at once: not several, or no bound"
    )


def wrong_reply(kind: str) -> Responder:
    # A reply that cannot be used for any block of a request of two blocks.
    def respond(asked: Asked) -> Reply | Rejection:
        passages = passages_asked(asked)
        match kind:
            case "refused":
                return REFUSED_REQUEST
            case "failed":
                return Reply(pieces=['{"blocks": ['], finish=Finish.FAILED)
            case "cut short":
                return Reply(pieces=['{"blocks": ['], finish=Finish.CUT_SHORT)
            case "unparsable":
                return Reply(pieces=["The ferry left at nine."])
            case _:
                moved = passages[-1].model_copy(update={"block": passages[0].block})
                return passages_reply([*passages[:-1], moved], marked)

    return respond


WRONG_REPLIES = (
    "refused",
    "failed",
    "cut short",
    "unparsable",
    "a passage under another block",
)


@pytest.mark.parametrize("kind", WRONG_REPLIES)
@pytest.mark.parametrize("wrong", [1, TRIES])
def test_a_request_is_tried_three_times(app: App, kind: str, wrong: int) -> None:
    blocks: list[Block] = [
        Paragraph(text="harbour ledger quorvex", marks=[]),
        Paragraph(text="zindlap évier straße", marks=[]),
    ]
    app.model.reset(first_replies(wrong, wrong_reply(kind), translating()))
    document = kept(app, blocks)
    asked_for(app, document.item_id)
    view = settled(app, document.item_id)
    requests = len(passage_requests(app))
    if wrong < TRIES:
        assert (view["progress"]["kind"], requests) == ("translated", wrong + 1), (
            f"a request answered {kind} {wrong} times was not tried again until it was answered: "
            f"{requests} requests, {view['progress']}"
        )
        assert set(by_number(view)) == {0, 1}, "a request tried again did not keep its blocks"
    else:
        assert requests == TRIES, (
            f"a request answered {kind} every time was tried {requests} times, not {TRIES}"
        )
        assert view["progress"]["kind"] == "stopped" and view["blocks"] == [], (
            f"a request that failed {TRIES} times did not stop the translation with nothing kept "
            f"of it: {view['progress']}, {len(view['blocks'])} blocks kept"
        )


# A request of two blocks whose reply answers the paragraph rightly and the table wrongly:
# the table's cells, and what the reply puts before a cell's answer when it is wrong.
CELLS = ("zindlap", "évier")
WRONGLY = "错"
PARTLY_WRONG = ("a cell's passage missing", "a passage not asked")


def partly_wrong(kind: str) -> Responder:
    def wording(passage: Passage) -> str:
        return f"{WRONGLY}{passage.text}" if plain(passage.text) in CELLS else marked(passage)

    def respond(asked: Asked) -> Reply | Rejection:
        passages = passages_asked(asked)
        cells = [passage for passage in passages if plain(passage.text) in CELLS]
        match kind:
            case "a cell's passage missing":
                return passages_reply([one for one in passages if one != cells[-1]], wording)
            case _:
                stray = cells[-1].model_copy(update={"number": passages[-1].number + 7})
                return passages_reply([*passages, stray], wording)

    return respond


@pytest.mark.parametrize("kind", PARTLY_WRONG)
@pytest.mark.parametrize("wrong", [1, TRIES])
def test_a_replys_blocks_are_judged_one_by_one_and_only_the_wrong_asked_again(
    app: App, kind: str, wrong: int
) -> None:
    paragraph = Paragraph(text="harbour ledger quorvex", marks=[])
    blocks: list[Block] = [paragraph, Table(text="\t".join(CELLS))]
    app.model.reset(first_replies(wrong, partly_wrong(kind), translating()))
    document = kept(app, blocks)
    asked_for(app, document.item_id)
    view = settled(app, document.item_id)
    requests = passage_requests(app)
    first, *again = requests
    assert {plain(passage.text) for passage in passages_asked(first)} == {paragraph.text, *CELLS}, (
        "the first request does not carry both blocks"
    )
    kept_paragraph = [segment["text"] for segment in by_number(view).get(0, {}).get("segments", [])]
    assert kept_paragraph == [f"{TRANSLATED}{paragraph.text}"], (
        f"a block that a reply with {kind} answered rightly was not kept from that reply"
    )
    for asked in again:
        assert sorted(plain(passage.text) for passage in passages_asked(asked)) == sorted(CELLS), (
            f"a request asked again after a reply with {kind} does not carry only the block still "
            "missing"
        )
    kept_texts = [segment["text"] for block in view["blocks"] for segment in block["segments"]]
    assert not any(WRONGLY in text for text in kept_texts), (
        f"something of a block that a reply with {kind} answered wrongly was kept"
    )
    if wrong < TRIES:
        assert (view["progress"]["kind"], len(requests)) == ("translated", wrong + 1), (
            f"the block answered wrongly {wrong} times was not asked again until it was answered: "
            f"{len(requests)} requests, {view['progress']}"
        )
    else:
        assert (view["progress"]["kind"], len(requests), set(by_number(view))) == (
            "stopped",
            TRIES,
            {0},
        ), (
            f"a block answered wrongly every time was asked {len(requests)} times, not {TRIES}, "
            f"or the translation did not stop with it missing: {view['progress']}"
        )


@pytest.mark.parametrize("wrong", [1, TRIES])
def test_the_survey_is_tried_three_times(app: App, wrong: int) -> None:
    answered = iter(range(TRIES * 2))

    def respond(asked: Asked) -> Reply | Rejection:
        if not asks_passages(asked) and next(answered) < wrong:
            return REFUSED_REQUEST
        return translating()(asked)

    app.model.reset(respond)
    document = kept(app, [Paragraph(text="harbour ledger quorvex", marks=[])])
    asked_for(app, document.item_id)
    view = settled(app, document.item_id)
    tried = len(surveys(app))
    if wrong < TRIES:
        assert (tried, view["progress"]["kind"]) == (wrong + 1, "translated"), (
            f"a survey refused {wrong} times was not tried again until it was answered: "
            f"{tried} surveys, {view['progress']}"
        )
    else:
        assert (tried, view["progress"]["kind"]) == (TRIES, "stopped"), (
            f"a survey refused every time was tried {tried} times and left the translation "
            f"{view['progress']}, where it is tried {TRIES} times and the translation stops"
        )
        assert passage_requests(app) == [], "passages were sent though the survey failed"


@pytest.mark.parametrize(("term", "used"), TERMS_USED)
def test_a_request_carries_the_terms_its_passages_use_in_any_form(
    app: App, term: str, used: str
) -> None:
    def mentioning(number: int) -> str:
        return f"{used} " if number % 6 == 0 else ""

    app.model.reset(translating(survey=survey_reply(terms={term: TERM_TRANSLATION})))
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS, mentioning))
    translated_whole(app, document.item_id)
    using = [
        asked
        for asked in passage_requests(app)
        if any(used in passage.text for passage in passages_asked(asked))
    ]
    assert using, f"no request carried a passage that uses {used}"
    for asked in using:
        assert TERM_TRANSLATION in asked.text(), (
            f"a request whose passages use {term} as {used} does not carry the translation the "
            "survey gave the term"
        )


def recording(texts: list[str]) -> Paragraph:
    places: list[Place] = []
    end = -1
    for number, text in enumerate(texts):
        start = end + 1
        end = start + len(text)
        places.append(TimePlace(start=start, end=end, moment=number * 2.0, duration=1.5))
    return Paragraph(text=" ".join(texts), marks=[], places=places)


SPOKEN = ["The ferry left at nine.", "The keeper wrote it down.", "Fog lay over the harbour."]
SEGMENT_REPLIES: dict[str, Callable[[Passage], str]] = {
    "unlike segments answered alike": lambda passage: "渡船",
    "a segment answered out of all proportion": lambda passage: (
        " ".join([passage.text] * 60) if passage.text.startswith("The keeper") else passage.text
    ),
}


@pytest.mark.parametrize("kind", sorted(SEGMENT_REPLIES))
def test_a_reply_that_answers_a_recordings_segments_out_of_kind_is_asked_again(
    app: App, kind: str
) -> None:
    app.model.reset(first_replies(1, translating(SEGMENT_REPLIES[kind]), translating()))
    document = kept(app, [recording(SPOKEN)], media_type=RECORDING)
    [block] = translated_whole(app, document.item_id)["blocks"]
    assert [segment["text"] for segment in block["segments"]] == [
        f"{TRANSLATED}{text}" for text in SPOKEN
    ], f"a reply with {kind} was kept: {block['segments']}"
    assert len(passage_requests(app)) == 2, f"a reply with {kind} was not asked for again"


ONE_PASSAGE_REPLIES: dict[str, tuple[list[Block], Callable[[Passage], str]]] = {
    "a long heading answered in four characters": (
        [Heading(level=1, text="The harbour ledger of the town of Quillmoor", marks=[])],
        lambda passage: "港口账簿",
    ),
    "a table's cells answered in two characters": (
        [Table(text="The ferry that leaves at night\tThe keeper of the harbour ledger")],
        lambda passage: "渡船",
    ),
    "a paragraph answered at length": (
        [Paragraph(text="Fog.", marks=[])],
        lambda passage: " ".join(["浓雾笼罩着东边的防波堤"] * 20),
    ),
    "unlike paragraphs answered alike": (
        [Paragraph(text="The ferry left.", marks=[]), Paragraph(text="The boat left.", marks=[])],
        lambda passage: "船开走了。",
    ),
}


@pytest.mark.parametrize("kind", sorted(ONE_PASSAGE_REPLIES))
def test_a_block_of_one_passage_is_never_refused_for_the_length_or_likeness_of_its_answer(
    app: App, kind: str
) -> None:
    blocks, wording = ONE_PASSAGE_REPLIES[kind]
    app.model.reset(translating(wording))
    document = kept(app, blocks)
    view = translated_whole(app, document.item_id)
    assert len(view["blocks"]) == len(blocks), f"a reply with {kind} was not kept"
    assert len(passage_requests(app)) == 1, f"a reply with {kind} was asked for again"


# R6: a document in the reader's language already.


def test_a_document_the_survey_finds_in_the_readers_language_is_unneeded(app: App) -> None:
    app.model.reset(translating(survey=survey_reply(written_in_asked_language=True)))
    document = kept(app, [Paragraph(text="港口的账簿记下了每一班渡船。", marks=[])])
    asked_for(app, document.item_id)
    view = settled(app, document.item_id)
    assert (view["progress"], view["blocks"]) == ({"kind": "unneeded"}, []), (
        f"a document the survey found in the reader's language is not Unneeded: {view['progress']}"
    )
    again = asked_for(app, document.item_id)
    quiet(app)
    assert again["progress"] == {"kind": "unneeded"} and passage_requests(app) == [], (
        "PUT on an Unneeded translation changed it or sent passages to the model"
    )
    assert len(surveys(app)) == 1, "PUT on an Unneeded translation asked the survey again"


def test_post_on_an_unneeded_translation_translates_without_asking_again(app: App) -> None:
    app.model.reset(translating(survey=survey_reply(written_in_asked_language=True)))
    document = kept(app, [Paragraph(text="港口的账簿记下了每一班渡船。", marks=[])])
    asked_for(app, document.item_id)
    assert settled(app, document.item_id)["progress"] == {"kind": "unneeded"}, "not Unneeded"
    anew = app.client.post(translation_path(document.item_id))
    assert anew.status_code == HTTPStatus.OK, f"POST on an Unneeded translation failed: {anew.text}"
    view = settled(app, document.item_id)
    assert view["progress"] == {"kind": "translated"} and set(by_number(view)) == {0}, (
        f"POST on an Unneeded translation did not translate the document: {view['progress']}"
    )
    assert len(surveys(app)) == 1, "POST on an Unneeded translation asked the survey again"


# R7: the endpoints.


def test_get_starts_nothing(app: App) -> None:
    app.model.reset(translating())
    document = kept(app, [Paragraph(text="harbour ledger", marks=[])])
    view = translation(app, document.item_id)
    assert view == {
        "document": document.document_id,
        "language": api.settings.language,
        "progress": {"kind": "unasked"},
        "input_tokens": 0,
        "output_tokens": 0,
        "blocks": [],
    }, f"a translation nobody asked for is not Unasked and empty: {view}"
    quiet(app)
    assert app.model.asked() == [], "GET of a translation sent a request to the model"


def test_put_answers_at_once_and_changes_nothing_while_translating_or_translated(
    app: App,
) -> None:
    gate = threading.Event()
    app.model.reset(holding(translating(), gate, lambda asked: True))
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(app, blocks)
    started = asked_for(app, document.item_id)
    answered_at_once = not gate.is_set()
    assert answered_at_once and started["progress"] == {
        "kind": "translating",
        "translated": 0,
        "total": len(blocks),
    }, f"PUT did not answer at once with Translating: {started['progress']}"
    again = asked_for(app, document.item_id)
    assert again["progress"]["kind"] == "translating", "PUT while translating changed the progress"
    gate.set()
    translated = settled(app, document.item_id)
    assert translated["progress"]["kind"] == "translated", "the translation did not end"
    sent = app.model.asked()
    done = asked_for(app, document.item_id)
    quiet(app)
    assert done == translated and app.model.asked() == sent, (
        "PUT on a translated document changed it or sent requests to the model"
    )
    assert len(surveys(app)) == 1 and set(passages_sent(app).values()) == {1}, (
        "PUT while translating started the translation again"
    )


def test_a_block_is_served_as_soon_as_it_is_kept(app: App) -> None:
    gate = threading.Event()

    def later(asked: Asked) -> bool:
        return asks_passages(asked) and 0 not in paragraph_numbers(asked)

    app.model.reset(holding(translating(), gate, later))
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(app, blocks)
    asked_for(app, document.item_id)
    try:
        wait_for(lambda: bool(translation(app, document.item_id)["blocks"]), "a kept block")
        view = translation(app, document.item_id)
    finally:
        gate.set()
    assert view["progress"]["kind"] == "translating", "the translation ended with requests held"
    assert view["progress"] == {
        "kind": "translating",
        "translated": len(view["blocks"]),
        "total": len(blocks),
    }, f"Translating does not count the blocks kept: {view['progress']}"
    assert settled(app, document.item_id)["progress"]["kind"] == "translated", "did not end"


def test_post_discards_what_was_translated_and_ends_the_run_before_it(app: App) -> None:
    old, new = "旧", "新"
    gate = threading.Event()
    app.model.reset(
        holding(translating(lambda passage: f"{old}{passage.text}"), gate, asks_passages)
    )
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(app, blocks)
    asked_for(app, document.item_id)
    wait_for(lambda: len(passage_requests(app)) > 0, "the old run's first request")
    quiet(app)
    # From here the stand-in counts afresh: what it is asked is asked after the old run's
    # requests, which it holds.
    app.model.reset(translating(lambda passage: f"{new}{passage.text}"))
    anew = app.client.post(translation_path(document.item_id))
    assert anew.status_code == HTTPStatus.OK, f"POST failed: {anew.text}"
    assert (
        anew.json()["progress"]
        == {
            "kind": "translating",
            "translated": 0,
            "total": len(blocks),
        }
        and anew.json()["blocks"] == []
    ), f"POST did not start from the first block: {anew.json()}"
    time.sleep(QUIET_SECONDS_SHORT)
    gate.set()
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated", f"the new run did not end: {view['progress']}"
    texts = [block["segments"][0]["text"] for block in view["blocks"]]
    assert len(texts) == len(blocks) and all(text.startswith(new) for text in texts), (
        "a block of the run before POST was kept after it"
    )
    quiet(app)
    after = passages_sent(app)
    assert sorted(after) == sorted(block.text for block in blocks), (
        "after POST, a block was not sent to the model"
    )
    assert set(after.values()) == {1}, (
        "after POST, a passage was sent more than once: the run before it went on"
    )


def test_a_failed_passage_leaves_its_blocks_and_put_goes_on_with_them(app: App) -> None:
    def failing(asked: Asked) -> Reply | Rejection:
        if 20 in paragraph_numbers(asked):
            return REFUSED_REQUEST
        return translating()(asked)

    app.model.reset(failing)
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(app, blocks)
    asked_for(app, document.item_id)
    stopped = settled(app, document.item_id)
    kept_numbers = set(by_number(stopped))
    failed = {
        number
        for asked in passage_requests(app)
        if 20 in paragraph_numbers(asked)
        for number in paragraph_numbers(asked)
    }
    missing = len(blocks) - len(kept_numbers)
    assert stopped["progress"]["kind"] == "stopped", (
        f"a translation with blocks missing did not end Stopped: {stopped['progress']}"
    )
    assert kept_numbers == set(range(len(blocks))) - failed, (
        "a passage that failed did not leave only its own blocks untranslated: the run did not "
        "go on with the rest"
    )
    reason = stopped["progress"]["reason"]
    counts = {int(number) for number in NUMBER.findall(reason)}
    assert {missing, len(blocks)} <= counts and refused_reason(app) in reason, (
        f"the reason does not say that {missing} of {len(blocks)} blocks could not be translated "
        f"and why the last failed, as intake.failure_reason words it: {reason!r}"
    )
    app.model.reset(translating())
    asked_for(app, document.item_id)
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated", f"PUT did not go on: {view['progress']}"
    again = passages_sent(app)
    assert set(again) == {blocks[number].text for number in failed}, (
        "PUT on a stopped translation did not ask for exactly the missing blocks"
    )
    assert {number: by_number(view)[number] for number in kept_numbers} == by_number(stopped), (
        "a block kept before the translation stopped was changed when it went on"
    )


def test_the_tokens_are_what_the_translation_took_a_block_translated_again_included(
    app: App,
) -> None:
    app.model.reset(translating())
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS // 2))
    view = translated_whole(app, document.item_id)
    taken = len(app.model.asked())
    assert (view["input_tokens"], view["output_tokens"]) == (
        INPUT_TOKENS * taken,
        OUTPUT_TOKENS * taken,
    ), (
        f"the tokens are not those of the {taken} requests the translation made: "
        f"{view['input_tokens']} in, {view['output_tokens']} out"
    )
    assert app.client.post(block_path(document.item_id, 1)).status_code == HTTPStatus.OK
    again = translation(app, document.item_id)
    assert (again["input_tokens"], again["output_tokens"]) == (
        INPUT_TOKENS * (taken + 1),
        OUTPUT_TOKENS * (taken + 1),
    ), "the tokens do not count a block translated again"


@pytest.mark.parametrize("item_path", ["/no-such-page", None])
def test_an_item_without_a_document_has_no_translation(app: App, item_path: str | None) -> None:
    item_id = "0" * 64
    if item_path is not None:
        item_id = app.link(f"{SITE}{item_path}").json()["id"]
        assert app.settled(item_id)["reading"]["kind"] == "unread", "a missing page was read"
    app.model.reset(translating())
    path = translation_path(item_id)
    refused = [
        app.client.get(path),
        app.client.put(path),
        app.client.post(path),
        app.client.post(block_path(item_id, 0)),
    ]
    assert [response.status_code for response in refused] == [HTTPStatus.NOT_FOUND] * 4, (
        "an item without a document was not answered 404 on its translation"
    )
    for response in refused:
        assert isinstance(response.json()["detail"], str), "a refusal is not a Failure"
    assert app.model.asked() == [], "the model was asked about an item without a document"


# R8: asked twice at once.


def test_a_translation_asked_for_many_times_at_once_is_started_once(app: App) -> None:
    app.model.reset(translating())
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(app, blocks)
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: asked_for(app, document.item_id), range(8)))
    assert {answer["progress"]["kind"] for answer in answers} <= {"translating", "translated"}
    assert settled(app, document.item_id)["progress"]["kind"] == "translated", "did not end"
    quiet(app)
    sent = passages_sent(app)
    assert sorted(sent) == sorted(block.text for block in blocks), "a block was not requested"
    assert set(sent.values()) == {1} and len(surveys(app)) == 1, (
        f"a translation asked for at once was started more than once: passages sent "
        f"{max(sent.values())} times, {len(surveys(app))} surveys"
    )


# R9: a service that stops.


def test_a_translation_under_way_goes_on_after_a_restart_without_translating_again(
    start_app: Callable[..., App],
) -> None:
    first = start_app()
    gate = threading.Event()

    def later(asked: Asked) -> bool:
        return asks_passages(asked) and 0 not in paragraph_numbers(asked)

    # The requests held when the service stops end as a failure once the test is done with
    # them, so that the service that stopped keeps nothing of them.
    held = Reply(pieces=['{"blocks": ['], finish=Finish.FAILED)
    app_responder = translating()
    first.model.reset(
        lambda asked: (
            held.model_copy(update={"gate": gate}) if later(asked) else app_responder(asked)
        )
    )
    blocks = long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)
    document = kept(first, blocks)
    asked_for(first, document.item_id)
    wait_for(lambda: bool(translation(first, document.item_id)["blocks"]), "a kept block")
    kept_before = by_number(translation(first, document.item_id))
    first.stop()
    try:
        first.model.reset(translating())
        again = start_app()
        view = settled(again, document.item_id)
        resumed = passages_sent(again)
    finally:
        first.model.reset(lambda asked: REFUSED_REQUEST)
        gate.set()
    assert view["progress"]["kind"] == "translated", (
        f"a translation under way when the service stopped did not go on when it started: "
        f"{view['progress']}"
    )
    assert not set(resumed) & {blocks[number].text for number in kept_before}, (
        "a block kept before the service stopped was translated again after it started"
    )
    assert {number: by_number(view)[number] for number in kept_before} == kept_before, (
        "a block kept before the service stopped was changed after it started"
    )


# R10: one block again.


def again_or_first(asked: Asked) -> Reply | Rejection:
    if asks_passages(asked) and not by_translating_model(asked):
        return translating(lambda passage: f"{AGAIN}{passage.text}")(asked)
    return translating()(asked)


def test_one_block_again_is_translated_by_the_digest_model_with_its_context_and_kept(
    app: App,
) -> None:
    app.model.reset(again_or_first)
    blocks = long_paragraphs(5)
    document = kept(app, blocks)
    before = by_number(translated_whole(app, document.item_id))
    sent = len(app.model.asked())
    again = app.client.post(block_path(document.item_id, 2))
    assert again.status_code == HTTPStatus.OK, f"a block was not translated again: {again.text}"
    [asked] = app.model.asked()[sent:]
    assert asked.body["model"] == STANDIN_MODEL, (
        f"a block was translated again by {asked.body['model']}, not by settings.model"
    )
    assert [plain(passage.text) for passage in passages_asked(asked)] == [blocks[2].text], (
        "the request to translate one block again does not carry exactly that block"
    )
    assert "ends 1." in asked.text() and "Paragraph 3 begins" in asked.text(), (
        "the request to translate one block again does not carry the text around it"
    )
    expected = {
        "block_number": 2,
        "model": STANDIN_MODEL,
        "segments": [
            {
                "start": 0,
                "end": len(blocks[2].text),
                "text": f"{AGAIN}{blocks[2].text}",
                "marks": [],
            }
        ],
    }
    assert again.json() == expected, f"the block translated again is not returned: {again.json()}"
    after = by_number(translation(app, document.item_id))
    assert after == {**before, 2: expected}, (
        "the block translated again was not kept in place of its earlier translation alone"
    )


@pytest.mark.parametrize("standing", ["unasked", "unneeded"])
def test_one_block_again_is_refused_where_no_translation_was_asked_for(
    app: App, standing: str
) -> None:
    survey = survey_reply(written_in_asked_language=standing == "unneeded")
    app.model.reset(translating(survey=survey))
    document = kept(app, [Paragraph(text="harbour ledger", marks=[])])
    if standing == "unneeded":
        asked_for(app, document.item_id)
        assert settled(app, document.item_id)["progress"]["kind"] == "unneeded", "not Unneeded"
    sent = len(app.model.asked())
    refused = app.client.post(block_path(document.item_id, 0))
    assert refused.status_code == HTTPStatus.CONFLICT, (
        f"one block again on a translation that is {standing} was answered {refused.status_code}"
    )
    assert isinstance(refused.json()["detail"], str), "the refusal is not a Failure"
    view = translation(app, document.item_id)
    assert len(app.model.asked()) == sent and view["blocks"] == [], (
        f"one block again on a translation that is {standing} asked the model or kept something"
    )
    assert view["progress"]["kind"] == standing, "a refused request changed the progress"


UNTRANSLATED_BLOCKS: list[Block] = [
    Paragraph(text="harbour ledger", marks=[]),
    Code(text="print('harbour')"),
    Paragraph(text="a+b=c", marks=[FormulaMark(start=0, end=5)]),
    Paragraph(text="12 — 34 %", marks=[]),
]


@pytest.mark.parametrize("block_number", [1, 2, 3, len(UNTRANSLATED_BLOCKS), 99])
def test_one_block_again_is_404_for_a_block_never_translated_or_not_held(
    app: App, block_number: int
) -> None:
    app.model.reset(translating())
    document = kept(app, UNTRANSLATED_BLOCKS)
    translated_whole(app, document.item_id)
    sent = len(app.model.asked())
    refused = app.client.post(block_path(document.item_id, block_number))
    assert refused.status_code == HTTPStatus.NOT_FOUND, (
        f"block {block_number}, never translated or not in the document, was answered "
        f"{refused.status_code}"
    )
    assert isinstance(refused.json()["detail"], str), "the refusal is not a Failure"
    assert len(app.model.asked()) == sent, "the model was asked for a block that is not translated"


@pytest.mark.parametrize(
    "kind", ["refused", "failed", "cut short", "unparsable", "a passage missing"]
)
def test_one_block_again_answers_502_when_the_model_fails_and_keeps_the_earlier(
    app: App, kind: str
) -> None:
    app.model.reset(translating())
    blocks: list[Block] = [
        Paragraph(text="harbour ledger quorvex", marks=[]),
        Paragraph(text="zindlap évier straße", marks=[]),
    ]
    document = kept(app, blocks)
    before = translated_whole(app, document.item_id)
    wrong = wrong_reply(kind)

    def respond(asked: Asked) -> Reply | Rejection:
        if kind == "a passage missing":
            return passages_reply([], lambda passage: "")
        return wrong(asked)

    app.model.reset(respond)
    failed = app.client.post(block_path(document.item_id, 0))
    assert failed.status_code == HTTPStatus.BAD_GATEWAY, (
        f"a reply that is {kind} for one block again was answered {failed.status_code}"
    )
    assert isinstance(failed.json()["detail"], str), "the refusal is not a Failure"
    after = translation(app, document.item_id)
    assert after["blocks"] == before["blocks"], "a failed block again changed what was kept"


def test_a_translation_under_way_never_writes_over_a_block_kept_again(app: App) -> None:
    gate = threading.Event()
    app.model.reset(holding(again_or_first, gate, by_translating_model))
    blocks: list[Block] = [Paragraph(text="harbour ledger quorvex", marks=[])]
    document = kept(app, blocks)
    # The survey too is held, and so the run waits; the block is translated again meanwhile,
    # which a translation that is asked for allows.
    asked_for(app, document.item_id)
    try:
        wait_for(lambda: len(app.model.asked()) > 0, "the run's first request")
        again = app.client.post(block_path(document.item_id, 0))
    finally:
        gate.set()
    assert again.status_code == HTTPStatus.OK, f"a block was not translated again: {again.text}"
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated", f"the run did not end: {view['progress']}"
    assert [block["segments"][0]["text"] for block in view["blocks"]] == [
        f"{AGAIN}{blocks[0].text}"
    ], "the run under way wrote over the block translated again"


# R12: kept in the library.


def test_a_translation_is_kept_by_language_and_survives_a_restart(
    start_app: Callable[..., App],
) -> None:
    first = start_app()
    first.model.reset(translating())
    document = kept(first, long_paragraphs(3))
    done = translated_whole(first, document.item_id)
    first.stop()
    other = start_app(language="Suomi")
    elsewhere = translation(other, document.item_id)
    assert (elsewhere["language"], elsewhere["progress"], elsewhere["blocks"]) == (
        "Suomi",
        {"kind": "unasked"},
        [],
    ), "a translation into one language shows in another"
    other.stop()
    again = start_app()
    assert translation(again, document.item_id) == done, "a translation did not survive a restart"


def test_a_translation_belongs_to_its_owner(app: App) -> None:
    app.model.reset(translating())
    blocks = long_paragraphs(2)
    document = kept(app, blocks)
    translated_whole(app, document.item_id)
    stranger = "another-owner"
    theirs = kept_document_of(app, stranger, blocks)
    api.app.dependency_overrides[api.current_owner] = lambda: stranger
    try:
        foreign = app.client.get(translation_path(document.item_id))
        own = translation(app, theirs)
    finally:
        api.app.dependency_overrides.clear()
    assert foreign.status_code == HTTPStatus.NOT_FOUND, "another owner reached a translation"
    assert (own["progress"], own["blocks"]) == ({"kind": "unasked"}, []), (
        "another owner's item of the same page shares its translation"
    )


def kept_document_of(app: App, owner: str, blocks: list[Block]) -> str:

    return kept_document(app.store, "shared-page", TITLE, blocks, owner=owner).item_id


def test_a_translation_changes_nothing_of_the_item_its_document_and_digest(app: App) -> None:
    app.model.reset(translating())
    item_id = app.read("/ledger")
    paths = [f"/api/items/{item_id}", f"/api/items/{item_id}/document"]
    digest = app.client.put(f"/api/items/{item_id}/digest").json()
    before = [app.client.get(path).json() for path in paths]
    translated_whole(app, item_id)
    after = [app.client.get(path).json() for path in paths]
    assert (after, app.client.put(f"/api/items/{item_id}/digest").json()) == (before, digest), (
        "translating an item changed its entry, document or digest"
    )


def test_a_document_read_again_has_its_own_translation_and_the_old_run_stops(
    start_app: Callable[..., App],
) -> None:
    app = start_app()
    gate = threading.Event()
    app.model.reset(holding(translating(), gate, by_translating_model))
    item_id = app.read("/revised")
    old_document = app.client.get(f"/api/items/{item_id}/document").json()["id"]
    asked_for(app, item_id)
    try:
        wait_for(lambda: len(surveys(app)) > 0, "the survey of the first document")
        app.link(f"{SITE}/revised")
        reread = app.settled(item_id)
    finally:
        gate.set()
    assert reread["reading"]["document"] != old_document, "the page was not read anew"
    view = translation(app, item_id)
    assert (view["document"], view["progress"], view["blocks"]) == (
        reread["reading"]["document"],
        {"kind": "unasked"},
        [],
    ), f"the document read again does not have a translation of its own: {view}"
    quiet(app)
    old_passages = {text for text in LEDGER} & set(passages_sent(app))
    assert not old_passages, "the translation of the document read over went on after the reading"
    app.stop()
    sent = len(app.model.asked())
    again = start_app()
    quiet(again)
    assert len(again.model.asked()) == sent, (
        "the translation of a document read over was taken up when the service started"
    )
    assert REVISED[0] not in passages_sent(again), "the new document was translated unasked"
