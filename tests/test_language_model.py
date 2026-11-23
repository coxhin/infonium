import threading
from collections.abc import Callable
from http import HTTPStatus

import openai
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.language_model import (
    REPLY_ROUNDS,
    Called,
    LanguageModel,
    Message,
    Replied,
    ReplyEvent,
    Side,
    Tool,
    UnmeteredCompletion,
    UnstructuredCompletion,
    WireApi,
    Written,
    openai_model,
)
from infonium.model_connection import Ending
from tests.model_standin import (
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STANDIN_MODEL,
    Asked,
    Call,
    Finish,
    ModelStandin,
    Rejection,
    Reply,
    call_id,
    pieces_of,
)

INSTRUCTIONS = "Count the words the asker names."
QUESTION = "How often does the word harbour come?"
TOOL_NAME = "lookup"
UNKNOWN_TOOLS = ("search", "read_block", "lookup_word")
WORDS = ("harbour", "ledger", "ferry", "fog", "港", "évier")
# A model may write text in a request in which it goes on to call tools; that text is not
# the reply.
ASIDE = "Let me look that up first."
FAILURES = ("refused", "cut_short", "filtered", "failed", "unparsable", "misshapen", "unmetered")
# The examples a property runs: each is a few requests to the stand-in.
EXAMPLES = settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])


class Tally(BaseModel):
    words: list[str]
    count: int


class Lookup(BaseModel):
    word: str
    times: int


class Faulty(Exception):
    pass


tallies = st.builds(
    Tally, words=st.lists(st.sampled_from(WORDS), max_size=4), count=st.integers(0, 10**6)
)


def lookup_result(arguments: Lookup) -> str:
    return f"{arguments.word} comes {arguments.times} times"


def model_of(wire_api: WireApi) -> LanguageModel:
    return openai_model(name=STANDIN_MODEL, wire_api=wire_api)


def replying(
    wire_api: WireApi, tools: list[Tool[Lookup]], events: list[ReplyEvent[Tally]]
) -> list[ReplyEvent[Tally]]:
    # Every event of a reply, kept in the list as it is handed on, so that a tool sees what
    # was handed on before it runs.
    messages = [Message(side=Side.ASKER, text=QUESTION)]
    reply = model_of(wire_api).replying(INSTRUCTIONS, messages, Tally, tools, Ending())
    for event in reply:
        events.append(event)
    return events


def lookup_tool(run: Callable[[Lookup], str]) -> Tool[Lookup]:
    return Tool(
        name=TOOL_NAME, description="How often a word comes in the text.", arguments=Lookup, run=run
    )


def by_request(replies: list[Reply]) -> Callable[[Asked], Reply]:
    # The stand-in answers each request of a reply with the next of the replies, and the last
    # one to every request after.
    return lambda asked: replies[min(asked.number, len(replies) - 1)]


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(tally=tallies, cuts=st.lists(st.integers(0, 10**4), max_size=12))
@EXAMPLES
def test_a_reply_is_handed_on_as_it_is_written_and_whole_at_its_end(
    model: ModelStandin, wire_api: WireApi, tally: Tally, cuts: list[int]
) -> None:
    text = tally.model_dump_json()
    pieces = pieces_of(text, cuts)
    model.reset(lambda asked: Reply(pieces=pieces))
    events = replying(wire_api, [], [])
    written = [event for event in events if isinstance(event, Written)]
    assert [event.text for event in written] == [
        "".join(pieces[: count + 1]) for count in range(len(pieces))
    ], f"the reply was not handed on as far as it was written, each time more arrived: {pieces}"
    assert {event.request for event in written} == {0}, "a reply of one request named another"
    assert isinstance(events[-1], Replied) and len(events) == len(written) + 1, (
        "the whole reply was not handed on once, last"
    )
    completion = events[-1].completion
    assert completion.output == tally, "the whole reply is not the reply the model wrote"
    assert (completion.input_tokens, completion.output_tokens) == (INPUT_TOKENS, OUTPUT_TOKENS), (
        "the whole reply does not hold the tokens the endpoint counted"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
def test_a_reply_is_handed_on_before_the_model_has_finished_it(
    model: ModelStandin, wire_api: WireApi
) -> None:
    text = Tally(words=["harbour"], count=3).model_dump_json()
    gate = threading.Event()
    model.reset(lambda asked: Reply(pieces=[text[:12], text[12:]], gate=gate))
    reply = model_of(wire_api).replying(
        INSTRUCTIONS, [Message(side=Side.ASKER, text=QUESTION)], Tally, [], Ending()
    )
    try:
        first = next(reply)
        assert not gate.is_set() and first == Written(request=0, text=text[:12]), (
            f"what the model had written was not handed on while it wrote: {first}"
        )
    finally:
        gate.set()
    assert isinstance([*reply][-1], Replied), "the reply did not end whole once the model wrote on"


@pytest.mark.parametrize("wire_api", list(WireApi))
def test_a_structured_reply_is_the_parsed_reply_with_its_tokens(
    model: ModelStandin, wire_api: WireApi
) -> None:
    tally = Tally(words=["ferry", "fog"], count=41)
    model.reset(lambda asked: Reply(pieces=[tally.model_dump_json()]))
    completion = model_of(wire_api).structured(INSTRUCTIONS, QUESTION, Tally)
    assert (completion.output, completion.input_tokens, completion.output_tokens) == (
        tally,
        INPUT_TOKENS,
        OUTPUT_TOKENS,
    ), "a structured reply is not the model's reply with the tokens the endpoint counted"
    [asked] = model.asked()
    assert QUESTION in asked.text() and INSTRUCTIONS in asked.text(), (
        "a structured reply was not asked with its instructions and prompt"
    )


calls = st.one_of(
    st.builds(
        lambda word, times: (
            "good",
            Call(name=TOOL_NAME, arguments=Lookup(word=word, times=times).model_dump_json()),
        ),
        st.sampled_from(WORDS),
        st.integers(0, 99),
    ),
    st.sampled_from(UNKNOWN_TOOLS).map(
        lambda name: ("unknown", Call(name=name, arguments='{"word": "fog", "times": 1}'))
    ),
    st.sampled_from(['{"word": "fog", "times": "often"}', '{"word": "fog"}', "{not json"]).map(
        lambda arguments: ("wrong", Call(name=TOOL_NAME, arguments=arguments))
    ),
)
rounds = st.lists(
    st.tuples(st.booleans(), st.lists(calls, min_size=1, max_size=3)),
    max_size=REPLY_ROUNDS - 1,
)


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(tally=tallies, planned=rounds)
@EXAMPLES
def test_tools_are_run_with_their_arguments_until_the_model_replies_within_the_bound(
    model: ModelStandin,
    wire_api: WireApi,
    tally: Tally,
    planned: list[tuple[bool, list[tuple[str, Call]]]],
) -> None:
    replies = [
        Reply(pieces=[ASIDE] if aside else [], calls=[call for _, call in round_calls])
        for aside, round_calls in planned
    ]
    model.reset(by_request([*replies, Reply(pieces=[tally.model_dump_json()])]))
    events: list[ReplyEvent[Tally]] = []
    ran: list[tuple[Lookup, ReplyEvent[Tally]]] = []

    def run(arguments: Lookup) -> str:
        ran.append((arguments, events[-1]))
        return lookup_result(arguments)

    replying(wire_api, [lookup_tool(run)], events)
    asked = model.asked()
    assert len(asked) == len(planned) + 1, "the reply did not go on until the model replied"
    good = [
        Lookup.model_validate_json(call.arguments)
        for _, round_calls in planned
        for kind, call in round_calls
        if kind == "good"
    ]
    assert [arguments for arguments, _ in ran] == good, (
        "the tool was not run once for each call, with the arguments validated, in order"
    )
    assert [event for event in events if isinstance(event, Called)] == [
        Called(tool=TOOL_NAME, arguments=arguments) for arguments in good
    ], "the caller did not learn of each call of a tool"
    assert all(seen == Called(tool=TOOL_NAME, arguments=arguments) for arguments, seen in ran), (
        "a call was not handed on before its tool ran"
    )
    for number, (_, round_calls) in enumerate(planned):
        results = asked[number + 1].tool_results()
        identities = [call_id(asked[number], index) for index in range(len(round_calls))]
        assert set(identities) <= set(results), (
            f"request {number + 1} does not carry a result for every call of the one before"
        )
        for (kind, call), identity in zip(round_calls, identities, strict=True):
            if kind == "good":
                expected = lookup_result(Lookup.model_validate_json(call.arguments))
                assert results[identity] == expected, "a tool's result did not go back to the model"
            else:
                assert call.name in results[identity], (
                    f"the result of a call the model got wrong does not tell it so: {results}"
                )
    last = len(planned)
    written = [event for event in events if isinstance(event, Written)]
    assert [event for event in written if event.request != last] == [
        Written(request=number, text=ASIDE) for number, (aside, _) in enumerate(planned) if aside
    ], "text written beside calls of tools was not handed on as part of its own request"
    assert written and written[-1] == Written(request=last, text=tally.model_dump_json()), (
        "the reply was not handed on as part of the request that wrote it"
    )
    final = events[-1]
    assert isinstance(final, Replied) and final.completion.output == tally, (
        "the reply is not what the model wrote in its last request"
    )
    assert (final.completion.input_tokens, final.completion.output_tokens) == (
        INPUT_TOKENS * (last + 1),
        OUTPUT_TOKENS * (last + 1),
    ), "the reply does not hold the tokens of every request it took"


@pytest.mark.parametrize("wire_api", list(WireApi))
@pytest.mark.parametrize("tool_rounds", [REPLY_ROUNDS - 1, REPLY_ROUNDS, 3 * REPLY_ROUNDS])
def test_a_reply_is_failed_with_a_reason_past_the_bound_on_its_requests(
    model: ModelStandin, wire_api: WireApi, tool_rounds: int
) -> None:
    call = Call(name=TOOL_NAME, arguments=Lookup(word="fog", times=1).model_dump_json())
    tally = Tally(words=["fog"], count=1)
    model.reset(
        by_request([*[Reply(calls=[call])] * tool_rounds, Reply(pieces=[tally.model_dump_json()])])
    )
    if tool_rounds < REPLY_ROUNDS:
        events = replying(wire_api, [lookup_tool(lookup_result)], [])
        assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
            f"a reply that took {REPLY_ROUNDS} requests, the bound, was not handed on"
        )
        return
    with pytest.raises(UnstructuredCompletion) as caught:
        replying(wire_api, [lookup_tool(lookup_result)], [])
    assert str(caught.value).strip(), "a reply past the bound failed without a reason"
    assert len(model.asked()) == REPLY_ROUNDS, (
        f"a reply that still called tools was not failed at the bound of {REPLY_ROUNDS} requests"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
def test_a_tool_that_raises_is_not_caught(model: ModelStandin, wire_api: WireApi) -> None:
    raised = Faulty("the library could not be read")
    model.reset(
        by_request([Reply(calls=[Call(name=TOOL_NAME, arguments='{"word": "fog", "times": 1}')])])
    )

    def run(arguments: Lookup) -> str:
        raise raised

    with pytest.raises(Faulty) as caught:
        replying(wire_api, [lookup_tool(run)], [])
    assert caught.value is raised, "a tool's failure was turned into another"


def failed_reply(failure: str, tally: Tally) -> Reply:
    text = tally.model_dump_json()
    match failure:
        case "refused":
            return Reply(pieces=["I cannot help with that."], refused=True)
        case "cut_short":
            return Reply(pieces=[text[: len(text) // 2]], finish=Finish.CUT_SHORT)
        case "filtered":
            return Reply(pieces=[text[: len(text) // 2]], finish=Finish.FILTERED)
        case "failed":
            return Reply(pieces=[text[: len(text) // 2]], finish=Finish.FAILED)
        case "unparsable":
            return Reply(pieces=["The count is three."])
        case "misshapen":
            return Reply(pieces=['{"words": "harbour", "total": 3}'])
        case _:
            return Reply(pieces=[text], metered=False)


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(
    failure=st.sampled_from(FAILURES),
    tool_rounds=st.integers(0, 2),
    tally=tallies,
)
@EXAMPLES
def test_a_reply_that_fails_in_any_request_raises_the_interfaces_own_failure(
    model: ModelStandin, wire_api: WireApi, failure: str, tool_rounds: int, tally: Tally
) -> None:
    call = Call(name=TOOL_NAME, arguments='{"word": "fog", "times": 1}')
    model.reset(by_request([*[Reply(calls=[call])] * tool_rounds, failed_reply(failure, tally)]))
    events: list[ReplyEvent[Tally]] = []
    expected: tuple[type[Exception], ...] = (
        (UnmeteredCompletion,) if failure == "unmetered" else (UnstructuredCompletion,)
    )
    # A stream the Chat Completions API breaks off with an error is the openai package's
    # error of a request that failed.
    if failure == "failed" and wire_api == WireApi.CHAT_COMPLETIONS:
        expected = (UnstructuredCompletion, openai.APIError)
    with pytest.raises(expected):
        replying(wire_api, [lookup_tool(lookup_result)], events)
    assert len(model.asked()) == tool_rounds + 1, "the failing request was not the one that failed"
    assert not any(isinstance(event, Replied) for event in events), (
        f"a reply that is {failure} was handed on as a whole reply"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
def test_a_request_the_endpoint_refuses_raises_the_openai_packages_error(
    model: ModelStandin, wire_api: WireApi
) -> None:
    model.reset(
        lambda asked: Rejection(status=HTTPStatus.BAD_REQUEST, body='{"error": {"message": "no"}}')
    )
    with pytest.raises(openai.APIStatusError) as caught:
        replying(wire_api, [], [])
    assert caught.value.status_code == HTTPStatus.BAD_REQUEST, "the endpoint's answer was lost"
