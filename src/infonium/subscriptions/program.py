import os
import re
import subprocess
import sys
import traceback
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Annotated, Literal
from urllib.parse import quote, quote_plus

import httpx
from pydantic import Field, SecretStr, TypeAdapter, ValidationError

from infonium import lineage
from infonium.document import OVERDUE_SOURCE, Tagged, error_text
from infonium.subscriptions.discovery import (
    Discovered,
    discovered_feed,
    is_route,
    polled_route,
    rsshub_query,
    with_advice,
)
from infonium.subscriptions.feed import UnreadableFeed
from infonium.subscriptions.fetcher import Fetched, Validators, fetched_feed, shown, unreached

# The fetcher of feeds that infonium has built in: a program of the kind a plugin's fetcher is,
# run once for each operation, its command and its answer in JSON. It polls a feed, or finds
# the feed an address leads to, and reads it, fetching and reading alike in a process of its
# own, as a media page is fetched in one: no check from inside a process holds every bound
# against another site's bytes, which may inflate, trickle, or make a parser or a pattern
# grow. The service parses, matches and inflates none of them.
FETCHER = [sys.executable, lineage.RUN_AS_MODULE, __spec__.name]
# It takes, of infonium's environment, the PATH, the locale and the proxies, which httpx reads.
FETCHER_TAKES = re.compile("|".join((lineage.SEARCH_PATH, lineage.LOCALE, lineage.PROXIES)))
# The most data it may hold, its start among it: 20 MiB of unclosed elements took feedparser
# 37 s and 3.2 GiB. The bound is on data, not on address space, of which a program reserves
# many times what it uses. And the longest it may run: a poll's or a discovery's own minute,
# and room to start and to answer.
FETCHER_MEMORY_BYTES = 512 * 1024**2
FETCHER_LIMIT_SECONDS = 75.0
# Memory it holds from its start and gives up first when an operation ends without its result,
# so that it has room left to write its answer.
ANSWER_ROOM_BYTES = 4 * 1024**2
# The folder it is given holds its command and takes its answer. The key of the RSSHub
# instance is given in its environment alone, so that a folder left behind holds no key; and it
# is taken out of whatever the program writes, should a sentence or a traceback carry it.
COMMAND_FILE = "command.json"
ANSWER_FILE = "answer.json"
KEY_VARIABLE = "RSSHUB_ACCESS_KEY"
KEY_SHOWN = "…"
# What the service is told of an operation that ended without its result.
OUTGROWN = "{source} holds more than infonium reads"
# Endings that polls meet all the time, and a bound that a feed passed, whose place in the code
# says nothing.
EXPECTED_ENDINGS = (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL, MemoryError)
UNANSWERED = "the fetcher of {source} ended without an answer, with status {status}"


class OperationKind(StrEnum):
    POLL = "poll"
    DISCOVERY = "discovery"


class Poll(Tagged):
    # A feed's address as a subscription keeps it, a web address or a route, where the poll
    # starts from, and the RSSHub instance that serves a route.
    kind: Literal[OperationKind.POLL] = OperationKind.POLL
    address: str
    validators: Validators
    rsshub: str


class Discovery(Tagged):
    # The address a reader gave, and the RSSHub instance that serves a route.
    kind: Literal[OperationKind.DISCOVERY] = OperationKind.DISCOVERY
    address: str
    rsshub: str


Operation = Annotated[Poll | Discovery, Field(discriminator="kind")]
OPERATION = TypeAdapter(Operation)


class AnswerKind(StrEnum):
    POLLED = "polled"
    FOUND = "found"
    REFUSED = "refused"


class Polled(Tagged):
    kind: Literal[AnswerKind.POLLED] = AnswerKind.POLLED
    fetched: Fetched


class Found(Tagged):
    kind: Literal[AnswerKind.FOUND] = AnswerKind.FOUND
    discovered: Discovered


class Refused(Tagged):
    # Why the operation ended without its result, in the words a reader is told.
    kind: Literal[AnswerKind.REFUSED] = AnswerKind.REFUSED
    reason: str


FetcherAnswer = Annotated[Polled | Found | Refused, Field(discriminator="kind")]
FETCHER_ANSWER = TypeAdapter(FetcherAnswer)
# What the service takes as the answer to each operation.
POLL_ANSWER = TypeAdapter(Annotated[Polled | Refused, Field(discriminator="kind")])
DISCOVERY_ANSWER = TypeAdapter(Annotated[Found | Refused, Field(discriminator="kind")])


def nothing_left() -> None:
    # The fetcher starts no program, so once its service is gone it has nothing to end but
    # itself.
    return None


def carried_out(operation: Operation, key: str | None) -> FetcherAnswer:
    match operation:
        case Poll(address=address, validators=validators, rsshub=rsshub) if is_route(address):
            return Polled(fetched=polled_route(address, validators, rsshub, key))
        case Poll(address=address, validators=validators):
            return Polled(fetched=fetched_feed(address, validators))
        case Discovery(address=address, rsshub=rsshub):
            return Found(discovered=discovered_feed(address, rsshub, key))


def failed_at(failure: httpx.HTTPError, address: str, key: str | None) -> str:
    # The address whose request failed, where httpx tells it, without the key.
    try:
        return shown(failure.request.url, rsshub_query(key))
    except RuntimeError:
        return address


def without_key(written: str, key: str | None) -> str:
    # What the program writes, with the key taken out however an address wrote it.
    for form in [key, quote(key, safe=""), quote_plus(key)] if key else []:
        written = written.replace(form, KEY_SHOWN)
    return written


def refusal(operation: Operation, said: str) -> str:
    # Every refusal of a discovery ends with what to give instead, as discovered_feed's own do.
    return with_advice(said) if isinstance(operation, Discovery) else said


def told(operation: Operation, ending: Exception, key: str | None) -> str:
    # What ended an operation, in the words a reader is told: what it refused as it stands, a
    # failure of the connection with the address and what failed, and memory run out as a
    # source that holds more than infonium reads.
    match ending:
        case UnreadableFeed():
            return str(ending)
        case httpx.HTTPError():
            said = unreached(failed_at(ending, operation.address, key), ending)
        case httpx.InvalidURL():
            said = unreached(operation.address, ending)
        case MemoryError():
            said = OUTGROWN.format(source=operation.address)
        case _:
            said = error_text(ending)
    return refusal(operation, said)


def main() -> None:
    # What this module does when it is run as a program: it carries out the operation in the
    # folder it is given and writes its answer beside it. An exception does not cross from one
    # process to another, so whatever ends the operation is answered in the words it would give
    # a reader. Where a fault arose that is neither a refusal, a failure of the connection nor
    # a bound, which polls meet all the time, is written to standard error, the service's own.
    Thread(target=lineage.end_when_abandoned, args=(nothing_left,), daemon=True).start()
    lineage.hold_data_to(FETCHER_MEMORY_BYTES)
    room = bytearray(ANSWER_ROOM_BYTES)
    folder = Path(sys.argv[1])
    operation = OPERATION.validate_json((folder / COMMAND_FILE).read_bytes())
    key = os.environ.get(KEY_VARIABLE) or None
    try:
        answer = carried_out(operation, key)
    except EXPECTED_ENDINGS as ending:
        del room
        answer = Refused(reason=without_key(told(operation, ending, key), key))
    except Exception as ending:
        del room
        sys.stderr.write(without_key(traceback.format_exc(), key))
        answer = Refused(reason=without_key(told(operation, ending, key), key))
    (folder / ANSWER_FILE).write_bytes(FETCHER_ANSWER.dump_json(answer))


def answered_apart[Answer](
    operation: Operation, answers: TypeAdapter[Answer], key: SecretStr | None
) -> Answer:
    # The answer of the fetcher to an operation, started in a session of its own with the key
    # of the RSSHub instance, and ended with all of it at its limit. Whatever ends it without
    # an answer, or with one that is not what was asked for, is refused as the address it was
    # given.
    given = {KEY_VARIABLE: key.get_secret_value()} if key else {}
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        (folder / COMMAND_FILE).write_bytes(OPERATION.dump_json(operation))
        command = [*FETCHER, directory]
        with lineage.started_in_own_session(command, FETCHER_TAKES, given) as fetcher:
            try:
                status = fetcher.wait(timeout=FETCHER_LIMIT_SECONDS)
            except subprocess.TimeoutExpired as overdue:
                said = OVERDUE_SOURCE.format(source=operation.address)
                raise UnreadableFeed(refusal(operation, said)) from overdue
        unanswered = UNANSWERED.format(source=operation.address, status=status)
        if status != 0:
            raise UnreadableFeed(refusal(operation, unanswered))
        try:
            return answers.validate_json((folder / ANSWER_FILE).read_bytes())
        except (OSError, ValidationError) as failure:
            raise UnreadableFeed(refusal(operation, unanswered)) from failure


def polled_apart(
    address: str, validators: Validators, rsshub: str, key: SecretStr | None = None
) -> Fetched:
    # One poll of a feed by the fetcher, from where it stopped: a route is asked of the RSSHub
    # instance given at the time of the poll, with its key. Whatever ends it without the feed
    # is raised as an UnreadableFeed, a failure of the connection among them.
    operation = Poll(address=address, validators=validators, rsshub=rsshub)
    match answered_apart(operation, POLL_ANSWER, key):
        case Polled(fetched=fetched):
            return fetched
        case Refused(reason=reason):
            raise UnreadableFeed(reason)


def discovered_apart(address: str, rsshub: str, key: SecretStr | None = None) -> Discovered:
    # The feed an address a reader gave leads to, found by the fetcher. Whatever ends it
    # without a feed is raised as an UnreadableFeed that tells what to give instead.
    match answered_apart(Discovery(address=address, rsshub=rsshub), DISCOVERY_ANSWER, key):
        case Found(discovered=discovered):
            return discovered
        case Refused(reason=reason):
            raise UnreadableFeed(reason)


if __name__ == "__main__":
    main()
