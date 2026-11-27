import re
import subprocess
import sys
import time
import traceback
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Annotated, Literal

import psutil
from pydantic import Field, TypeAdapter

from infonium import lineage
from infonium.document import Tagged
from infonium.subscriptions.feed import Feed, UnreadableFeed, read_feed

# A feed is read in a process of its own, as a media page is fetched in one: no check of its
# bytes holds against every way a parser can be made to grow, and elements nested millions deep
# took feedparser a minute and 3.2 GB. The process may hold this much data beyond what it holds
# once it has started, and take this long in all, its start among it; a feed that needs more
# holds more than infonium reads. Its data is bounded, not its address space, of which a
# program reserves many times what it uses.
READER_MEMORY_BYTES = 512 * 1024**2
READER_LIMIT_SECONDS = 30.0
# Memory the process holds from its start and gives up first when the reading outgrows its
# bound, so that it has room left to write its answer.
ANSWER_ROOM_BYTES = 4 * 1024**2
# How long the service lets pass between two looks at whether the process has ended.
READER_WATCH_SECONDS = 0.02
# The process is this module run as a program, named by its specification, as run so it calls
# itself "__main__". It takes none of infonium's environment: it reads a file and writes one.
READER = [sys.executable, lineage.RUN_AS_MODULE, __spec__.name]
NOTHING_TAKEN = re.compile(r"(?!)")
# The folder it is given holds the feed's bytes as fetched, and takes its answer.
FEED_FILE = "feed"
ANSWER_FILE = "answer.json"
OUTGROWN = "{source} holds more than infonium reads"


class FeedAnswerKind(StrEnum):
    READ = "read"
    REFUSED = "refused"


class FeedRead(Tagged):
    kind: Literal[FeedAnswerKind.READ] = FeedAnswerKind.READ
    feed: Feed


class FeedRefused(Tagged):
    # Why the bytes hold no feed that infonium reads, in the words read_feed tells a reader.
    kind: Literal[FeedAnswerKind.REFUSED] = FeedAnswerKind.REFUSED
    reason: str


# What the process tells the service of the feed it was given.
FeedAnswer = Annotated[FeedRead | FeedRefused, Field(discriminator="kind")]
FEED_ANSWER = TypeAdapter(FeedAnswer)


def nothing_left() -> None:
    # A reader starts no program, so once its service is gone it has nothing to end but itself.
    return None


def main() -> None:
    # What this module does when it is run as a program: it reads the feed in the folder it is
    # given, as served at the address given and declared as the kind given, and writes the
    # answer beside it. A feed whose reading runs out of the memory the process may hold holds
    # more than infonium reads; where the reading broke off is written to standard error, the
    # service's own. The bytes are taken out of the folder once read, so that a service ended
    # meanwhile, which removes nothing, leaves no copy of them behind.
    Thread(target=lineage.end_when_abandoned, args=(nothing_left,), daemon=True).start()
    lineage.hold_data_to(psutil.Process().memory_info().data + READER_MEMORY_BYTES)
    room = bytearray(ANSWER_ROOM_BYTES)
    folder, declared, address = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    content = (folder / FEED_FILE).read_bytes()
    (folder / FEED_FILE).unlink()
    try:
        answer: FeedAnswer = FeedRead(feed=read_feed(content, declared, address))
    except UnreadableFeed as refusal:
        answer = FeedRefused(reason=str(refusal))
    except MemoryError:
        del room
        traceback.print_exc()
        answer = FeedRefused(reason=OUTGROWN.format(source=address))
    (folder / ANSWER_FILE).write_bytes(FEED_ANSWER.dump_json(answer))


def read_in_process(content: bytes, declared: str, address: str) -> Feed:
    # The feed that bytes served at an address hold, read by a process of its own within its
    # bounds. The bytes reach it through a file, and the service holds no other copy of them. A
    # process that ends without an answer, as one ended from outside does, is told as the
    # program that failed.
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        (folder / FEED_FILE).write_bytes(content)
        command = [*READER, str(folder), declared, address]
        began = time.monotonic()
        with lineage.started_in_own_session(command, NOTHING_TAKEN, {}) as reader:
            while reader.poll() is None:
                if time.monotonic() - began > READER_LIMIT_SECONDS:
                    raise UnreadableFeed(OUTGROWN.format(source=address))
                time.sleep(READER_WATCH_SECONDS)
        if reader.returncode != 0:
            raise subprocess.CalledProcessError(reader.returncode, command)
        answer = FEED_ANSWER.validate_json((folder / ANSWER_FILE).read_bytes())
    match answer:
        case FeedRead(feed=feed):
            return feed
        case FeedRefused(reason=reason):
            raise UnreadableFeed(reason)


if __name__ == "__main__":
    main()
