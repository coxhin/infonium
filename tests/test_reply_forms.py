import json
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.anchor import Citation
from infonium.citing import DraftStatement
from infonium.digest import DraftDigest, DraftSection, SectionForm
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
# list itself: an object's content comes last. The one exception is a statement's text, which
# follows its citations in alphabetical order: the statement itself, which no model takes for
# done before it is written. A reply that names a renamed field as it was named before, as a
# model told the schema in text may from habit, is read as one that names it as it is now.

# Every form a model's reply is held to, by the reply.
REPLY_FORMS: dict[str, type[BaseModel]] = {
    "a digest": DraftDigest,
    "an answer about an item": DraftAnswer,
    "an answer about the library": LibraryDraftAnswer,
    "a document's survey": DocumentSurvey,
    "a translation": Reply,
}
# The one key allowed after a list without holding one: a statement's text after its citations.
LISTED_BEFORE = "citations"
FOLLOWING = "text"
# The fields renamed so that they no longer fall due after a list, by the form and the object
# that holds them: each as it is named now and as it was named before.
RENAMED = {
    "a digest": ("DraftSection", "heading", "title"),
    "a document's survey": (
        "DocumentSurvey",
        "matches_asked_language",
        "written_in_asked_language",
    ),
}
DEFINITIONS = "$defs"
REFERENCE = "$ref"
PROPERTIES = "properties"

drafted_statements = st.builds(
    DraftStatement,
    text=st.text(max_size=20),
    citations=st.lists(
        st.builds(Citation, block_number=st.integers(0, 999), quote=st.text(max_size=20)),
        max_size=3,
    ),
)
drafted_digests = st.builds(
    DraftDigest,
    sections=st.lists(
        st.builds(
            DraftSection,
            heading=st.text(max_size=12),
            form=st.sampled_from(SectionForm),
            statements=st.lists(drafted_statements, max_size=3),
        ),
        max_size=5,
    ),
)
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
    # The keys that come after a key holding a list without holding one, but a statement's text
    # after its citations alone.
    after = keys[min((keys.index(key) for key in listing), default=len(keys)) :]
    late = [key for key in after if key not in listing]
    return [] if late == [FOLLOWING] and set(after) - {FOLLOWING} == {LISTED_BEFORE} else late


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


@given(draft=drafted_digests, data=st.data())
def test_a_digest_whose_sections_name_their_title_as_before_is_read_as_one_that_names_its_heading(
    draft: DraftDigest, data: st.DataObject
) -> None:
    written = draft.model_dump(mode="json")
    renamed = data.draw(st.sets(st.integers(0, max(len(draft.sections) - 1, 0))))
    as_before = {
        "sections": [
            {
                ("title" if key == "heading" and at in renamed else key): value
                for key, value in section.items()
            }
            for at, section in enumerate(written["sections"])
        ]
    }
    read = parsed_reply(DraftDigest, json.dumps(as_before, ensure_ascii=False))
    assert read == parsed_reply(DraftDigest, json.dumps(written, ensure_ascii=False)) == draft, (
        f"a digest whose sections {sorted(renamed)} name their title as before is read otherwise"
    )


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
