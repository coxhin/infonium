import json
import re
import string
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pypdfium2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pikepdf import Name

from infonium import pdf
from infonium.pdfium import FIRST_WIDE
from infonium.plain_letters import (
    PLAIN_FORM,
    TEXT_ENCODING,
    UNPAIRED_HALVES,
    plain,
    plain_letters_file,
    plain_map,
    written_string,
)
from tests.pdf_engine_standin import PDF_DATA
from tests.test_pdf import REFERENCE, SAMPLES, as_stored, differences

# The samples whose text layer is written in Latin letters: one in fonts whose codes are two
# bytes, and two in fonts whose codes are one.
LATIN_SAMPLES = ["marks_digital", "mixed_layers", "turned_digital"]
# The letters of the mathematical alphabets, by the names Unicode gives them.
MATHEMATICAL_BLOCK = range(0x1D400, 0x1D800)
MATHEMATICAL_LETTERS = {
    unicodedata.name(chr(point)): chr(point)
    for point in MATHEMATICAL_BLOCK
    if unicodedata.name(chr(point), "")
}
ALPHABETS = [
    "BOLD",
    "ITALIC",
    "BOLD ITALIC",
    "SCRIPT",
    "FRAKTUR",
    "DOUBLE-STRUCK",
    "SANS-SERIF",
    "SANS-SERIF BOLD",
    "MONOSPACE",
]
# A letter may be given as itself, as a letter of a mathematical alphabet, or as a character
# beyond the first 65,536 of Unicode that is no form of others: an emoji, one for each letter.
AS_ITSELF = "itself"
AS_EMOJI = "emoji"
FIRST_EMOJI = 0x1F600
# How a run of a map's codes is written: a code at a time, as a range named by its first
# string, or as a range that lists its strings.
FORMS = ["single", "range", "list"]
MOST_IN_BLOCK = 100
COPY = "plain.pdf"
STYLED = "styled.pdf"
MAP_STRING = rb"<([0-9A-Fa-f]*)>"


def sample_file(sample: str) -> Path:
    return PDF_DATA / "samples" / f"{sample}.pdf"


def given_as(letter: str, alphabet: str) -> str:
    # A letter as it is given: as itself, as an emoji, or as its letter in a mathematical
    # alphabet, which is itself where the alphabet has none of its own.
    match alphabet:
        case "itself":
            return letter
        case "emoji":
            return chr(FIRST_EMOJI + string.ascii_letters.index(letter))
        case _:
            case_name = "CAPITAL" if letter.isupper() else "SMALL"
            name = f"MATHEMATICAL {alphabet} {case_name} {letter.upper()}"
            return MATHEMATICAL_LETTERS.get(name, letter)


def text_of(digits: bytes) -> str:
    return bytes.fromhex(digits.decode()).decode(TEXT_ENCODING, UNPAIRED_HALVES)


def read_map(written: bytes) -> tuple[dict[int, str], int]:
    # What each code of a font's map stands for, and how many bytes a code takes.
    entries: dict[int, str] = {}
    widths = set()
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", written, re.DOTALL):
        strings = re.findall(MAP_STRING, block[1])
        for code, text in zip(strings[::2], strings[1::2], strict=True):
            entries[int(code, 16)] = text_of(text)
            widths.add(len(code) // 2)
    ranges = rb"<([0-9A-Fa-f]*)>\s*<([0-9A-Fa-f]*)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])"
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", written, re.DOTALL):
        for first, last, named in re.findall(ranges, block[1]):
            codes = range(int(first, 16), int(last, 16) + 1)
            widths.add(len(first) // 2)
            if named.startswith(b"["):
                for code, text in zip(codes, re.findall(MAP_STRING, named), strict=True):
                    entries[code] = text_of(text)
            else:
                units = bytes.fromhex(named[1:-1].decode())
                for step, code in enumerate(codes):
                    last_unit = int.from_bytes(units[-2:]) + step
                    entries[code] = (units[:-2] + last_unit.to_bytes(2)).decode(
                        TEXT_ENCODING, UNPAIRED_HALVES
                    )
    [width] = widths
    return entries, width


def follows(entries: dict[int, str], earlier: int, code: int) -> bool:
    # Whether a code goes on from the one before it in a range: the next code with the same
    # bytes before its last, standing for the next string.
    before = entries[earlier].encode(TEXT_ENCODING, UNPAIRED_HALVES)
    after = entries[code].encode(TEXT_ENCODING, UNPAIRED_HALVES)
    return (
        code == earlier + 1
        and code >> 8 == earlier >> 8
        and len(before) == len(after) > 0
        and before[:-2] == after[:-2]
        and int.from_bytes(after[-2:]) == int.from_bytes(before[-2:]) + 1
    )


def written_map(
    written: bytes, entries: dict[int, str], width: int, form_of: Callable[[int], str]
) -> bytes:
    # A font's map written anew with these entries, each run of them in the form chosen for
    # it, between what the map wrote before its first entries and after its last.
    head = written[: written.rfind(b"\n", 0, written.index(b"beginbf")) + 1]
    tail = written[written.index(b"\n", written.rindex(b"endbf")) :]
    runs: list[list[int]] = []
    for code in sorted(entries):
        if runs and follows(entries, runs[-1][-1], code):
            runs[-1].append(code)
        else:
            runs.append([code])
    singles: list[str] = []
    ranges: list[str] = []
    for run in runs:
        first, last = (f"<{each:0{2 * width}X}>" for each in (run[0], run[-1]))
        match form_of(len(run)):
            case "single":
                singles.extend(
                    f"<{each:0{2 * width}X}> {written_string(entries[each]).decode()}"
                    for each in run
                )
            case "range":
                ranges.append(f"{first} {last} {written_string(entries[run[0]]).decode()}")
            case _:
                listed = " ".join(written_string(entries[each]).decode() for each in run)
                ranges.append(f"{first} {last} [{listed}]")
    blocks = [
        f"{len(chunk)} begin{kind}\n" + "\n".join(chunk) + f"\nend{kind}\n"
        for kind, lines in (("bfchar", singles), ("bfrange", ranges))
        for chunk in (lines[at : at + MOST_IN_BLOCK] for at in range(0, len(lines), MOST_IN_BLOCK))
    ]
    return head + "".join(blocks).encode() + tail


def styled_file(
    file: Path, alphabet_of: dict[str, str], form_of: Callable[[int], str], copy: Path
) -> Path:
    # A copy of a file in which every font's map says each Latin letter as the alphabet chosen
    # for it, writing its entries in the forms chosen.
    with pikepdf.open(file) as document:
        maps = {
            mapped.objgen: mapped
            for font in document.objects
            if isinstance(font, pikepdf.Dictionary)
            and isinstance(mapped := font.get(Name.ToUnicode), pikepdf.Stream)
        }
        for mapped in maps.values():
            written = mapped.read_bytes()
            entries, width = read_map(written)
            styled = {
                code: "".join(
                    given_as(character, alphabet_of[character])
                    if character in alphabet_of
                    else character
                    for character in text
                )
                for code, text in entries.items()
            }
            mapped.write(written_map(written, styled, width, form_of))
        document.save(copy)
    return copy


def page_texts(file: Path) -> list[str]:
    document = pypdfium2.PdfDocument(file)
    try:
        return [page.get_textpage().get_text_range() for page in document]
    finally:
        document.close()


def as_given(texts: list[str], alphabet_of: dict[str, str], kept: set[str]) -> list[str]:
    # The texts with each letter given as its chosen alphabet has it, where that alphabet is
    # one of those kept; other letters are themselves.
    return [
        "".join(
            given_as(character, alphabet_of[character])
            if alphabet_of.get(character) in kept
            else character
            for character in text
        )
        for text in texts
    ]


@pytest.mark.parametrize("sample", SAMPLES)
def test_a_pdf_whose_text_holds_no_character_beyond_the_first_65536_is_handed_as_it_is(
    sample: str, tmp_path: Path
) -> None:
    handed = plain_letters_file(sample_file(sample), tmp_path / COPY)
    assert handed == sample_file(sample), (
        f"{sample}, whose text is all narrow, was handed as a copy"
    )
    assert not (tmp_path / COPY).exists(), f"a copy of {sample} was written though none was handed"


@settings(max_examples=40, deadline=None)
@given(data=st.data(), sample=st.sampled_from(LATIN_SAMPLES))
def test_letters_of_the_mathematical_alphabets_are_read_plain_in_the_copy(
    data: st.DataObject, sample: str
) -> None:
    # Letters are given in one alphabet each, or all in one; runs are written in one form each,
    # or all in one, so that long ranges are written too.
    choices = st.sampled_from([AS_ITSELF, AS_EMOJI, *ALPHABETS])
    one_alphabet = data.draw(st.one_of(st.none(), choices), label="one alphabet")
    alphabet_of = {
        letter: one_alphabet if one_alphabet is not None else data.draw(choices, label=letter)
        for letter in string.ascii_letters
    }
    one_form = data.draw(st.one_of(st.none(), st.sampled_from(FORMS)), label="one form")

    def form_of(length: int) -> str:
        if one_form is not None:
            return one_form
        return data.draw(st.sampled_from(FORMS), label=f"form of a run of {length}")

    with tempfile.TemporaryDirectory() as folder:
        styled = styled_file(sample_file(sample), alphabet_of, form_of, Path(folder) / STYLED)
        original = page_texts(sample_file(sample))
        read = page_texts(styled)
        assert read == as_given(original, alphabet_of, {AS_EMOJI, *ALPHABETS}), (
            "PDFium reads the maps the test wrote otherwise than the test meant, "
            "a fault of the test"
        )
        mathematical = any(
            ord(character) in MATHEMATICAL_BLOCK for text in read for character in text
        )
        handed = plain_letters_file(styled, Path(folder) / COPY)
        assert (handed != styled) == mathematical, (
            f"{sample} was handed {'as it is' if mathematical else 'as a copy'}, though its text "
            f"{'holds' if mathematical else 'holds no'} letter of a mathematical alphabet"
        )
        expected = as_given(original, alphabet_of, {AS_EMOJI})
        assert page_texts(handed) == expected, (
            f"the copy of {sample} reads otherwise than its letters, with only its emoji kept"
        )


narrow_texts = st.text(
    st.characters(max_codepoint=FIRST_WIDE - 1, exclude_categories=["Cs"]), max_size=3
)


@given(entries=st.lists(st.tuples(st.integers(0, 0xFFFF), narrow_texts), max_size=12))
def test_a_map_that_says_no_character_beyond_the_first_65536_is_left_as_it_is(
    entries: list[tuple[int, str]],
) -> None:
    lines = "\n".join(f"<{code:04X}> {written_string(text).decode()}" for code, text in entries)
    written = f"{len(entries)} beginbfchar\n{lines}\nendbfchar\n".encode()
    assert plain_map(written) == written, "a map without a wide character was written anew"


soup = st.lists(
    st.one_of(
        st.sampled_from(["beginbfchar", "endbfchar", "beginbfrange", "endbfrange", "[", "]", " "]),
        st.from_regex(r"<[0-9A-Fa-f ]{0,9}>", fullmatch=True),
    ),
    max_size=24,
)


@given(tokens=soup)
def test_any_map_however_written_is_read_without_failing(tokens: list[str]) -> None:
    written = " ".join(tokens).encode()
    assert isinstance(plain_map(written), bytes), "a map was not read"


odd_digits = st.from_regex(r"[0-9A-F]([0-9A-F]{2}){0,3}", fullmatch=True)
wide_strings = st.sampled_from([*MATHEMATICAL_LETTERS.values()]).map(
    lambda letter: letter.encode(TEXT_ENCODING).hex().upper()
)


@given(
    odd_strings=st.lists(
        st.tuples(st.integers(0, 0xFFFF), wide_strings, st.booleans()), max_size=6
    ),
    odd_codes=st.lists(st.tuples(odd_digits, odd_digits, wide_strings), max_size=6),
)
def test_an_entry_written_in_an_odd_number_of_digits_is_left_as_it_is_written(
    odd_strings: list[tuple[int, str, bool]], odd_codes: list[tuple[str, str, str]]
) -> None:
    # A string of a mathematical letter with a digit dropped or added, or a range whose codes
    # are written in an odd number of digits: PDFium reads either in a way of its own.
    singles = "\n".join(
        f"<{code:04X}> <{digits + 'A' if longer else digits[:-1]}>"
        for code, digits, longer in odd_strings
    )
    ranges = "\n".join(f"<{first}> <{last}> <{digits}>" for first, last, digits in odd_codes)
    written = (
        f"{len(odd_strings)} beginbfchar\n{singles}\nendbfchar\n"
        f"{len(odd_codes)} beginbfrange\n{ranges}\nendbfrange\n"
    ).encode()
    assert plain_map(written) == written, "an entry PDFium reads in a way of its own was rewritten"


# A map's stream may be data its filter cannot decode, or name a filter that no reader knows.
BROKEN_STREAMS = [(b"not deflated", "/FlateDecode"), (b"anything", "/NoSuchDecode")]


@pytest.mark.parametrize(("data", "filter_name"), BROKEN_STREAMS)
def test_a_map_that_cannot_be_read_is_left_and_the_others_are_given_plain(
    data: bytes, filter_name: str, tmp_path: Path
) -> None:
    italic = dict.fromkeys(string.ascii_letters, "ITALIC")
    styled = styled_file(sample_file("mixed_layers"), italic, lambda _: "single", tmp_path / STYLED)
    with pikepdf.open(styled) as document:
        maps = [
            mapped
            for font in document.objects
            if isinstance(font, pikepdf.Dictionary)
            and isinstance(mapped := font.get(Name.ToUnicode), pikepdf.Stream)
        ]
        assert len(maps) > 1, "the sample has no two fonts with maps of their own"
        maps[0].write(data, filter=Name(filter_name))
        broken = tmp_path / "broken.pdf"
        document.save(broken)
    read = page_texts(broken)
    assert any(ord(c) in MATHEMATICAL_BLOCK for text in read for c in text), (
        "no letter of the file with a broken map is read as a mathematical one"
    )
    handed = plain_letters_file(broken, tmp_path / COPY)
    assert handed != broken, "the fonts whose maps can be read were not given plain"
    left = [c for text in page_texts(handed) for c in text if ord(c) in MATHEMATICAL_BLOCK]
    assert not left, f"letters of the fonts whose maps can be read stay mathematical: {left}"


@given(text=st.text())
def test_a_plain_text_holds_no_character_beyond_the_first_65536_that_is_a_form_of_others(
    text: str,
) -> None:
    plain_text = plain(text)
    wide = [
        character
        for character in plain_text
        if ord(character) >= FIRST_WIDE
        and all(ord(part) < FIRST_WIDE for part in unicodedata.normalize(PLAIN_FORM, character))
    ]
    assert not wide, f"{text!r} was given plain as {plain_text!r}, which holds {wide!r}"
    narrow = "".join(character for character in text if ord(character) < FIRST_WIDE)
    assert plain(narrow) == narrow, f"{narrow!r}, all within the first 65,536, was changed"


@pytest.mark.mineru
@pytest.mark.parametrize("sample", ["marks_digital", "turned_digital"])
def test_a_pdf_whose_letters_are_given_in_a_mathematical_alphabet_reads_as_the_reference(
    sample: str, pdf_engine: Path, tmp_path: Path
) -> None:
    italic = dict.fromkeys(string.ascii_letters, "ITALIC")
    styled = styled_file(
        sample_file(sample), italic, lambda _: "single", tmp_path / f"{sample}.pdf"
    )
    folder = tmp_path / "work"
    folder.mkdir()
    extracted = pdf.extract_pdf(styled, sample, pdf_engine, folder)
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample}, with its letters given as mathematical italic, is read otherwise than "
        f"{REFERENCE} read it: {differences(expected, read)}"
    )
