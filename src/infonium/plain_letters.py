import re
import unicodedata
from pathlib import Path
from typing import Final

import pikepdf
from pikepdf import Name

from infonium.pdfium import FIRST_WIDE, holds_wide_characters

# MinerU reads a PDF's text layer through pdftext, which asks PDFium for the characters of a
# page one unit of UTF-16 at a time. A character beyond the first 65,536 of Unicode comes in
# two such units, and pdftext takes each for a broken character and puts U+FFFD in its place.
# The letters of the mathematical alphabets are such characters, and the mathematical fonts of
# TeX and of Word say their letters are those: in a paper, the mathematical italic N (U+1D441)
# of "N is the total number of action tokens" came out as U+FFFD. In the copy of a PDF that
# MinerU is given, each font says that such a letter is the plain letter it is a form of, as
# Unicode's compatibility form has it: the italic N is N, and the italic alpha is alpha. A
# character beyond the first 65,536 that is no form of others, as an emoji, is left as it is.

# Where a font's map from its codes to text says what its codes stand for: one code at a time,
# each with the string it stands for; or a range of codes that differ in their last byte, each
# standing for the string named for the first code with its last unit counted on by one for
# each code after it, or for the string listed for it. The map is read and written here by
# hand: the libraries that read such maps, pdfminer's and pypdf's, read them into text and write
# none back, and a map is to change in its strings of such characters alone, every other entry
# staying as PDFium reads it.
SINGLE_CODES = re.compile(rb"(?<=beginbfchar)(.*?)(?=endbfchar)", re.DOTALL)
CODE_RANGES = re.compile(rb"(?<=beginbfrange)(.*?)(?=endbfrange)", re.DOTALL)
STRING = rb"<[0-9A-Fa-f\s]*>"
CODE_AND_STRING = re.compile(rb"(" + STRING + rb"\s*)(" + STRING + rb")")
RANGE = re.compile(rb"(" + STRING + rb")\s*(" + STRING + rb")(\s*)(" + STRING + rb"|\[[^\]]*\])")
LISTED_STRING = re.compile(STRING)
STRING_OPENING = b"<"
STRING_CLOSING = b">"
LIST_OPENING = b"["
LIST_CLOSING = b"]"
LIST_GAP = b" "
# The text a code stands for is written in UTF-16, high byte first, in hexadecimal digits. A
# half of a character that a map writes without its other half is kept as it is written.
TEXT_ENCODING = "utf-16-be"
UNPAIRED_HALVES = "surrogatepass"
UNIT_BYTES = 2
UNIT_SPAN = 1 << 16
DIGITS_ENCODING = "ascii"
MOST_IN_RANGE = 256
# The form of a text in which a character that is a form of others, as a ligature or a letter
# of a mathematical alphabet, is those others.
PLAIN_FORM: Final = "NFKC"


def plain(text: str) -> str:
    # The text with each character beyond the first 65,536 that is a form of characters within
    # them given as those.
    plain_characters = []
    for character in text:
        form = unicodedata.normalize(PLAIN_FORM, character)
        narrow = all(ord(part) < FIRST_WIDE for part in form)
        plain_characters.append(form if ord(character) >= FIRST_WIDE and narrow else character)
    return "".join(plain_characters)


def digits_of(written: bytes) -> bytes:
    # The hexadecimal digits of a string of a font's map, without its brackets and spaces.
    return b"".join(written[1:-1].split())


def written_string(text: str) -> bytes:
    units = text.encode(TEXT_ENCODING, UNPAIRED_HALVES)
    return STRING_OPENING + units.hex().upper().encode(DIGITS_ENCODING) + STRING_CLOSING


def plain_string(written: bytes) -> bytes:
    # A string of a font's map, as the map writes it, with the text it stands for given plain.
    # A string that is not written in whole units of UTF-16 is left as it is written: PDFium
    # reads a string of an odd number of digits in a way of its own.
    digits = digits_of(written)
    if len(digits) % (2 * UNIT_BYTES):
        return written
    units = bytes.fromhex(digits.decode(DIGITS_ENCODING))
    text = units.decode(TEXT_ENCODING, UNPAIRED_HALVES)
    plain_text = plain(text)
    return written if plain_text == text else written_string(plain_text)


def plain_range(found: re.Match[bytes]) -> bytes:
    # A range of a font's map with the text of each of its codes given plain: a range that
    # names one string for its first code is written as the list of its strings where the text
    # of any code changes. A range is left as it is written where its codes are written in no
    # digits or an odd number of them, which PDFium counts in a way of its own; where it runs
    # over more codes than a byte tells apart; or where its strings run past a unit.
    first, last, gap, named = found.groups()
    codes = [digits_of(code) for code in (first, last)]
    if any(not digits or len(digits) % 2 for digits in codes):
        return found[0]
    if named.startswith(LIST_OPENING):
        plain_list = LISTED_STRING.sub(lambda listed: plain_string(listed[0]), named)
        return found[0] if plain_list == named else first + LIST_GAP + last + gap + plain_list
    digits = digits_of(named)
    count = int(codes[1], 16) - int(codes[0], 16) + 1
    if not digits or len(digits) % (2 * UNIT_BYTES) or not 0 < count <= MOST_IN_RANGE:
        return found[0]
    units = bytes.fromhex(digits.decode(DIGITS_ENCODING))
    start = int.from_bytes(units[-UNIT_BYTES:])
    if start + count > UNIT_SPAN:
        return found[0]
    texts = [
        (units[:-UNIT_BYTES] + (start + step).to_bytes(UNIT_BYTES)).decode(
            TEXT_ENCODING, UNPAIRED_HALVES
        )
        for step in range(count)
    ]
    if all(plain(text) == text for text in texts):
        return found[0]
    listed = LIST_GAP.join(written_string(plain(text)) for text in texts)
    return first + LIST_GAP + last + gap + LIST_OPENING + listed + LIST_CLOSING


def plain_map(written: bytes) -> bytes:
    # A font's map from its codes to text, as the font writes it, with the text of every code
    # given plain.
    singles = SINGLE_CODES.sub(
        lambda block: CODE_AND_STRING.sub(
            lambda entry: entry[1] + plain_string(entry[2]), block[0]
        ),
        written,
    )
    return CODE_RANGES.sub(lambda block: RANGE.sub(plain_range, block[0]), singles)


def plain_letters_file(file: Path, copy: Path) -> Path:
    # A file whose text layer holds no character beyond the first 65,536 that is a form of
    # others: the file itself where it holds none, and otherwise a copy, written where it is
    # asked for, in which each font's map says the others where it said such a character.
    if not holds_wide_characters(file):
        return file
    with pikepdf.open(file) as document:
        maps = {
            mapped.objgen: mapped
            for font in document.objects
            if isinstance(font, pikepdf.Dictionary)
            and isinstance(mapped := font.get(Name.ToUnicode), pikepdf.Stream)
        }
        rewritten = {}
        for key, mapped in maps.items():
            # A map whose stream QPDF cannot read, by a filter it does not know or data it
            # cannot decode, is left as it is: PDFium reads of it what it can, and the file is
            # read as it was.
            try:
                written = mapped.read_bytes()
            except pikepdf.PdfError:
                continue
            plain_written = plain_map(written)
            if plain_written != written:
                rewritten[key] = plain_written
        if not rewritten:
            return file
        for key, plain_written in rewritten.items():
            maps[key].write(plain_written)
        document.save(copy)
    return copy
