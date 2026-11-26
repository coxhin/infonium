import logging
import select
import socket
import ssl
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp
from threading import Lock, local
from typing import Any

import httpcore
import httpx
from PIL import Image
from pydantic import BaseModel

from infonium import egress
from infonium.document import (
    WEB_SCHEMES,
    Extracted,
    ExtractedPicture,
    Figure,
    MediaType,
    PictureSide,
    error_status,
    error_text,
    is_small_picture,
)

# The pictures of a web page's figures, downloaded when the page is read, for the library to
# keep with its document. The most pictures of one page that are downloaded, the first the
# page shows: a page that shows more is a gallery more than an article.
PICTURES_A_PAGE = 200
# The most one picture may bring, which a photograph at a camera's full size stays within, and
# the most all the pictures of one page may bring together.
PICTURE_LIMIT_BYTES = 25 * 1024**2
PAGE_PICTURES_LIMIT_BYTES = 300 * 1024**2
# The longest one picture may take, and all the pictures of one page together, each counted
# from when it begins; a picture not done by then keeps its source. A silent server is given
# up sooner, after the silence a page's fetch allows. Every wait on a connection ends by then,
# but for the making of a connection, which egress gives 15 seconds at most.
PICTURE_LIMIT_SECONDS = 60.0
PAGE_PICTURES_LIMIT_SECONDS = 120.0
# How many pictures are downloaded at a time: as many connections as a browser opens to one
# host.
PICTURES_AT_ONCE = 6
# How many redirects the address of a picture may lead through.
REDIRECTS = 5
# What a browser that shows a picture accepts, but for SVG, which is not kept. The bytes of a
# picture are asked for as they are, since they are compressed already, and a compression that
# is undone could turn a few bytes into very many: an answer compressed all the same is
# refused, and the bytes of every answer are read as they came.
PICTURE_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
AS_THEY_ARE = "identity"
# The kinds of picture kept, those of PICTURE_MEDIA_TYPES, by the names of the formats Pillow
# opens them by; only these are tried. Pillow names a JPEG that holds further pictures MPO
# once it is open, and a browser shows it as its first picture, a JPEG.
PICTURE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "AVIF": "image/avif",
}
OPENED_FORMATS = PICTURE_FORMATS | {"MPO": "image/jpeg"}
SECURE_SCHEME = "https"
CONTENT_TYPE = "content-type"
CONTENT_LENGTH = "content-length"
CONTENT_ENCODING = "content-encoding"
UNDECLARED = ""
PICTURES_FOLDER = "pictures-"
REFERRER = "Referer"
# Why a picture is not kept, beside what a failure of its download says.
TOO_LARGE = "it holds more than 25 MB"
PAGE_TOO_LARGE = "the pictures of the page hold more than 300 MB"
OVERDUE = "it takes longer than 60 seconds"
PAGE_OVERDUE = "the pictures of the page take longer than 120 seconds"
BEYOND = "the page shows more than 200 pictures"
TOO_MANY_REDIRECTS = "it leads through more than 5 redirects"
TIME_UP = "the time of the picture is up"
OTHER_KIND = "its bytes are no JPEG, PNG, GIF, WebP or AVIF picture ({served})"
SERVED_AS = "served as {kind}"
SERVED_UNNAMED = "served as no kind"
ENCODED = "it is served encoded as {coding}, which is not undone"
BOMB = "Pillow takes it for a decompression bomb, at {width} by {height} pixels"
SMALL = "it is small, at {width} by {height} pixels"
# What the log says of a picture that is not kept, and of the pictures of a page.
KEEPS_SOURCE = "the picture %s of %s keeps its source: %s"
DOWNLOADED = "%s: %d of its %d pictures downloaded, %d bytes"

logger = logging.getLogger(__name__)
# The moment, on the monotonic clock, by which a thread that downloads a picture gives it up;
# the thread sets it for each picture, and every wait on a connection it makes ends by then.
picture_ends = local()


class Refused(Exception):
    # A picture that is downloaded no further, or not kept, and why.
    pass


def reason_of(failure: Exception) -> str:
    # Why a picture is not kept: what its refusal says, or the kind of failure it met and what
    # that says.
    said = str(failure).strip()
    if isinstance(failure, Refused) or not said:
        return said or type(failure).__name__
    return f"{type(failure).__name__}: {said}"


class Downloaded(BaseModel):
    # A picture downloaded for the figures that show it: a file of a kind kept, as it was
    # served, and its size.
    file: Path
    media_type: MediaType
    width: PictureSide
    height: PictureSide


def is_readable(connection: socket.socket) -> bool:
    # Whether a read would end at once, which for a connection that waits for its next request
    # is its server having closed it.
    if connection.fileno() < 0:
        return True
    poll = select.poll()
    poll.register(connection, select.POLLIN)
    return bool(poll.poll(0))


def within(timeout: float | None) -> float | None:
    # A wait on a connection, cut to end when the picture it serves is given up, where it serves
    # one; one that would end at once has timed out.
    ends: float | None = getattr(picture_ends, "at", None)
    if ends is None:
        return timeout
    left = ends - time.monotonic()
    if left <= 0:
        raise TimeoutError(TIME_UP)
    return left if timeout is None else min(timeout, left)


@contextmanager
def failing_as(overdue: type[Exception], broken: type[Exception]) -> Iterator[None]:
    # A failure of a connection, as httpcore names it for the step that failed.
    try:
        yield
    except TimeoutError as failure:
        raise overdue(error_text(failure)) from failure
    except OSError as failure:
        raise broken(error_text(failure)) from failure


class JudgedStream(httpcore.NetworkStream):
    # A connection that egress's judgment made, or the secure connection made over it, as
    # httpcore's own streams speak over a socket.
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        with failing_as(httpcore.ReadTimeout, httpcore.ReadError):
            self.connection.settimeout(within(timeout))
            return self.connection.recv(max_bytes)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        with failing_as(httpcore.WriteTimeout, httpcore.WriteError):
            self.connection.settimeout(within(timeout))
            self.connection.sendall(buffer)

    def close(self) -> None:
        self.connection.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        # The certificate is checked for the host the address names, whatever address of it
        # the connection was made to.
        try:
            with failing_as(httpcore.ConnectTimeout, httpcore.ConnectError):
                self.connection.settimeout(within(timeout))
                secured = ssl_context.wrap_socket(self.connection, server_hostname=server_hostname)
        except BaseException:
            self.connection.close()
            raise
        return JudgedStream(secured)

    def get_extra_info(self, info: str) -> Any:
        # httpcore asks of a secure connection the protocol agreed on, which the socket tells.
        match info:
            case "ssl_object" if isinstance(self.connection, ssl.SSLSocket):
                return self.connection
            case "socket":
                return self.connection
            case "client_addr":
                return self.connection.getsockname()
            case "server_addr":
                return self.connection.getpeername()
            case "is_readable":
                return is_readable(self.connection)
            case _:
                return None


class PublicBackend(httpcore.NetworkBackend):
    # Every connection is made by egress's judgment, as the browser's are: the host's name is
    # looked up once, every address it has must be public, and the connection is made to an
    # address that was judged, in the time that judgment allows. A redirect to another host
    # is a connection of its own, and judged as one.
    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        with failing_as(httpcore.ConnectTimeout, httpcore.ConnectError):
            # No connection is begun for a picture whose time is up; the making of one is given
            # the time egress gives it.
            within(timeout)
            return JudgedStream(egress.public_connection(host, port))

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class AnswerBytes(httpx.SyncByteStream):
    # The body of an answer that httpcore read, for httpx.
    def __init__(self, answer: httpcore.Response) -> None:
        self.answer = answer

    def __iter__(self) -> Iterator[bytes]:
        yield from self.answer.iter_stream()

    def close(self) -> None:
        self.answer.close()


class PublicTransport(httpx.BaseTransport):
    # httpx's requests sent over a pool of httpcore's connections, each made through
    # PublicBackend; httpx's own transport takes no backend for its connections.
    def __init__(self) -> None:
        self.pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=PICTURES_AT_ONCE,
            network_backend=PublicBackend(),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        answer = self.pool.handle_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            status_code=answer.status,
            headers=answer.headers,
            stream=AnswerBytes(answer),
            extensions=answer.extensions,
        )

    def close(self) -> None:
        self.pool.close()


def public_transport() -> httpx.BaseTransport:
    # What the pictures of a page are downloaded through.
    return PublicTransport()


def referrer_of(page: str, address: str) -> str | None:
    # What a browser that shows the page sends as the referrer of one of its pictures, by the
    # policy it holds to unless the page names another: the page's address to the page's own
    # origin, the origin alone to any other, and nothing from a secure page to an address that
    # is not. A page without an address on the web, as one uploaded, sends nothing.
    if not page.startswith(WEB_SCHEMES):
        return None
    shown, asked = httpx.URL(page), httpx.URL(address)
    if shown.scheme == SECURE_SCHEME and asked.scheme != SECURE_SCHEME:
        return None
    if (asked.scheme, asked.host, asked.port) == (shown.scheme, shown.host, shown.port):
        return str(shown.copy_with(fragment=None, userinfo=b""))
    return str(httpx.URL(scheme=shown.scheme, host=shown.host, port=shown.port, path="/"))


def served_as(kind: str) -> str:
    # The kind a server gave bytes, as a reason tells it.
    return SERVED_AS.format(kind=kind) if kind else SERVED_UNNAMED


def judged_picture(file: Path, served: str) -> Downloaded:
    # Bytes are kept as they were served when Pillow, reading their header and drawing nothing,
    # finds them a picture of a kind kept, no decompression bomb and not small.
    other_kind = Refused(OTHER_KIND.format(served=served_as(served)))
    try:
        with Image.open(file, formats=list(PICTURE_FORMATS)) as picture:
            kind, (width, height) = picture.format, picture.size
    except Image.DecompressionBombError as bomb:
        raise Refused(error_text(bomb)) from bomb
    except Exception as unread:
        raise other_kind from unread
    media_type = OPENED_FORMATS.get(kind or "")
    if media_type is None:
        raise other_kind
    bound = Image.MAX_IMAGE_PIXELS
    if bound is not None and width * height > bound:
        raise Refused(BOMB.format(width=width, height=height))
    if is_small_picture(width, height):
        raise Refused(SMALL.format(width=width, height=height))
    return Downloaded(file=file, media_type=media_type, width=width, height=height)


class Allowance:
    # What the pictures of one page may still bring, taken as their bytes arrive by the
    # downloads under way at once.
    def __init__(self, size: int) -> None:
        self.left = size
        self.lock = Lock()

    def take(self, size: int) -> bool:
        with self.lock:
            if size > self.left:
                return False
            self.left -= size
            return True


class PageDownloads:
    # The downloads of one page's pictures into a folder of the reading, over one client, by
    # the page's own limits of bytes and time.
    def __init__(self, client: httpx.Client, page: str, folder: Path, silence: float) -> None:
        self.client = client
        self.page = page
        self.folder = folder
        self.silence = silence
        self.deadline = time.monotonic() + PAGE_PICTURES_LIMIT_SECONDS
        self.allowance = Allowance(PAGE_PICTURES_LIMIT_BYTES)

    def outcome(self, number: int, address: str) -> Downloaded | str:
        # A picture downloaded, or why it is not; whatever befalls one picture is told here,
        # and the others go on.
        file = self.folder / str(number)
        try:
            return self.downloaded(address, file)
        except Exception as failure:
            file.unlink(missing_ok=True)
            return reason_of(failure)

    def downloaded(self, address: str, file: Path) -> Downloaded:
        # The address is followed through its redirects, the connection to each host judged as
        # one of its own, and an answer that redirects is let go unread. A wait on a connection
        # that the picture's time runs out in ends then.
        begun = time.monotonic()
        deadline = min(self.deadline, begun + PICTURE_LIMIT_SECONDS)
        overdue = PAGE_OVERDUE if deadline == self.deadline else OVERDUE
        referrer = referrer_of(self.page, address)
        request = self.client.build_request(
            "GET",
            address,
            headers={} if referrer is None else {REFERRER: referrer},
            timeout=self.silence,
        )
        picture_ends.at = deadline
        try:
            for _ in range(REDIRECTS + 1):
                if time.monotonic() >= deadline:
                    raise Refused(overdue)
                answer = self.client.send(request, stream=True)
                try:
                    if answer.next_request is None:
                        return self.picture_of(answer, file, deadline, overdue)
                    request = answer.next_request
                finally:
                    answer.close()
            raise Refused(TOO_MANY_REDIRECTS)
        except httpcore.TimeoutException as late:
            if time.monotonic() >= deadline:
                raise Refused(overdue) from late
            raise
        finally:
            del picture_ends.at

    def picture_of(
        self, answer: httpx.Response, file: Path, deadline: float, overdue: str
    ) -> Downloaded:
        # The picture an answer that redirects nowhere brings, written to the file.
        if not answer.is_success:
            raise Refused(error_status(str(answer.url), answer.status_code))
        codings = answer.headers.get_list(CONTENT_ENCODING, split_commas=True)
        if {coding.lower() for coding in codings} - {UNDECLARED, AS_THEY_ARE}:
            raise Refused(ENCODED.format(coding=answer.headers[CONTENT_ENCODING]))
        declared = answer.headers.get(CONTENT_LENGTH, UNDECLARED)
        if declared.isdigit() and int(declared) > PICTURE_LIMIT_BYTES:
            raise Refused(TOO_LARGE)
        with file.open("wb") as written:
            # Bytes are taken as they arrive, so that a picture that only trickles is still held
            # to the time it may take, and as they came, so that none is decoded.
            for chunk in answer.iter_raw():
                if written.tell() + len(chunk) > PICTURE_LIMIT_BYTES:
                    raise Refused(TOO_LARGE)
                if not self.allowance.take(len(chunk)):
                    raise Refused(PAGE_TOO_LARGE)
                written.write(chunk)
                if time.monotonic() >= deadline:
                    raise Refused(overdue)
        return judged_picture(file, answer.headers.get(CONTENT_TYPE, UNDECLARED))


def with_pictures(
    extracted: Extracted, page: str, directory: Path, user_agent: str, silence: float
) -> Extracted:
    # The text of a page, as the extractor found it, with the pictures of its figures that were
    # downloaded into the reading's directory: each address once, from the address the page
    # was read from (or the name of the file it was uploaded as), by the user agent given, and
    # after a server's silence of the seconds given. A figure whose picture is not kept keeps
    # its source, and is shown from there as before; nothing that befalls a picture fails the
    # reading.
    addresses = list(
        dict.fromkeys(
            block.source
            for block in extracted.blocks
            if isinstance(block, Figure) and block.source.startswith(WEB_SCHEMES)
        )
    )
    if not addresses:
        return extracted
    folder = Path(mkdtemp(prefix=PICTURES_FOLDER, dir=directory))
    outcomes: dict[str, Downloaded | str] = dict.fromkeys(addresses[PICTURES_A_PAGE:], BEYOND)
    taken = addresses[:PICTURES_A_PAGE]
    with (
        httpx.Client(
            transport=public_transport(),
            headers={
                "User-Agent": user_agent,
                "Accept": PICTURE_ACCEPT,
                "Accept-Encoding": AS_THEY_ARE,
            },
            trust_env=False,
        ) as client,
        ThreadPoolExecutor(max_workers=PICTURES_AT_ONCE, thread_name_prefix="picture") as pool,
    ):
        downloads = PageDownloads(client, page, folder, silence)
        outcomes |= dict(
            zip(taken, pool.map(downloads.outcome, range(len(taken)), taken), strict=True)
        )
    for address, outcome in outcomes.items():
        if isinstance(outcome, str):
            logger.info(KEEPS_SOURCE, address, page, outcome)
    downloaded = {
        address: outcome for address, outcome in outcomes.items() if isinstance(outcome, Downloaded)
    }
    logger.info(
        DOWNLOADED,
        page,
        len(downloaded),
        len(addresses),
        sum(picture.file.stat().st_size for picture in downloaded.values()),
    )
    pictures: list[ExtractedPicture] = []
    for number, block in enumerate(extracted.blocks):
        if isinstance(block, Figure) and block.source in downloaded:
            picture = downloaded[block.source]
            pictures.append(
                ExtractedPicture(
                    block=number,
                    file=picture.file,
                    media_type=picture.media_type,
                    width=picture.width,
                    height=picture.height,
                )
            )
    return Extracted(title=extracted.title, blocks=extracted.blocks, pictures=pictures)
