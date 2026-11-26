import json
import time
from collections.abc import Callable
from http import HTTPStatus
from itertools import pairwise
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from infonium import api, model_connection
from infonium.digest import DraftDigest
from infonium.language_model import FORM_TOLD, JSON_OBJECT, reply_schema
from infonium.library import LOCAL_OWNER
from infonium.library_tools import SEARCH_TOOL
from infonium.rendition import Reply
from infonium.shelf import (
    EARLIER_MODEL,
    Running,
    TranslationRecord,
    TranslationShelf,
    Unsurveyed,
)
from infonium.survey import DocumentSurvey
from tests.conversation import asking
from tests.model_standin import (
    GLM_CHAT_PATH,
    GLM_KEY,
    MODEL_KEY,
    RESPONSES_PATH,
    STANDIN_MODEL,
    TOO_OFTEN,
    TRANSLATION_MODEL,
    Asked,
    Call,
    Rejection,
    Responder,
)
from tests.model_standin import Reply as StreamedReply
from tests.service import OVERVIEW, App, digest_reply
from tests.translation_standin import (
    TITLE,
    asks_passages,
    block_path,
    by_number,
    form_name,
    is_survey,
    kept,
    long_paragraphs,
    passages_asked,
    plain,
    quiet,
    settled,
    translating,
    translation,
    translation_path,
)

MODELS_PATH = "/api/models"
LIBRARY_CONVERSATION = "/api/conversation"
NOT_OFFERED = "no-such-model"
QUESTION = "When was the quay extended?"
ANSWER = json.dumps({"statements": [{"text": "In 1901.", "citations": []}]})
# A document long enough to take several requests.
SEVERAL_REQUESTS_PARAGRAPHS = 40
# The paragraph whose request the first model is refused, so that its translation stops with
# the blocks of the other requests kept.
REFUSED_PARAGRAPH = SEVERAL_REQUESTS_PARAGRAPHS - 1
REFUSED_REQUEST = Rejection(
    status=HTTPStatus.BAD_REQUEST, body='{"error": {"message": "the request is refused"}}'
)
# A pace of GLM's endpoint quick enough for a test, and how much earlier than its turn a
# request may arrive at the stand-in, for the time the connection takes.
PACED_REQUESTS = 600
EARLY_ARRIVAL = 0.03
# How long GLM's endpoint refuses every request, and the first pause after a refusal: the
# pauses, doubling, outlast the refusals before a request has been tried three times.
BUSY_SECONDS = 1.8
REFUSAL_PAUSE = 0.3


def glm_model() -> str:
    return api.settings.glm_models[0]


def glm_flash() -> str:
    return api.settings.glm_models[1]


def with_glm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.settings, "glm_api_key", SecretStr(GLM_KEY))


def answering(asked: Asked) -> StreamedReply | Rejection:
    # Translates passages, writes digests, and answers questions about an item or the
    # library, citing nothing.
    if is_survey(asked) or asks_passages(asked):
        return translating()(asked)
    if form_name(asked) == DraftDigest.__name__:
        return digest_reply(asked)
    return StreamedReply(pieces=[ANSWER])


def offered(app: App) -> dict[str, Any]:
    served = app.client.get(MODELS_PATH)
    assert served.status_code == HTTPStatus.OK, f"GET of the models failed: {served.text}"
    view: dict[str, Any] = served.json()
    return view


def asked_with(app: App, item_id: str, model: str) -> dict[str, Any]:
    put = app.client.put(translation_path(item_id), params={"model": model})
    assert put.status_code == HTTPStatus.OK, f"PUT of a translation failed: {put.text}"
    view: dict[str, Any] = put.json()
    return view


def models_of(view: dict[str, Any]) -> set[str]:
    return {block["model"] for block in view["blocks"]}


def translation_requests(app: App) -> list[Asked]:
    return [asked for asked in app.model.asked() if is_survey(asked) or asks_passages(asked)]


def digest_path(item_id: str) -> str:
    return f"/api/items/{item_id}/digest"


def digests_path(item_id: str) -> str:
    return f"/api/items/{item_id}/digests"


def conversation_path(item_id: str) -> str:
    return f"/api/items/{item_id}/conversation"


def asking_with(app: App, path: str, params: dict[str, str]) -> dict[str, Any]:
    query = f"?{httpx2.QueryParams(params)}" if params else ""
    questioned = asking(app.client, f"{path}{query}", QUESTION)
    assert questioned.status == HTTPStatus.OK, f"a question was refused: {questioned.body}"
    assert questioned.events[-1]["kind"] == "answered", (
        f"a question the stand-in answered was not answered: {questioned.events}"
    )
    turn: dict[str, Any] = questioned.events[-1]["turn"]
    return turn


# What a reader may choose.


def test_a_reader_is_offered_the_default_then_the_digest_model_then_glms_unready_without_its_key(
    app: App,
) -> None:
    assert offered(app) == {
        "models": [
            {"name": TRANSLATION_MODEL, "ready": True},
            {"name": STANDIN_MODEL, "ready": True},
            *({"name": name, "ready": False} for name in api.settings.glm_models),
        ],
        "translation": TRANSLATION_MODEL,
        "answering": STANDIN_MODEL,
    }, "the models offered are not the default, the digest model and GLM's, in that order"


def test_glms_models_are_ready_once_glms_key_is_set(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    unready = [model["name"] for model in offered(app)["models"] if not model["ready"]]
    assert unready == [], f"models are offered as unready though GLM's key is set: {unready}"


OPERATIONS: dict[str, Callable[[App, str, dict[str, str]], httpx2.Response]] = {
    "translation PUT": lambda app, item, query: app.client.put(
        translation_path(item), params=query
    ),
    "translation POST": lambda app, item, query: app.client.post(
        translation_path(item), params=query
    ),
    "digest POST": lambda app, item, query: app.client.post(
        digest_path(item), params=query, json=OVERVIEW
    ),
    "block again": lambda app, item, query: app.client.post(block_path(item, 0), params=query),
    "item question": lambda app, item, query: app.client.post(
        conversation_path(item), params=query, json={"text": QUESTION}
    ),
    "library question": lambda app, item, query: app.client.post(
        LIBRARY_CONVERSATION, params=query, json={"text": QUESTION}
    ),
}


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_model_that_is_not_offered_is_refused_and_nothing_is_asked(
    app: App, operation: str
) -> None:
    app.model.reset(answering)
    document = kept(app, long_paragraphs(3))
    refused = OPERATIONS[operation](app, document.item_id, {"model": NOT_OFFERED})
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
        f"{operation} with a model not offered answered {refused.status_code}: {refused.text}"
    )
    assert TRANSLATION_MODEL in refused.text, (
        f"the refusal of a model not offered does not name the models offered: {refused.text}"
    )
    quiet(app)
    progress = translation(app, document.item_id)["progress"]
    assert progress == {"kind": "unasked"} and app.model.asked() == [], (
        f"{operation} with a model not offered asked the model, or started a translation: "
        f"{progress}"
    )


@pytest.mark.parametrize("operation", ["digest POST", "item question", "library question"])
def test_a_glm_model_without_glms_key_is_refused_before_anything_is_sent(
    app: App, operation: str
) -> None:
    app.model.reset(answering)
    document = kept(app, long_paragraphs(3))
    refused = OPERATIONS[operation](app, document.item_id, {"model": glm_model()})
    assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE, (
        f"{operation} of GLM without GLM's key answered {refused.status_code}: {refused.text}"
    )
    assert "INFONIUM_GLM_API_KEY" in refused.json()["detail"], (
        f"the refusal of GLM without its key does not say what is missing: {refused.text}"
    )
    quiet(app)
    assert app.model.asked() == [], f"{operation} of GLM without GLM's key sent requests"


# A translation.


def test_a_translation_is_made_with_the_model_asked_for(app: App) -> None:
    app.model.reset(translating())
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS))
    started = asked_with(app, document.item_id, STANDIN_MODEL)
    assert started["progress"]["model"] == STANDIN_MODEL, (
        f"a translation asked of {STANDIN_MODEL} is under way with another: {started['progress']}"
    )
    view = settled(app, document.item_id)
    asked_models = {asked.body["model"] for asked in translation_requests(app)}
    assert view["progress"]["kind"] == "translated" and asked_models == {STANDIN_MODEL}, (
        f"a translation asked of {STANDIN_MODEL} asked {asked_models}: {view['progress']}"
    )
    assert models_of(view) == {STANDIN_MODEL}, (
        f"the blocks of a translation asked of {STANDIN_MODEL} say {models_of(view)} made them"
    )


def test_a_translation_asked_for_without_a_model_is_made_with_the_default(app: App) -> None:
    app.model.reset(translating())
    document = kept(app, long_paragraphs(3))
    app.client.put(translation_path(document.item_id))
    view = settled(app, document.item_id)
    asked_models = {asked.body["model"] for asked in translation_requests(app)}
    assert asked_models == {TRANSLATION_MODEL} and models_of(view) == {TRANSLATION_MODEL}, (
        f"a translation asked for without a model was made by {asked_models}, not the default"
    )


def test_a_glm_model_is_asked_at_glms_endpoint_with_glms_key_for_json_of_the_form_it_is_told(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    app.model.reset(translating())
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS))
    asked_with(app, document.item_id, glm_model())
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated" and models_of(view) == {glm_model()}, (
        f"a translation asked of {glm_model()} was not made by it: {view['progress']}"
    )
    requests = translation_requests(app)
    misplaced = [
        (asked.path, asked.body["model"])
        for asked in requests
        if (asked.path, asked.key) != (GLM_CHAT_PATH, GLM_KEY)
    ]
    assert requests and misplaced == [], (
        f"requests of a GLM translation went elsewhere than GLM's endpoint with GLM's key: "
        f"{misplaced}"
    )
    for asked in requests:
        form = DocumentSurvey if is_survey(asked) else Reply
        told = FORM_TOLD.format(
            name=form.__name__, schema=json.dumps(reply_schema(form), ensure_ascii=False)
        )
        system = asked.body["messages"][0]["content"]
        assert asked.body["response_format"] == {"type": JSON_OBJECT}, (
            f"a request to GLM asks for {asked.body['response_format']}, not JSON alone"
        )
        assert system.endswith(told), (
            f"a request to GLM does not tell the form {form.__name__} after its instructions"
        )


def test_each_glm_model_is_asked_to_reason_as_much_as_is_set_for_it(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    monkeypatch.setattr(api.settings, "glm_reasoning_efforts", {glm_model(): "low"})
    app.model.reset(translating())
    for model in (glm_model(), glm_flash()):
        document = kept(app, long_paragraphs(3))
        asked_with(app, document.item_id, model)
        settled(app, document.item_id)
    efforts = {
        (asked.body["model"], asked.body.get("reasoning_effort"))
        for asked in translation_requests(app)
    }
    assert efforts == {(glm_model(), "low"), (glm_flash(), None)}, (
        f"GLM's models were asked to reason {efforts}, where {glm_model()} is set to low and "
        f"{glm_flash()} to the endpoint's default"
    )


def test_a_glm_translation_without_glms_key_stops_and_sends_nothing_anywhere(app: App) -> None:
    app.model.reset(translating())
    document = kept(app, long_paragraphs(3))
    asked_with(app, document.item_id, glm_model())
    view = settled(app, document.item_id)
    quiet(app)
    progress = view["progress"]
    assert progress["kind"] == "stopped" and progress["model"] == glm_model(), (
        f"a GLM translation without GLM's key did not stop: {progress}"
    )
    assert "INFONIUM_GLM_API_KEY" in progress["reason"], (
        f"a GLM translation without GLM's key stopped without saying so: {progress['reason']}"
    )
    assert app.model.asked() == [], (
        "a GLM translation without GLM's key sent requests: "
        f"{[(asked.path, asked.key) for asked in app.model.asked()]}"
    )


def refusing_one_request_of(model: str) -> Callable[[Asked], StreamedReply | Rejection]:
    # Translates every passage, but refuses the model given the request that carries the
    # last paragraph.
    answer = translating()

    def respond(asked: Asked) -> StreamedReply | Rejection:
        refused = asks_passages(asked) and any(
            plain(passage.text).startswith(f"Paragraph {REFUSED_PARAGRAPH} ")
            for passage in passages_asked(asked)
        )
        return REFUSED_REQUEST if refused and asked.body["model"] == model else answer(asked)

    return respond


def test_a_stopped_translation_goes_on_with_the_model_asked_for_now(app: App) -> None:
    app.model.reset(refusing_one_request_of(TRANSLATION_MODEL))
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS))
    asked_with(app, document.item_id, TRANSLATION_MODEL)
    stopped = settled(app, document.item_id)
    assert stopped["progress"]["kind"] == "stopped" and stopped["blocks"], (
        f"a translation one of whose requests was refused did not stop with blocks kept: "
        f"{stopped['progress']}"
    )
    went_on = asked_with(app, document.item_id, STANDIN_MODEL)
    assert went_on["progress"]["model"] == STANDIN_MODEL, (
        f"a translation that went on did not go on with the model asked for: {went_on}"
    )
    view = by_number(settled(app, document.item_id))
    first = {number: block["model"] for number, block in by_number(stopped).items()}
    later = {number: block["model"] for number, block in view.items() if number not in first}
    assert first == {number: view[number]["model"] for number in first}, (
        "the blocks kept before a translation went on were made again"
    )
    assert len(view) == SEVERAL_REQUESTS_PARAGRAPHS and set(later.values()) == {STANDIN_MODEL}, (
        f"the blocks left by a stopped translation were made by {set(later.values())}, not by "
        f"the model it went on with"
    )


def test_a_translation_under_way_when_the_service_stopped_goes_on_with_its_model(
    start_app: Callable[..., App],
) -> None:
    first = start_app()
    document = kept(first, long_paragraphs(3))
    first.stop()
    with first.store.transaction(LOCAL_OWNER) as transaction:
        TranslationShelf(transaction).keep_record(
            TranslationRecord(
                document_id=document.document_id,
                item_id=document.item_id,
                language=api.settings.language,
                title=TITLE,
                survey=Unsurveyed(),
                run=1,
                model=STANDIN_MODEL,
                course=Running(),
                input_tokens=0,
                output_tokens=0,
            )
        )
    first.model.reset(translating())
    again = start_app()
    view = settled(again, document.item_id)
    asked_models = {asked.body["model"] for asked in translation_requests(again)}
    assert view["progress"]["kind"] == "translated" and asked_models == {STANDIN_MODEL}, (
        f"a translation of {STANDIN_MODEL} went on after a restart with {asked_models}"
    )


def test_a_record_kept_before_the_model_was_named_was_made_with_the_earlier_model() -> None:
    record = TranslationRecord(
        document_id="document",
        item_id="item",
        language="简体中文",
        title=TITLE,
        survey=Unsurveyed(),
        run=1,
        model=STANDIN_MODEL,
        course=Running(),
        input_tokens=0,
        output_tokens=0,
    )
    earlier = record.model_dump(mode="json", exclude={"model"})
    assert TranslationRecord.model_validate(earlier).model == EARLIER_MODEL, (
        "a record that names no model is not read as made with the earlier model"
    )


# Digests, answers, and a block translated again.


def test_a_digest_is_written_by_the_model_chosen_and_kept_whatever_is_chosen_later(
    app: App,
) -> None:
    app.model.reset(answering)
    document = kept(app, long_paragraphs(3))
    written = app.client.post(
        digest_path(document.item_id), params={"model": TRANSLATION_MODEL}, json=OVERVIEW
    )
    assert written.status_code == HTTPStatus.OK, f"a digest was not written: {written.text}"
    [asked] = app.model.asked()
    assert asked.body["model"] == written.json()["model"] == TRANSLATION_MODEL, (
        f"a digest asked of {TRANSLATION_MODEL} was written by {asked.body['model']}"
    )
    kept_digests = app.client.get(digests_path(document.item_id))
    assert kept_digests.json() == [written.json()] and len(app.model.asked()) == 1, (
        "a digest kept is not read as it was written, or was written again"
    )
    rewritten = app.client.post(
        digest_path(document.item_id), params={"model": STANDIN_MODEL}, json=OVERVIEW
    )
    assert rewritten.json()["model"] == app.model.asked()[-1].body["model"] == STANDIN_MODEL, (
        "a digest written anew was not written by the model chosen"
    )


@pytest.mark.parametrize("scope", ["item", "library"])
def test_a_question_is_answered_by_the_model_chosen_or_else_the_digest_model(
    app: App, scope: str
) -> None:
    app.model.reset(answering)
    document = kept(app, long_paragraphs(3))
    path = conversation_path(document.item_id) if scope == "item" else LIBRARY_CONVERSATION
    for model, expected in ((TRANSLATION_MODEL, TRANSLATION_MODEL), (None, STANDIN_MODEL)):
        sent = len(app.model.asked())
        params = {} if model is None else {"model": model}
        answered = asking_with(app, path, params)
        asked_models = {asked.body["model"] for asked in app.model.asked()[sent:]}
        assert answered["answer"]["model"] == expected and asked_models == {expected}, (
            f"a question asked of {model} was answered by {answered['answer']['model']}, "
            f"asking {asked_models}"
        )


def test_a_block_is_translated_again_by_the_model_chosen(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    app.model.reset(translating())
    document = kept(app, long_paragraphs(3))
    asked_with(app, document.item_id, TRANSLATION_MODEL)
    settled(app, document.item_id)
    sent = len(app.model.asked())
    again = app.client.post(block_path(document.item_id, 1), params={"model": glm_flash()})
    assert again.status_code == HTTPStatus.OK, f"a block was not translated again: {again.text}"
    [asked] = app.model.asked()[sent:]
    assert (asked.path, asked.key, asked.body["model"], again.json()["model"]) == (
        GLM_CHAT_PATH,
        GLM_KEY,
        glm_flash(),
        glm_flash(),
    ), f"a block asked again of {glm_flash()} went to {asked.path}, of {asked.body['model']}"
    sent = len(app.model.asked())
    app.client.post(block_path(document.item_id, 2))
    [asked] = app.model.asked()[sent:]
    assert (asked.path, asked.key, asked.body["model"]) == (
        RESPONSES_PATH,
        MODEL_KEY,
        STANDIN_MODEL,
    ), f"a block asked again of no model went to {asked.path}, of {asked.body['model']}"


def test_glm_answering_about_the_library_is_held_to_json_but_in_the_first_request_that_may_search(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    kept(app, long_paragraphs(3))
    searched = StreamedReply(
        calls=[Call(name=SEARCH_TOOL, arguments=json.dumps({"query": "quay"}))]
    )
    app.model.reset(lambda asked: StreamedReply(pieces=[ANSWER]) if asked.number else searched)
    asking_with(app, LIBRARY_CONVERSATION, {"model": glm_flash()})
    formats = [asked.body.get("response_format") for asked in app.model.asked()]
    assert formats == [None, {"type": JSON_OBJECT}], (
        f"a GLM reply that searched first was asked with the forms {formats}, where the first "
        "request, which may search, holds it to nothing and the next to JSON"
    )


# The pace of GLM's endpoint.


def timed(arrivals: list[float], answer: Responder) -> Responder:
    # Keeps when each request arrived, and answers it as the responder given does.
    def respond(asked: Asked) -> StreamedReply | Rejection:
        arrivals.append(time.monotonic())
        return answer(asked)

    return respond


def busy_for(seconds: float, answer: Responder) -> Responder:
    # GLM's endpoint refuses every request for being asked too often until the time given has
    # passed since the first, then answers as the responder given does.
    arrivals: list[float] = []

    def respond(asked: Asked) -> StreamedReply | Rejection:
        arrivals.append(time.monotonic())
        return TOO_OFTEN if arrivals[-1] - arrivals[0] < seconds else answer(asked)

    return respond


def test_the_requests_of_glm_translations_by_both_its_models_keep_the_pace_set_for_its_endpoint(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_glm_key(monkeypatch)
    monkeypatch.setattr(api.settings, "glm_requests_per_minute", PACED_REQUESTS)
    arrivals: list[float] = []
    app.model.reset(timed(arrivals, translating()))
    models = (glm_model(), glm_flash())
    documents = [kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS)) for _ in models]
    for document, model in zip(documents, models, strict=True):
        asked_with(app, document.item_id, model)
    for document, model in zip(documents, models, strict=True):
        view = settled(app, document.item_id)
        assert view["progress"]["kind"] == "translated", (
            f"a paced translation by {model} did not end translated: {view['progress']}"
        )
    interval = 60 / PACED_REQUESTS
    spacing = [later - earlier for earlier, later in pairwise(arrivals)]
    assert len(arrivals) > 2 and min(spacing) >= interval - EARLY_ARRIVAL, (
        f"the requests of two GLM translations came {spacing} s apart, at a pace of "
        f"{PACED_REQUESTS} a minute"
    )


def test_a_glm_translation_that_the_endpoint_refuses_for_a_while_is_translated_after_its_pauses(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unpaced, a request made at once again and again, with the client's own short wait,
    # spends its tries within the time the endpoint refuses, and the translation stops.
    with_glm_key(monkeypatch)
    monkeypatch.setattr(model_connection, "FIRST_PAUSE_SECONDS", REFUSAL_PAUSE)
    app.model.reset(busy_for(BUSY_SECONDS, translating()))
    document = kept(app, long_paragraphs(SEVERAL_REQUESTS_PARAGRAPHS))
    asked_with(app, document.item_id, glm_flash())
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated" and models_of(view) == {glm_flash()}, (
        f"a GLM translation refused for {BUSY_SECONDS} s was not translated once the endpoint "
        f"answered: {view['progress']}"
    )
