import shutil
import sys
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import settings
from pydantic import SecretStr

from infonium import api, embedding, intake, search, speech
from infonium.language_model import LanguageModel, WireApi, endable_openai_model, openai_model
from infonium.library import LibraryStore
from infonium.settings import Settings
from tests import pdf_engine_standin
from tests.model_standin import (
    MODEL_BASE_URL,
    MODEL_KEY,
    STANDIN_MODEL,
    TRANSLATION_MODEL,
    ModelStandin,
    model_standin,
)
from tests.service import LOWERED_FETCH_BYTES, LOWERED_FETCH_SECONDS, App, Site, digest_reply

# The examples of a property run as fast as the extractor and the parsers they call, whose
# first calls load what they keep; a deadline per example would fail them at random.
settings.register_profile("infonium", deadline=None)
settings.load_profile("infonium")

# The programs a marked test needs, found as infonium finds them: Chrome and MinerU by the
# settings, which a variable of the environment may name, and FFmpeg's on the PATH.
APP_SETTINGS = Settings()
MARKED_PROGRAMS = {
    "chrome": (APP_SETTINGS.browser,),
    "ffmpeg": (Path("ffmpeg"), Path("ffprobe")),
    "mineru": (APP_SETTINGS.pdf_engine,),
}
# The variables by which a client of the openai package finds its endpoint, and those that
# would send its connections through a proxy.
MODEL_ADDRESS = {"OPENAI_BASE_URL": MODEL_BASE_URL, "OPENAI_API_KEY": MODEL_KEY}
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
SETTING_HINT = {
    "chrome": "set INFONIUM_BROWSER or put google-chrome on the PATH",
    "ffmpeg": "put ffmpeg and ffprobe on the PATH",
    "mineru": "set INFONIUM_PDF_ENGINE or put mineru on the PATH",
}


def found_program(program: Path) -> Path | None:
    found = shutil.which(program)
    return None if found is None else Path(found)


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker, programs in MARKED_PROGRAMS.items():
        missing = [str(program) for program in programs if found_program(program) is None]
        if item.get_closest_marker(marker) and missing:
            pytest.skip(f"{', '.join(missing)} not found: {SETTING_HINT[marker]}")


@pytest.fixture
def chrome() -> Path:
    found = found_program(APP_SETTINGS.browser)
    if found is None:
        pytest.fail("a test that needs Chrome is marked chrome")
    return found


@pytest.fixture
def pdf_engine() -> Path:
    found = found_program(APP_SETTINGS.pdf_engine)
    if found is None:
        pytest.fail("a test that needs MinerU is marked mineru")
    return found


@pytest.fixture
def standin_pdf_engine(tmp_path: Path) -> Callable[..., Path]:
    # The command of a stand-in for MinerU that replays the engine's stored results for a
    # sample of the PDF fixtures, or the results given in their place.
    def engine_for(sample: str, results: Path | None = None) -> Path:
        named = (
            sample
            if results is None
            else f"{sample}{pdf_engine_standin.RESULTS_SEPARATOR}{results}"
        )
        engine = tmp_path / f"mineru-{sample}{'' if results is None else '-changed'}"
        engine.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{pdf_engine_standin.__file__}" "{named}" "$@"\n'
        )
        engine.chmod(0o755)
        return engine

    return engine_for


@pytest.fixture(scope="session")
def served_model() -> Iterator[ModelStandin]:
    with model_standin() as standin:
        yield standin


@pytest.fixture
def model(served_model: ModelStandin, monkeypatch: pytest.MonkeyPatch) -> ModelStandin:
    # The stand-in model, answering digests until a test tells it otherwise, and the openai
    # package's clients sent to it.
    served_model.reset(digest_reply)
    for name, value in MODEL_ADDRESS.items():
        monkeypatch.setenv(name, value)
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return served_model


@pytest.fixture
def start_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    standin_pdf_engine: Callable[[str], Path],
    model: ModelStandin,
) -> Iterator[Callable[..., App]]:
    # Starts the service on the test's library, its models speaking the wire API given, and
    # translating into the language given.
    site = Site()
    monkeypatch.setattr(httpx, "stream", site.stream)
    monkeypatch.setattr(intake, "FETCH_LIMIT_SECONDS", LOWERED_FETCH_SECONDS)
    monkeypatch.setattr(intake, "FETCH_LIMIT_BYTES", LOWERED_FETCH_BYTES)
    engines = intake.Engines(
        pdf=standin_pdf_engine("trap"),
        recogniser=speech.Recogniser(
            base_url="https://speech.test/v1", api_key=SecretStr("unused"), model="none"
        ),
        browser=Path("no-such-browser"),
    )
    embedding_engine = embedding.EmbeddingEngine(
        base_url="https://embedding.test/v1", api_key=SecretStr("unused"), model="none"
    )
    started: list[App] = []
    # The service's own provider of its translator, called anew for each start.
    made_translator = api.translator.__wrapped__

    def start(wire_api: WireApi = WireApi.RESPONSES, language: str = api.settings.language) -> App:
        @cache
        def language_model() -> LanguageModel:
            return openai_model(name=STANDIN_MODEL, wire_api=wire_api)

        @cache
        def answering_model() -> LanguageModel:
            return endable_openai_model(name=STANDIN_MODEL, wire_api=wire_api)

        store = LibraryStore(tmp_path / "data")
        reader = intake.Intake(store=store, readers=api.READERS, engines=engines)
        monkeypatch.setattr(api, "library_store", lambda: store)
        monkeypatch.setattr(api, "intake", lambda: reader)
        index = search.BlockIndex(store=store, engine=embedding_engine)
        monkeypatch.setattr(api, "block_index", lambda: index)
        monkeypatch.setattr(api, "language_model", language_model)
        monkeypatch.setattr(api, "answering_model", answering_model)

        @cache
        def translation_model() -> LanguageModel:
            return openai_model(name=TRANSLATION_MODEL, wire_api=wire_api)

        # The translator is made anew for each start by the service's own provider, on the
        # test's library and models; its translating model has a name of its own, so that the
        # stand-in tells its requests from those of the model that writes digests.
        monkeypatch.setattr(api, "translation_model", translation_model)
        monkeypatch.setattr(api.settings, "language", language)
        translating = made_translator()
        monkeypatch.setattr(api, "translator", lambda: translating)
        client = TestClient(api.app)
        client.__enter__()
        started.append(App(client, store, site, model))
        return started[-1]

    yield start
    for app in started:
        if app.running:
            app.client.__exit__(None, None, None)


@pytest.fixture
def app(start_app: Callable[..., App]) -> App:
    return start_app()
