import json
from collections.abc import Callable
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from infonium.digest import SectionForm
from tests.conversation import answered_turn, asking, outline_of
from tests.model_standin import Asked, Reply
from tests.service import OVERVIEW, App, as_owner, keep_sources, new_owner

# A14: a quote that the model wrote with the escapes of the JSON the document reached it in
# is found; one found as written stays where it was found, whatever it holds.
SPECIAL = (
    "\t",
    "\n",
    "\r",
    "\\",
    '"',
    "/",
    "é",
    "\\u00e9",
    r"C:\temp\new\table",
    r"\nabla",
    "\nabla",
    r"\theta",
    "\theta",
    r"\"quoted\"",
    "back\\slash",
)
WORDS = ("amber", "heron", "dune", "harbour", "港口")
WRITINGS = ("as_written", "escaped", "escaped_slash", "escaped_ascii")
PLACES = ("digest", "item", "library")
EXAMPLES = settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])


def written_as(raw: str, writing: str) -> str:
    # The quote as the model writes it: as it stands in the block, or with JSON's escapes.
    match writing:
        case "as_written":
            return raw
        case "escaped":
            return json.dumps(raw, ensure_ascii=False)[1:-1]
        case "escaped_slash":
            return json.dumps(raw, ensure_ascii=False)[1:-1].replace("/", "\\/")
        case _:
            return json.dumps(raw, ensure_ascii=True)[1:-1]


@st.composite
def blocks_and_quotes(draw: st.DrawFn) -> tuple[str, str]:
    # A block of words and things JSON escapes, each word once; and a quote of it that begins
    # and ends with a word.
    count = draw(st.integers(2, 6))
    words = [f"{draw(st.sampled_from(WORDS))}{number}" for number in range(count)]
    between = [draw(st.sampled_from(SPECIAL)) for _ in range(count - 1)]
    parts = [part for pair in zip(words, [*between, ""], strict=True) for part in pair][:-1]
    first = draw(st.integers(0, count - 1))
    last = draw(st.integers(first, count - 1))
    return "".join(parts), "".join(parts[2 * first : 2 * last + 1])


def expected_start(block: str, raw: str, written: str) -> int:
    # Where the quote is anchored: where it stands as written, or else where it stands decoded.
    found = block.find(written)
    return found if found >= 0 else block.find(raw)


def digest_citing(quote: str) -> Callable[[Asked], Reply]:
    citation = {"block_number": 0, "quote": quote}
    statement = {"text": "Quoted.", "citations": [citation]}
    sections = [{"heading": "Summary", "form": SectionForm.LIST, "statements": [statement]}]
    return lambda asked: Reply(pieces=[json.dumps({"sections": sections})])


def answer_citing(quote: str, document: str | None) -> Callable[[Asked], Reply]:
    def cite(asked: Asked) -> Reply:
        citation: dict[str, Any] = {"block_number": 0, "quote": quote}
        if document is not None:
            [source] = outline_of(asked)["sources"]
            citation["document"] = source["document"]
        statements = [{"text": "Quoted.", "citations": [citation]}]
        return Reply(pieces=[json.dumps({"statements": statements})])

    return cite


def anchors_in(app: App, place: str, item_id: str, written: str) -> list[dict[str, Any]]:
    match place:
        case "digest":
            app.model.reset(digest_citing(written))
            digest = app.client.post(f"/api/items/{item_id}/digest", json=OVERVIEW).json()
            anchors: list[dict[str, Any]] = digest["sections"][0]["statements"][0]["anchors"]
            return anchors
        case "item":
            app.model.reset(answer_citing(written, None))
            path = f"/api/items/{item_id}/conversation"
            turn = answered_turn(asking(app.client, path, "Where?"))
        case _:
            app.model.reset(answer_citing(written, "library"))
            turn = answered_turn(asking(app.client, "/api/conversation", "Where?"))
    found: list[dict[str, Any]] = turn["answer"]["statements"][0]["anchors"]
    return found


@given(
    block_and_quote=blocks_and_quotes(),
    writing=st.sampled_from(WRITINGS),
    place=st.sampled_from(PLACES),
)
@EXAMPLES
def test_a_quote_is_anchored_as_written_or_else_with_jsons_escapes_decoded(
    app: App, block_and_quote: tuple[str, str], writing: str, place: str
) -> None:
    block, raw = block_and_quote
    written = written_as(raw, writing)
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Escapes", [block])])
    with as_owner(owner):
        anchors = anchors_in(app, place, source.item, written)
    start = expected_start(block, raw, written)
    assert len(anchors) == 1, (
        f"a quote {written!r} of the block {block!r} was not anchored once in the {place}"
    )
    [anchor] = anchors
    assert (anchor["block_number"], anchor["start"]) == (0, start), (
        f"the quote {written!r} of the block {block!r} was anchored at {anchor['start']} in the "
        f"{place}, not where it stands {'as written' if block.find(written) >= 0 else 'decoded'}"
        f" ({start})"
    )
    assert block[anchor["start"] : anchor["end"]] == anchor["quote"], (
        f"an anchor's range does not hold its quote in the {place}: {anchor}"
    )


def test_a_latex_command_found_as_written_stays_where_it_was(app: App) -> None:
    # The block holds \nabla as written and, later, a line break before "abla": the quote
    # \nabla is found as written, so it is not taken for the line break.
    block = "grad \\nabla f and then\nabla again"
    for place in PLACES:
        owner = new_owner()
        [source] = keep_sources(app.store, owner, [("LaTeX", [block])])
        with as_owner(owner):
            [anchor] = anchors_in(app, place, source.item, "\\nabla")
        assert anchor["start"] == block.index("\\nabla"), (
            f"a quote found as written was anchored elsewhere in the {place}: {anchor}"
        )
