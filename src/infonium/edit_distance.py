from pydantic import BaseModel

# What each letter of a text before a stretch costs, in end_distances: nothing where the
# stretch may begin anywhere, one where it begins with the text.
ANYWHERE = 0
AT_THE_BEGINNING = 1


class Stretch(BaseModel):
    # Letters of a text, from start to end, and how many letters must change to make them a
    # needle's.
    start: int
    end: int
    distance: int


def end_distances(needle: str, text: str, entry: int) -> list[int]:
    # For each end in the text, the fewest letters to put in, leave out or change that make the
    # needle the stretch of the text that ends there, a stretch that begins anywhere or at the
    # beginning of the text, as entry says. This is Myers' bit-parallel count, as Hyyro writes
    # it: a bit for each letter of the needle, so that a letter of the text takes a few
    # operations on whole numbers.
    size = len(needle)
    full = (1 << size) - 1
    top = 1 << (size - 1)
    matches: dict[str, int] = {}
    for position, letter in enumerate(needle):
        matches[letter] = matches.get(letter, 0) | 1 << position
    rising, falling, distance = full, 0, size
    distances: list[int] = []
    for letter in text:
        equal = matches.get(letter, 0)
        vertical = equal | falling
        horizontal = (((equal & rising) + rising) ^ rising) | equal
        up = falling | (full & ~(horizontal | rising))
        down = rising & horizontal
        distance += 1 if up & top else -1 if down & top else 0
        up = ((up << 1) | entry) & full
        down = (down << 1) & full
        rising = down | (full & ~(vertical | up))
        falling = up & vertical
        distances.append(distance)
    return distances


def prefix_distances(needle: str, text: str) -> list[int]:
    # For each beginning of the text, from none of it to all of it, the fewest changes that
    # make the needle that beginning.
    if not needle:
        return list(range(len(text) + 1))
    return [len(needle), *end_distances(needle, text, AT_THE_BEGINNING)]


def partings(head: str, tail: str, text: str) -> list[int]:
    # The places the text may part at into what is made the head and what is made the tail by
    # the fewest changes.
    costs = [
        to_head + to_tail
        for to_head, to_tail in zip(
            prefix_distances(head, text),
            prefix_distances(tail[::-1], text[::-1])[::-1],
            strict=True,
        )
    ]
    return [at for at, cost in enumerate(costs) if cost == min(costs)]


def nearest_stretches(needle: str, text: str, apart: int) -> list[Stretch]:
    # The stretches of the text that the fewest changes make the needle: one for each place
    # they end at, ends no more than apart letters from the one before being one place, as
    # they are where a letter could go with the stretch or beside it. Of such ends and of the
    # beginnings that go with one, the stretch takes the widest: a letter that could go with
    # it or beside it goes with it. A text that repeats itself, as some recognitions write,
    # holds the needle at several places.
    ends = end_distances(needle, text, ANYWHERE)
    distance = min(ends)
    places: list[int] = []
    for at, held in enumerate(ends):
        if held == distance and places and at - places[-1] <= apart:
            places[-1] = at
        elif held == distance:
            places.append(at)
    return [
        Stretch(
            start=at - (len(starts) - 1 - starts[::-1].index(min(starts))),
            end=at + 1,
            distance=distance,
        )
        for at in places
        for starts in [end_distances(needle[::-1], text[: at + 1][::-1], AT_THE_BEGINNING)]
    ]
