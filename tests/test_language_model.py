import json
import logging
import threading
from collections.abc import Callable
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

import openai
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr

from infonium import language_model
from infonium.anchor import Citation
from infonium.citing import DraftStatement
from infonium.digest import DraftDigest, DraftSection, SectionForm
from infonium.language_model import (
    REPLY_ROUNDS,
    RUNAWAY_WHITE_SPACE,
    Called,
    GlmEndpoint,
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
    endable_openai_model,
    glm_model,
    openai_model,
)
from infonium.model_connection import Ending, Pace
from tests.model_standin import (
    GLM_BASE_URL,
    GLM_KEY,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STANDIN_MODEL,
    Asked,
    Call,
    Finish,
    ModelStandin,
    Rejection,
    Reply,
    by_request,
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
FAILURES = ("refused", "cut_short", "filtered", "unparsable", "misshapen", "unmetered")
# How long what the model has written may take to be handed on while it writes on.
WRITING_SECONDS = 2.0
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
    with ThreadPoolExecutor(max_workers=1) as reader:
        first = reader.submit(next, reply)
        written = futures.wait([first], timeout=WRITING_SECONDS).done
        gate.set()
    assert written and first.result() == Written(request=0, text=text[:12]), (
        f"what the model had written was not handed on within {WRITING_SECONDS} s while it wrote on"
    )
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
    with pytest.raises(expected):
        replying(wire_api, [lookup_tool(lookup_result)], events)
    assert len(model.asked()) == tool_rounds + 1, "the failing request was not the one that failed"
    assert not any(isinstance(event, Replied) for event in events), (
        f"a reply that is {failure} was handed on as a whole reply"
    )


@pytest.mark.parametrize(
    ("wire_api", "finish"),
    [
        (WireApi.RESPONSES, Finish.FAILED),
        (WireApi.RESPONSES, Finish.BROKEN_OFF),
        (WireApi.CHAT_COMPLETIONS, Finish.BROKEN_OFF),
    ],
)
@pytest.mark.parametrize("tool_rounds", [0, 2])
def test_a_reply_the_endpoint_fails_in_any_request_raises_the_interfaces_own_failure(
    model: ModelStandin, wire_api: WireApi, finish: Finish, tool_rounds: int
) -> None:
    call = Call(name=TOOL_NAME, arguments=Lookup(word="fog", times=1).model_dump_json())
    failed = Reply(pieces=['{"words": ["fog"], '], finish=finish)
    model.reset(by_request([*[Reply(calls=[call])] * tool_rounds, failed]))
    events: list[ReplyEvent[Tally]] = []
    with pytest.raises(UnstructuredCompletion):
        replying(wire_api, [lookup_tool(lookup_result)], events)
    assert len(model.asked()) == tool_rounds + 1, "the failing request was not the one that failed"
    assert not any(isinstance(event, Replied) for event in events), (
        "a reply the endpoint failed was handed on as a whole reply"
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


# A reply that runs away: one held to JSON that writes RUNAWAY_WHITE_SPACE white-space characters
# in a row outside its strings, between two of its tokens, before the first or after the last, is
# ended, its stream closed, and its request sent again while the request has attempts left.

# The white space JSON allows outside its strings.
JSON_WHITE_SPACE = " \t\n\r"
# So many requests to GLM's endpoint a minute that its pace holds no test back.
UNHURRIED_GLM_REQUESTS = 600_000
# Runs of white space to set before, between or after tokens: a few characters, as a reply set out
# with spaces writes, runs about the bound, and runs past it.
white_space_runs = st.one_of(
    st.integers(0, 4),
    st.integers(RUNAWAY_WHITE_SPACE - 2, RUNAWAY_WHITE_SPACE + 2),
    st.integers(RUNAWAY_WHITE_SPACE, 3 * RUNAWAY_WHITE_SPACE),
).flatmap(lambda length: st.text(alphabet=JSON_WHITE_SPACE, min_size=length, max_size=length))
short_white_space_runs = st.integers(0, RUNAWAY_WHITE_SPACE - 1).flatmap(
    lambda length: st.text(alphabet=JSON_WHITE_SPACE, min_size=length, max_size=length)
)
# Words whose strings hold runs of spaces of any length, beside the marks that open, close and
# escape a string, so that their JSON writes a quote or a backslash right before the spaces.
spaced_words = st.lists(
    st.builds(
        lambda before, spaces, after: before + " " * spaces + after,
        st.text(alphabet='"\\a港', max_size=3),
        st.integers(0, 3 * RUNAWAY_WHITE_SPACE),
        st.text(alphabet='"\\a', max_size=3),
    ),
    min_size=1,
    max_size=3,
)
cuts_anywhere = st.lists(st.integers(0, 10**4), max_size=12)
drafted_digests = st.builds(
    DraftDigest,
    sections=st.lists(
        st.builds(
            DraftSection,
            title=st.text(max_size=12),
            form=st.sampled_from(SectionForm),
            statements=st.lists(
                st.builds(
                    DraftStatement,
                    text=st.text(max_size=30),
                    citations=st.lists(
                        st.builds(
                            Citation, block_number=st.integers(0, 999), quote=st.text(max_size=20)
                        ),
                        max_size=3,
                    ),
                ),
                max_size=4,
            ),
        ),
        max_size=4,
    ),
)


def joined(parts: list[list[str]]) -> list[str]:
    return [
        token for number, part in enumerate(parts) for token in ([","] if number else []) + part
    ]


def tokens_of(value: object) -> list[str]:
    # The tokens of a value's JSON in the order they are written: each mark of an object or an
    # array, and each key, string, number and other literal whole.
    match value:
        case dict():
            members = [[json.dumps(key), ":", *tokens_of(item)] for key, item in value.items()]
            return ["{", *joined(members), "}"]
        case list():
            return ["[", *joined([tokens_of(item) for item in value]), "]"]
        case _:
            return [json.dumps(value, ensure_ascii=False)]


def spaced(value: object, gaps: dict[int, str]) -> tuple[str, int]:
    # The value's JSON with white space set outside its strings: at each place given, taken
    # modulo the places there are, before a token or after the last. And the longest run so set.
    tokens = tokens_of(value)
    at = {place % (len(tokens) + 1): white for place, white in gaps.items()}
    text = "".join(at.get(place, "") + token for place, token in enumerate(tokens))
    return text + at.get(len(tokens), ""), max(map(len, at.values()), default=0)


def runaway_reply(head: str, white: str, gate: threading.Event | None = None) -> Reply:
    # What a model that runs away writes: the head of its reply, then white space, which the
    # stand-in sends in one piece and holds open after it if there is a gate.
    return Reply(pieces=[head + white, ', "count": 3}'], gate=gate)


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(
    tally=tallies,
    gaps=st.dictionaries(st.integers(0, 60), white_space_runs, max_size=4),
    cuts=cuts_anywhere,
)
@example(tally=Tally(words=["fog"], count=1), gaps={3: " " * (RUNAWAY_WHITE_SPACE - 1)}, cuts=[])
@example(tally=Tally(words=["fog"], count=1), gaps={3: " " * RUNAWAY_WHITE_SPACE}, cuts=[])
@EXAMPLES
def test_a_reply_is_sent_again_once_it_writes_300_white_space_characters_in_a_row_outside_strings(
    model: ModelStandin, wire_api: WireApi, tally: Tally, gaps: dict[int, str], cuts: list[int]
) -> None:
    text, longest = spaced(tally.model_dump(mode="json"), gaps)
    first = Reply(pieces=pieces_of(text, cuts))
    model.reset(by_request([first, Reply(pieces=[tally.model_dump_json()])]))
    events = replying(wire_api, [], [])
    ran_away = longest >= RUNAWAY_WHITE_SPACE
    assert len(model.asked()) == (2 if ran_away else 1), (
        f"a reply that wrote at most {longest} white-space characters in a row outside its strings "
        f"was {'not ' if ran_away else ''}ended and sent again"
    )
    assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
        "the reply of the request that was not ended is not the reply"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
@pytest.mark.parametrize("run", [RUNAWAY_WHITE_SPACE - 1, RUNAWAY_WHITE_SPACE])
@pytest.mark.parametrize("white", [" ", "\r \n\t"])
def test_white_space_where_a_key_falls_due_after_a_list_runs_away_at_300_and_not_at_299(
    model: ModelStandin, wire_api: WireApi, run: int, white: str
) -> None:
    # Where the replies that ran away began their white space: right after a list closed, where
    # the next key fell due; of spaces alone, or of carriage returns, newlines, tabs and spaces.
    tally = Tally(words=["harbour"], count=3)
    head = '{"words": ["harbour"]'
    model.reset(
        by_request(
            [
                Reply(pieces=[head, (white * run)[:run], ', "count": 3}']),
                Reply(pieces=[tally.model_dump_json()]),
            ]
        )
    )
    events = replying(wire_api, [], [])
    assert len(model.asked()) == (2 if run >= RUNAWAY_WHITE_SPACE else 1), (
        f"a reply that wrote {run} white-space characters in a row after a list was "
        f"{'not ' if run >= RUNAWAY_WHITE_SPACE else ''}sent again"
    )
    assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
        f"a reply that wrote {run} white-space characters in a row after a list was not read"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(
    words=spaced_words,
    gaps=st.dictionaries(st.integers(0, 60), short_white_space_runs, max_size=6),
    cuts=cuts_anywhere,
)
@EXAMPLES
def test_spaces_inside_a_string_never_end_a_reply_however_many_and_wherever_the_stream_is_cut(
    model: ModelStandin,
    wire_api: WireApi,
    words: list[str],
    gaps: dict[int, str],
    cuts: list[int],
) -> None:
    tally = Tally(words=words, count=len(words))
    text, _ = spaced(tally.model_dump(mode="json"), gaps)
    model.reset(lambda asked: Reply(pieces=pieces_of(text, cuts)))
    events = replying(wire_api, [], [])
    assert len(model.asked()) == 1, (
        "a reply whose strings hold runs of spaces, and that writes fewer than "
        f"{RUNAWAY_WHITE_SPACE} white-space characters in a row outside them, was sent again"
    )
    assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
        "a reply whose strings hold runs of spaces was not read as written"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
@given(
    draft=drafted_digests,
    indent=st.one_of(st.integers(0, 8), st.just("\t")),
    cuts=cuts_anywhere,
)
@EXAMPLES
def test_a_reply_set_out_with_indents_is_read_whole(
    model: ModelStandin, wire_api: WireApi, draft: DraftDigest, indent: int | str, cuts: list[int]
) -> None:
    text = json.dumps(draft.model_dump(mode="json"), indent=indent, ensure_ascii=False)
    model.reset(lambda asked: Reply(pieces=pieces_of(text, cuts)))
    reply = model_of(wire_api).replying(
        INSTRUCTIONS, [Message(side=Side.ASKER, text=QUESTION)], DraftDigest, [], Ending()
    )
    events = list(reply)
    assert len(model.asked()) == 1, f"a digest set out with an indent of {indent!r} was sent again"
    assert isinstance(events[-1], Replied) and events[-1].completion.output == draft, (
        f"a digest set out with an indent of {indent!r} was not read as written"
    )


@pytest.mark.parametrize("wire_api", list(WireApi))
def test_a_reply_that_runs_away_is_cut_off_and_logged_without_its_text(
    model: ModelStandin, wire_api: WireApi, caplog: pytest.LogCaptureFixture
) -> None:
    # The stand-in holds the reply that runs away open after its first piece, as a model that
    # writes white space without end does: only the client's closing ends it.
    caplog.set_level(logging.WARNING, logger=language_model.__name__)
    tally = Tally(words=["harbour"], count=3)
    head = '{"words": ["harbour"]'
    white = "\r \n\t" * RUNAWAY_WHITE_SPACE
    model.reset(
        by_request(
            [runaway_reply(head, white, threading.Event()), Reply(pieces=[tally.model_dump_json()])]
        )
    )
    events = replying(wire_api, [], [])
    first, _ = model.requests()
    assert first.ended.wait(WRITING_SECONDS) and first.left, (
        f"the stream of a reply that ran away was not closed within {WRITING_SECONDS} s"
    )
    assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
        "the request of a reply that ran away was not answered by the request sent again"
    )
    told = [
        record.getMessage()
        for record in caplog.records
        if record.name == language_model.__name__ and record.levelno == logging.WARNING
    ]
    assert len(told) == 1, f"a reply that ran away once was not told once as a warning: {told}"
    assert STANDIN_MODEL in told[0] and str(len(head + white)) in told[0], (
        f"the warning of a runaway does not name its model and how far it came: {told[0]}"
    )
    assert "harbour" not in told[0], f"the warning of a runaway gives its text: {told[0]}"


def failed_first(asked: Asked) -> Rejection:
    return Rejection(status=HTTPStatus.INTERNAL_SERVER_ERROR, body='{"error": {"message": "busy"}}')


# How the requests of a reply run away, with the model that asks: each with how many requests the
# endpoint receives before the reply fails. A digest's model sends a request again once, and an
# answer's never; a request that the client already sent again after a failure has no attempt left.
ATTEMPTS = {
    "a digest's request, every reply running away": (openai_model, [], 2),
    "an answer's request": (endable_openai_model, [], 1),
    "a request the client sent again after a failure": (openai_model, [failed_first], 2),
}


@pytest.mark.parametrize("wire_api", list(WireApi))
@pytest.mark.parametrize("attempts", ATTEMPTS)
def test_a_reply_that_runs_away_is_sent_again_only_while_its_request_has_attempts_left(
    model: ModelStandin, wire_api: WireApi, attempts: str
) -> None:
    made, before, sent = ATTEMPTS[attempts]
    head = '{"words": ["fog"]'
    tally = Tally(words=["fog"], count=1)

    def answer(asked: Asked) -> Reply | Rejection:
        if asked.number < len(before):
            return before[asked.number](asked)
        return runaway_reply(head, " " * RUNAWAY_WHITE_SPACE)

    model.reset(answer)
    reply = made(name=STANDIN_MODEL, wire_api=wire_api).replying(
        INSTRUCTIONS, [Message(side=Side.ASKER, text=QUESTION)], Tally, [], Ending()
    )
    with pytest.raises(UnstructuredCompletion) as caught:
        for event in reply:
            assert not isinstance(event, Replied) or event.completion.output != tally
    assert "white space" in str(caught.value), (
        f"{attempts} failed without saying that it wrote white space without end: {caught.value}"
    )
    assert len(model.asked()) == sent, (
        f"{attempts} reached the endpoint {len(model.asked())} times, not {sent}"
    )


@pytest.mark.parametrize("searching", [True, False])
def test_a_glm_request_left_free_of_json_is_not_read_for_white_space(
    model: ModelStandin, searching: bool
) -> None:
    # The first request of a GLM reply that offers tools leaves the model to the form it is told,
    # and it may answer in prose: such a request is not ended for its white space. Any other is
    # held to JSON, and is.
    endpoint = GlmEndpoint(
        base_url=GLM_BASE_URL,
        api_key=SecretStr(GLM_KEY),
        reasoning_efforts={},
        pace=Pace(requests_per_minute=UNHURRIED_GLM_REQUESTS),
    )
    tally = Tally(words=["fog"], count=3)
    head = '{"words": ["fog"]'
    model.reset(
        by_request(
            [
                runaway_reply(head, " " * 2 * RUNAWAY_WHITE_SPACE),
                Reply(pieces=[tally.model_dump_json()]),
            ]
        )
    )
    tools = [lookup_tool(lookup_result)] if searching else []
    events = list(
        glm_model(STANDIN_MODEL, endpoint).replying(
            INSTRUCTIONS, [Message(side=Side.ASKER, text=QUESTION)], Tally, tools, Ending()
        )
    )
    assert len(model.asked()) == (1 if searching else 2), (
        f"a GLM request {'left free of' if searching else 'held to'} JSON was "
        f"{'' if searching else 'not '}ended for its white space"
    )
    assert isinstance(events[-1], Replied) and events[-1].completion.output == tally, (
        "a GLM reply was not read"
    )
