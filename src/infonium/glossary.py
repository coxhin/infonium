import re

from pydantic import BaseModel

from infonium.document import is_unspaced

# The terms of a document to translate: the names and terms that recur in it, each with the
# one translation it is given throughout, and which of them a request carries.
#
# How a term lists several forms of itself, and the part of a form that may be left out.
TERM_FORMS = re.compile(r"\s*/\s*")
OPTIONAL_PART = re.compile(r"\([^)]*\)")
# A word of a term's form is found in a passage where a word of the passage begins as it
# begins: a word of two letters or fewer only whole, one of up to this many letters in full,
# and a longer one without its last letters, as many as an ending changes, keeping as many.
# Measured on the measurement's Finnish and Spanish sources and their terms: Phobos finds
# Phoboksen, Lappeenranta Lappeenrannan, Haukivesi Haukiveden, Mars Marsin.
WHOLE_WORD_LETTERS = 2
STEM_LETTERS = 5
ENDING_LETTERS = 2
WORD = re.compile(r"[^\W\d_]+")


class Term(BaseModel):
    # A name or a term that recurs in a document, and the one translation it is given in all
    # of it.
    source: str
    translation: str


def term_forms(term: Term) -> list[str]:
    forms = [OPTIONAL_PART.sub("", form).strip() for form in TERM_FORMS.split(term.source)]
    return [form.casefold() for form in forms if form]


def word_found(word: str, words: set[str]) -> bool:
    # Whether a word of a term's form begins one of a passage's words, as far as an ending
    # leaves it.
    if len(word) <= WHOLE_WORD_LETTERS:
        return word in words
    stem = word[: max(min(len(word), STEM_LETTERS), len(word) - ENDING_LETTERS)]
    return any(said.startswith(stem) for said in words)


def form_found(form: str, said: str, words: set[str]) -> bool:
    # A form written without spaces between its words is looked for as it is written.
    if any(map(is_unspaced, form)):
        return form in said
    form_words = WORD.findall(form)
    return bool(form_words) and all(word_found(word, words) for word in form_words)


def occurring_terms(terms: list[Term], texts: list[str]) -> list[Term]:
    # The terms one of whose forms the texts use, in whatever form their words take.
    said = "\n".join(texts).casefold()
    words = set(WORD.findall(said))
    return [
        term for term in terms if any(form_found(form, said, words) for form in term_forms(term))
    ]
