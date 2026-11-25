import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise

import openai
import pytest
from pydantic import BaseModel, SecretStr

from infonium import model_connection
from infonium.language_model import (
    GlmEndpoint,
    LanguageModel,
    Message,
    Side,
    endable_glm_model,
    glm_model,
)
from infonium.model_connection import Ending, Pace, ReplyEnded
from tests.model_standin import (
    GLM_BASE_URL,
    GLM_KEY,
    TOO_OFTEN,
    Asked,
    ModelStandin,
    Rejection,
    Reply,
)

# Two models of GLM's endpoint, one asked over connections that a reader who leaves cuts.
GLM_MODEL = "glm-standin"
GLM_FLASH = "glm-standin-flash"
INSTRUCTIONS = "Count the words."
PROMPT = "fog and rain"
# A pace so quick that only its pauses space the requests.
UNHURRIED = 600_000
# Pauses short enough for a test, and the longest twice the first and a little more, so that
# a pause that doubled once too often or not at all is told from the right one.
FIRST_PAUSE = 0.3
LONGEST_PAUSE = 0.7
# How much earlier than its turn a request may arrive at the stand-in, for the time the
# connection takes, and how much later it may arrive than a pause asks.
EARLY = 0.03
LATE = 0.2
REQUESTS = 6


class Tally(BaseModel):
    words: list[str]
    count: int


TALLY = Tally(words=["fog", "rain"], count=2)


def endpoint(pace: Pace) -> GlmEndpoint:
    return GlmEndpoint(
        base_url=GLM_BASE_URL, api_key=SecretStr(GLM_KEY), reasoning_efforts={}, pace=pace
    )


def arriving(
    arrivals: list[float], refused: Callable[[int], bool]
) -> Callable[[Asked], Reply | Rejection]:
    # Keeps when each request arrived, refuses those the function says by their number, and
    # answers the others.
    def respond(asked: Asked) -> Reply | Rejection:
        arrivals.append(time.monotonic())
        return TOO_OFTEN if refused(asked.number) else Reply(pieces=[TALLY.model_dump_json()])

    return respond


def tallied(model: LanguageModel) -> Tally:
    return model.structured(INSTRUCTIONS, PROMPT, Tally).output


def gaps(arrivals: list[float]) -> list[float]:
    return [later - earlier for earlier, later in pairwise(arrivals)]


@pytest.fixture
def short_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_connection, "FIRST_PAUSE_SECONDS", FIRST_PAUSE)
    monkeypatch.setattr(model_connection, "LONGEST_PAUSE_SECONDS", LONGEST_PAUSE)


@pytest.mark.parametrize("requests_per_minute", [200, 400])
def test_requests_to_glms_endpoint_from_any_thread_and_model_keep_its_pace(
    model: ModelStandin, requests_per_minute: int
) -> None:
    arrivals: list[float] = []
    model.reset(arriving(arrivals, lambda number: False))
    pace = Pace(requests_per_minute=requests_per_minute)
    models = [glm_model(GLM_MODEL, endpoint(pace)), endable_glm_model(GLM_FLASH, endpoint(pace))]
    with ThreadPoolExecutor(max_workers=REQUESTS) as pool:
        tallies = list(pool.map(tallied, [models[number % 2] for number in range(REQUESTS)]))
    assert tallies == [TALLY] * REQUESTS, "a request kept waiting for its turn was not answered"
    interval = 60 / requests_per_minute
    assert len(arrivals) == REQUESTS and min(gaps(arrivals)) >= interval - EARLY, (
        f"requests to GLM's endpoint came {gaps(arrivals)} s apart, at a pace of "
        f"{requests_per_minute} a minute"
    )


@pytest.mark.usefixtures("short_pauses")
def test_after_a_refusal_for_asking_too_often_requests_wait_a_pause_doubling_until_an_answer(
    model: ModelStandin,
) -> None:
    # Each request is made once: the four first are refused, the fifth answered, the sixth
    # refused and the seventh answered.
    arrivals: list[float] = []
    model.reset(arriving(arrivals, lambda number: number < 4 or number == 5))
    glm = endable_glm_model(GLM_FLASH, endpoint(Pace(requests_per_minute=UNHURRIED)))
    for refused in [True] * 4 + [False, True, False]:
        if refused:
            with pytest.raises(openai.RateLimitError):
                tallied(glm)
        else:
            tallied(glm)
    pauses = [FIRST_PAUSE, 2 * FIRST_PAUSE, LONGEST_PAUSE, LONGEST_PAUSE]
    waited = gaps(arrivals)
    assert len(waited) == 6, f"the endpoint got {len(arrivals)} requests, not 7"
    for after, (pause, gap) in enumerate(zip(pauses, waited, strict=False), start=1):
        assert pause - EARLY <= gap < pause + LATE, (
            f"after {after} refusals in a row the next request came {gap:.2f} s later, where "
            f"the pause is {pause} s"
        )
    assert waited[4] < FIRST_PAUSE, (
        f"a request came {waited[4]:.2f} s after an answered one, as if a pause went on"
    )
    assert FIRST_PAUSE - EARLY <= waited[5] < FIRST_PAUSE + LATE, (
        f"after a refusal that followed an answer the next request came {waited[5]:.2f} s "
        f"later, where the pause begins again at {FIRST_PAUSE} s"
    )


@pytest.mark.usefixtures("short_pauses")
def test_the_clients_own_retry_of_a_refused_request_waits_for_the_pause(
    model: ModelStandin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client sends a refused request again after half a second of its own.
    monkeypatch.setattr(model_connection, "FIRST_PAUSE_SECONDS", 1.0)
    arrivals: list[float] = []
    model.reset(arriving(arrivals, lambda number: number == 0))
    assert tallied(glm_model(GLM_MODEL, endpoint(Pace(requests_per_minute=UNHURRIED)))) == TALLY
    assert len(arrivals) == 2 and gaps(arrivals)[0] >= 1.0 - EARLY, (
        f"the client sent a refused request again {gaps(arrivals)} s later, within the pause"
    )


@pytest.mark.usefixtures("short_pauses")
def test_refusals_of_requests_sent_before_a_pause_began_do_not_lengthen_it(
    model: ModelStandin,
) -> None:
    # Four requests are sent at once and refused; each is made again and answered.
    arrivals: list[float] = []
    model.reset(arriving(arrivals, lambda number: number < 4))
    glm = endable_glm_model(GLM_FLASH, endpoint(Pace(requests_per_minute=UNHURRIED)))
    starting = threading.Barrier(4)

    def tallied_again() -> Tally:
        starting.wait()
        with pytest.raises(openai.RateLimitError):
            tallied(glm)
        return tallied(glm)

    with ThreadPoolExecutor(max_workers=4) as pool:
        tallies = list(pool.map(lambda _: tallied_again(), range(4)))
    assert tallies == [TALLY] * 4, "a request made again after the pause was not answered"
    first_again = arrivals[4] - arrivals[3]
    assert len(arrivals) == 8 and FIRST_PAUSE - EARLY <= first_again < FIRST_PAUSE + LATE, (
        f"after four refusals of requests sent at once the next came {first_again:.2f} s "
        f"later, where the pause is {FIRST_PAUSE} s"
    )


def test_a_reply_that_ends_while_its_request_waits_for_its_turn_sends_nothing(
    model: ModelStandin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_connection, "FIRST_PAUSE_SECONDS", 30.0)
    monkeypatch.setattr(model_connection, "TURN_CHECK_SECONDS", 0.05)
    arrivals: list[float] = []
    model.reset(arriving(arrivals, lambda number: True))
    glm = endable_glm_model(GLM_FLASH, endpoint(Pace(requests_per_minute=UNHURRIED)))
    with pytest.raises(openai.RateLimitError):
        tallied(glm)
    ending = Ending()
    failures: list[BaseException] = []

    def replying() -> None:
        asked = [Message(side=Side.ASKER, text=PROMPT)]
        events = glm.replying(INSTRUCTIONS, asked, Tally, [], ending)
        try:
            list(events)
        except ReplyEnded as failure:
            failures.append(failure)

    reply = threading.Thread(target=replying)
    reply.start()
    time.sleep(0.2)
    ended = time.monotonic()
    ending.end()
    reply.join(timeout=5)
    assert not reply.is_alive() and time.monotonic() - ended < 1.0, (
        "a reply that ended while its request waited out a pause went on waiting"
    )
    assert len(failures) == 1, (
        f"a reply that ended while its request waited did not end so: {failures}"
    )
    assert len(arrivals) == 1, "a request was sent for a reply that had ended"
