import json
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.digest import DraftDigest
from infonium.glossary import Term
from infonium.item_conversation import DraftAnswer
from infonium.language_model import parsed_reply, reply_schema
from infonium.library_conversation import LibraryDraftAnswer
from infonium.rendition import Reply
from infonium.survey import DocumentSurvey

# The judgments of the forms a model's reply is held to. Through the relay a reply's keys come in
# alphabetical order, whatever the schema's, and where a field that a model could take for done
# fell due after the list that held an object's content, gpt-6.1-terra and gpt-6-luna at times
# wrote white space there without end. So in every object of every form, in the schema's order
# and in alphabetical order alike, no key comes after a key that holds a list unless it holds a
# list itself: an object's content comes last. Two keys are excepted, each after the one list it
# follows:
# - a statement's text, after its citations in alphabetical order: it is the statement itself,
#   which no model takes for done before it is written;
# - a section's title, after its statements in alphabetical order. Drafted first, named heading,
#   it led gpt-6-luna to write every quote of 5 of 101 digests in Chinese, so that each lost all
#   its citations, against none of 101 drafted title last. The white space without end that
#   gpt-6.1-terra and gpt-6-luna at times wrote where the title fell due last is ended by the
#   guard of language_model, which sends the request again.
# A reply that names a renamed field as it was named before, as a model told the schema in text
# may from habit, is read as one that names it as it is now.

# Every form a model's reply is held to, by the reply. Only the forms listed here are judged: a
# form added later must be added here, with the call that passes it to structured or replying.
REPLY_FORMS: dict[str, type[BaseModel]] = {
    "a digest": DraftDigest,
    "an answer about an item": DraftAnswer,
    "an answer about the library": LibraryDraftAnswer,
    "a document's survey": DocumentSurvey,
    "a translation": Reply,
}
# The keys allowed after a list without holding one, each with the one list it may follow: a
# statement's text after its citations, and a section's title after its statements.
FOLLOWING_A_LIST = {"text": "citations", "title": "statements"}
# The fields renamed so that they no longer fall due after a list, by the form and the object
# that holds them: each as it is named now and as it was named before.
RENAMED = {
    "a document's survey": (
        "DocumentSurvey",
        "matches_asked_language",
        "written_in_asked_language",
    ),
}
DEFINITIONS = "$defs"
REFERENCE = "$ref"
PROPERTIES = "properties"

surveys = st.builds(
    DocumentSurvey,
    language=st.text(max_size=12),
    matches_asked_language=st.booleans(),
    terms=st.lists(st.builds(Term, source=st.text(max_size=12), translation=st.text(max_size=12))),
)


def named(schema: dict[str, Any], definitions: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # A schema with the name of the definition it refers to, if any.
    reference = schema.get(REFERENCE)
    if reference is None:
        return schema.get("title", ""), schema
    name = reference.rsplit("/", 1)[-1]
    return name, definitions[name]


def objects(
    schema: dict[str, Any], definitions: dict[str, Any], path: str
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    # Every object a schema describes: the path of keys that leads to it, its name and itself.
    name, schema = named(schema, definitions)
    if PROPERTIES in schema:
        yield path, name, schema
        for key, value in schema[PROPERTIES].items():
            yield from objects(value, definitions, f"{path}.{key}")
    for key in ("items", "anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        for each in value if isinstance(value, list) else [value] if value else []:
            yield from objects(each, definitions, path)


def holds_a_list(schema: dict[str, Any], definitions: dict[str, Any]) -> bool:
    _, schema = named(schema, definitions)
    return schema.get("type") == "array" or any(
        holds_a_list(each, definitions) for each in schema.get("anyOf", [])
    )


def falling_due_late(keys: list[str], listing: set[str]) -> list[str]:
    # The keys that come after a key holding a list without holding one, but a key excepted that
    # follows its one list alone.
    after = keys[min((keys.index(key) for key in listing), default=len(keys)) :]
    late = [key for key in after if key not in listing]
    excepted = len(late) == 1 and set(after) == {late[0], FOLLOWING_A_LIST.get(late[0])}
    return [] if excepted else late


def sent_schema(reply: str) -> dict[str, Any]:
    # The schema the service sends for a reply, as each wire API and a GLM model's
    # instructions carry it.
    schema: dict[str, Any] = reply_schema(REPLY_FORMS[reply])
    return schema


@pytest.mark.parametrize("reply", REPLY_FORMS)
def test_no_key_of_a_reply_falls_due_after_a_list_but_its_content(reply: str) -> None:
    schema = sent_schema(reply)
    definitions = schema.get(DEFINITIONS, {})
    found: list[str] = []
    for path, name, described in objects(schema, definitions, "reply"):
        properties = described[PROPERTIES]
        listing = {key for key, value in properties.items() if holds_a_list(value, definitions)}
        for order, keys in (
            ("the schema's", list(properties)),
            ("alphabetical", sorted(properties)),
        ):
            late = falling_due_late(keys, listing)
            if late:
                found.append(f"{path} ({name}), in {order} order {keys}: {late} after a list")
    assert not found, f"in the schema sent for {reply}, a key falls due after a list: {found}"


@pytest.mark.parametrize("reply", RENAMED)
def test_the_schema_sent_names_a_renamed_field_by_its_new_name_alone(reply: str) -> None:
    holder, now, before = RENAMED[reply]
    schema = sent_schema(reply)
    described = list(objects(schema, schema.get(DEFINITIONS, {}), "reply"))
    held = [list(properties[PROPERTIES]) for _, name, properties in described if name == holder]
    assert held and all(now in keys for keys in held), (
        f"the schema sent for {reply} does not name {now} in {holder}: {held}"
    )
    named_before = [path for path, _, properties in described if before in properties[PROPERTIES]]
    assert not named_before, f"the schema sent for {reply} still names {before}: {named_before}"


@given(survey=surveys)
def test_a_survey_that_names_its_yes_or_no_as_before_is_read_as_one_that_names_it_as_now(
    survey: DocumentSurvey,
) -> None:
    written = survey.model_dump(mode="json")
    as_before = {
        ("written_in_asked_language" if key == "matches_asked_language" else key): value
        for key, value in written.items()
    }
    read = parsed_reply(DocumentSurvey, json.dumps(as_before, ensure_ascii=False))
    assert (
        read == parsed_reply(DocumentSurvey, json.dumps(written, ensure_ascii=False)) == survey
    ), (
        "a survey that names whether the document is in the asked language as before is read "
        "otherwise"
    )
