import json
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import SecretStr

from infonium import api
from infonium.language_model import FORM_TOLD, GLM_REASONING_EFFORT, JSON_OBJECT, reply_schema
from infonium.library import LOCAL_OWNER
from infonium.rendition import Reply
from infonium.shelf import (
    EARLIER_MODEL,
    Running,
    TranslationRecord,
    TranslationShelf,
    Unsurveyed,
)
from infonium.survey import DocumentSurvey
from tests.model_standin import (
    GLM_CHAT_PATH,
    GLM_KEY,
    MODEL_KEY,
    RESPONSES_PATH,
    STANDIN_MODEL,
    TRANSLATION_MODEL,
    Asked,
    Rejection,
)
from tests.model_standin import Reply as StreamedReply
from tests.service import App
from tests.translation_standin import (
    TITLE,
    asks_passages,
    block_path,
    by_number,
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

MODELS_PATH = "/api/translation-models"
NOT_OFFERED = "no-such-model"
# A document long enough to take several requests.
SEVERAL_REQUESTS_PARAGRAPHS = 40
# The paragraph whose request the first model is refused, so that its translation stops with
# the blocks of the other requests kept.
REFUSED_PARAGRAPH = SEVERAL_REQUESTS_PARAGRAPHS - 1
REFUSED_REQUEST = Rejection(
    status=HTTPStatus.BAD_REQUEST, body='{"error": {"message": "the request is refused"}}'
)


def glm_model() -> str:
    return api.settings.glm_models[0]


def offered(app: App) -> dict[str, Any]:
    served = app.client.get(MODELS_PATH)
    assert served.status_code == HTTPStatus.OK, f"GET of the models failed: {served.text}"
    view: dict[str, Any] = served.json()
    return view


def asked_with(app: App, item_id: str, model: str) -> dict[str, Any]:
    put = app.client.put(translation_path(item_id), json={"model": model})
    assert put.status_code == HTTPStatus.OK, f"PUT of a translation failed: {put.text}"
    view: dict[str, Any] = put.json()
    return view


def models_of(view: dict[str, Any]) -> set[str]:
    return {block["model"] for block in view["blocks"]}


def translation_requests(app: App) -> list[Asked]:
    return [asked for asked in app.model.asked() if is_survey(asked) or asks_passages(asked)]


def test_a_reader_is_offered_the_default_then_the_digest_model_then_glms_unready_without_its_key(
    app: App,
) -> None:
    assert offered(app) == {
        "default": TRANSLATION_MODEL,
        "models": [
            {"name": TRANSLATION_MODEL, "ready": True},
            {"name": STANDIN_MODEL, "ready": True},
            *({"name": name, "ready": False} for name in api.settings.glm_models),
        ],
    }, "the models offered are not the default, the digest model and GLM's, in that order"


def test_glms_models_are_ready_once_glms_key_is_set(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.settings, "glm_api_key", SecretStr(GLM_KEY))
    unready = [model["name"] for model in offered(app)["models"] if not model["ready"]]
    assert unready == [], f"models are offered as unready though GLM's key is set: {unready}"


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


@pytest.mark.parametrize("ask", ["put", "post"])
def test_a_model_that_is_not_offered_is_refused_and_nothing_starts(app: App, ask: str) -> None:
    app.model.reset(translating())
    document = kept(app, long_paragraphs(3))
    refused = getattr(app.client, ask)(
        translation_path(document.item_id), json={"model": NOT_OFFERED}
    )
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
        f"{ask.upper()} with a model not offered answered {refused.status_code}: {refused.text}"
    )
    assert TRANSLATION_MODEL in refused.text, (
        f"the refusal of a model not offered does not name the models offered: {refused.text}"
    )
    quiet(app)
    progress = translation(app, document.item_id)["progress"]
    assert progress == {"kind": "unasked"} and app.model.asked() == [], (
        f"a translation asked of a model not offered started: {progress}"
    )


def test_a_glm_model_is_asked_at_glms_endpoint_with_glms_key_for_json_of_the_form_it_is_told(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.settings, "glm_api_key", SecretStr(GLM_KEY))
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
        assert asked.body["reasoning_effort"] == GLM_REASONING_EFFORT, (
            f"a request to GLM asks it to reason {asked.body.get('reasoning_effort')}"
        )


def test_the_model_that_writes_digests_is_still_asked_at_its_own_endpoint_with_its_own_key(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.settings, "glm_api_key", SecretStr(GLM_KEY))
    app.model.reset(translating())
    document = kept(app, long_paragraphs(3))
    asked_with(app, document.item_id, glm_model())
    settled(app, document.item_id)
    sent = len(app.model.asked())
    again = app.client.post(block_path(document.item_id, 1))
    assert again.status_code == HTTPStatus.OK, f"a block was not translated again: {again.text}"
    [asked] = app.model.asked()[sent:]
    assert (asked.path, asked.key, asked.body["model"]) == (
        RESPONSES_PATH,
        MODEL_KEY,
        STANDIN_MODEL,
    ), f"a block translated again went to {asked.path} with another key, of {asked.body['model']}"


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
