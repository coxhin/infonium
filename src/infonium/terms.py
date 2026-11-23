import unicodedata
from functools import cache
from itertools import groupby

from infonium.document import UNSPACED_CODE_POINTS, WORD_GAP

# How the words of a text are written into the index of blocks, and how the words of a query
# are sought there. The index parts its text at spaces and punctuation, folds case and drops
# accents (FTS5's unicode61 tokenizer with remove_diacritics 2).
TOKENIZER = "unicode61 remove_diacritics 2"
# The writings that put no space between words: those of Chinese and Japanese, and Thai, Lao,
# Burmese and Khmer. A run of letters in them is indexed as the pairs of neighbours it holds,
# so that any two or more letters side by side in it are found.
UNSPACED_WRITING = (
    *UNSPACED_CODE_POINTS,
    range(0x0E00, 0x0F00),
    range(0x1000, 0x10A0),
    range(0x1780, 0x1800),
)
# Korean puts spaces between words, but fastens its particles and endings to them: 서울은,
# 서울에서. A word in Hangul is sought as the start of a word, so that 서울 finds both.
HANGUL = (range(0x1100, 0x1200), range(0x3130, 0x3190), range(0xAC00, 0xD7B0))
# The kinds of character that make up words, as the tokenizer takes them: letters, marks and
# numbers.
WORD_CATEGORIES = frozenset("LMN")
PHRASE_QUOTE = '"'
ANY_ENDING = "*"
EITHER = " OR "
EVERY = " AND "


# A library holds a few thousand distinct characters, and each is looked at for every time it
# is written, so what each one is, is remembered.
@cache
def is_word_character(character: str) -> bool:
    return unicodedata.category(character)[0] in WORD_CATEGORIES


@cache
def is_unspaced_letter(character: str) -> bool:
    return is_word_character(character) and any(
        ord(character) in code_points for code_points in UNSPACED_WRITING
    )


def is_hangul(character: str) -> bool:
    return any(ord(character) in code_points for code_points in HANGUL)


def has_word(text: str) -> bool:
    return any(map(is_word_character, text))


def word_total(text: str) -> int:
    # The words of a text as a reader counts them: each letter of an unspaced writing, and each
    # run of other characters between spaces that holds a letter, a mark or a number.
    return sum(
        len(run) if unspaced else sum(map(has_word, run.split()))
        for unspaced, characters in groupby(text, key=is_unspaced_letter)
        for run in ["".join(characters)]
    )


def neighbour_pairs(run: str) -> list[str]:
    return [run] if len(run) == 1 else [run[at : at + 2] for at in range(len(run) - 1)]


def indexed_words(text: str) -> str:
    # The text as the index takes it: each run of letters of an unspaced writing becomes the
    # pairs of neighbours it holds, apart from what stands around it; other text is left for
    # the tokenizer to part at its spaces and punctuation.
    return WORD_GAP.join(
        WORD_GAP.join(neighbour_pairs(run)) if unspaced else run
        for unspaced, characters in groupby(text, key=is_unspaced_letter)
        for run in ["".join(characters)]
    )


def phrase(words: str) -> str:
    # A quoted phrase is taken as words alone, so nothing of a query is read as the index's
    # syntax.
    return f"{PHRASE_QUOTE}{words.replace(PHRASE_QUOTE, PHRASE_QUOTE * 2)}{PHRASE_QUOTE}"


def sought(words: str) -> str:
    # Words as they are sought: a lone letter of an unspaced writing by itself and at the start
    # of a pair, words that end in Hangul as the start of a word, any other words as they are.
    if len(words) == 1 and is_unspaced_letter(words):
        return f"({phrase(words)}{EITHER}{phrase(words)} {ANY_ENDING})"
    if is_hangul(words[-1]):
        return f"{phrase(words)} {ANY_ENDING}"
    return phrase(words)


def query_parts(query: str) -> list[list[str]]:
    # The parts of a query between its spaces, each as the words and pairs of letters that the
    # index holds of it. A question in Chinese or Japanese is one part.
    parts = (
        [piece for piece in indexed_words(part).split() if has_word(piece)]
        for part in query.split()
    )
    return [part for part in parts if part]


def all_words_expression(query: str) -> str:
    # A block that holds every part of the query, the words of each part side by side.
    return EVERY.join(dict.fromkeys(sought(WORD_GAP.join(part)) for part in query_parts(query)))


def sought_pieces(query: str) -> list[str]:
    # Each word or pair of letters of the query, once, as it is sought.
    return list(dict.fromkeys(sought(piece) for part in query_parts(query) for piece in part))


def any_words_expression(pieces: list[str]) -> str:
    # A block that holds any of the pieces, ranked higher the more it holds.
    return EITHER.join(pieces)
