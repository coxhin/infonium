import errno
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from codecs import BOM_UTF8
from contextlib import suppress
from enum import IntEnum, StrEnum
from functools import partial
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from pathlib import Path
from socketserver import BaseRequestHandler, ThreadingTCPServer
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Annotated, Any, Literal
from urllib.parse import urldefrag, urlsplit

from playwright.sync_api import BrowserContext, Page, Request, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as WaitOverdue
from pydantic import BaseModel, Field, TypeAdapter

from infonium.document import (
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    Tagged,
    UnreadableSource,
    error_text,
)

RENDERED_FILE = "rendered"
FIRST_ERROR_STATUS = 400
WEB_ADDRESS_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}
# How long a server is given to answer for the page, how long the page is then given to load
# and after that to fall quiet, and how long what its scripts still do after that is waited
# for, in milliseconds. A page that answers is taken as it stands when it has not loaded or
# not fallen quiet in its time. In a survey of 30 sites the slowest page that was taken
# was taken 26.5 s after its browser started.
ANSWER_LIMIT_MS = 30_000
LOAD_LIMIT_MS = 20_000
QUIET_LIMIT_MS = 8_000
SETTLING_MS = 1_500
# How Chrome names a request it broke off because the network of the machine changed, and
# how many times a page is rendered when that keeps happening. On a machine where containers
# came and went every few seconds, 2 of 35 renderings of one page, and one page of 30 others,
# met a change three times running.
NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED"
RENDERING_ATTEMPTS = 5
# The longest a rendering may take in all, from the start of the browser to the kept file:
# the waits above, the start of the browser, and the page's own work while it is read. A
# page Chrome broke off twice was taken after 30.3 s.
RENDERING_LIMIT_SECONDS = 90.0
# The most a rendered page may hold. The extractor reads no page that is longer than this
# (trafilatura's MAX_FILE_SIZE); the largest page of the survey held 1.4 MB.
RENDERED_LIMIT_BYTES = 20_000_000
# The most memory a page's scripts may hold, in MiB: a page that allocates without end runs
# out of it and its renderer ends, which ends the rendering.
SCRIPT_MEMORY_MIB = 1024
# The most data each process of a rendering may hold: the renderer, Playwright's driver, the
# browser and each process the browser starts. A page's buffers lie outside its scripts'
# memory: without this bound a page held 4.5 GiB of them in three seconds, and with it no
# more than 2 GiB. Half of it is too little to hand the renderer a page of the largest size
# that is kept.
PROCESS_MEMORY_BYTES = 4 * 1024**3

# The browser is driven by a process of its own, this module run as a program, which starts
# the browser, the proxy the browser connects through, and Playwright's driver, so that a
# rendering is ended at its limit together with all of them. It is given the address of the
# page, a folder for its work, and the browser to start; it leaves the page in the folder
# and a report of the rendering beside it. It is to be started as the leader of a session of
# its own, with a pipe for its standard input that the process which starts it holds open and
# writes nothing to: when the pipe closes, the renderer ends the group it leads.
RUN_AS_MODULE = "-m"
RENDERER = [sys.executable, RUN_AS_MODULE, __spec__.name]
RENDERING_REPORT_FILE = "rendering.json"
# How long the process that started the renderer lets pass between two looks at whether it
# has ended, and at whether all it started has ended after it.
RENDERER_WATCH_SECONDS = 0.05
# A look at whether a process has ended that does not wait, and leaves it unreaped.
ENDED_UNREAPED = os.WEXITED | os.WNOHANG | os.WNOWAIT
# The browser makes the socket that keeps it single in the folder for temporary files, whose
# path must stay short enough for a socket's address, so that folder is one of its own made
# where the system keeps temporary files, apart from the work folder.
TEMPORARY_FOLDER_VARIABLE = "TMPDIR"
PROFILE_FOLDER = "profile"
BROWSER_LOG_FILE = "browser.log"
# The browser writes the port it takes for Playwright's connection into this file of its
# profile, on the first of two lines.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
DEVTOOLS_PORT_LINES = 2
LOOPBACK = "127.0.0.1"
EVERY_PORT = 0

# The proxy speaks SOCKS 5, which carries every connection a browser opens, a WebSocket and
# a secure connection alike, as a plain connection to a named host and port, and through
# which Chrome sends the name of a host unresolved. No library of SOCKS or HTTP proxies lets
# its caller decide on the address it resolved and then connect to that same address.
SOCKS_VERSION = 5
NO_AUTHENTICATION = 0
CONNECT = 1
SOCKS_HEADER_BYTES = 4
SOCKS_PORT_BYTES = 2
IPV4_BYTES = 4
IPV6_BYTES = 16
# How long a host is given to take a connection, and how much of a connection is passed on
# at a time.
CONNECTION_LIMIT_SECONDS = 15.0
RELAYED_BYTES = 64 * 1024
# IPv6 prefixes whose last 32 bits are an IPv4 address that a host or a gateway connects
# to: the IPv4-compatible form and the prefix of translation by NAT64.
IPV4_CARRYING_PREFIXES = (IPv6Network("::/96"), IPv6Network("64:ff9b::/96"))
IPV4_PART = 0xFFFF_FFFF


class AddressKind(IntEnum):
    IPV4 = 1
    NAME = 3
    IPV6 = 4


class SocksReply(IntEnum):
    SUCCEEDED = 0
    GENERAL_FAILURE = 1


# The address in a reply, which no browser reads: a zero IPv4 address and port.
SOCKS_REPLY_ADDRESS = bytes([AddressKind.IPV4, *bytes(IPV4_BYTES), *bytes(SOCKS_PORT_BYTES)])

# What the browser is started with. It renders without a window, in a profile of its own;
# Playwright connects to it on a port of loopback that the browser chooses. Every connection
# it opens goes through the proxy, those to loopback too, which a browser otherwise opens
# itself; it looks up no name itself, so a name is looked up only by the proxy; and it sends
# no WebRTC over UDP, which the proxy cannot carry. It sends no reports of its crashes, and
# fewer requests of its own.
BROWSER_FLAGS = [
    "--headless",
    f"--remote-debugging-port={EVERY_PORT}",
    "--proxy-bypass-list=<-loopback>",
    f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {LOOPBACK}",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-crash-reporter",
    "--disable-breakpad",
    "--no-first-run",
    "--no-default-browser-check",
    f"--js-flags=--max-old-space-size={SCRIPT_MEMORY_MIB}",
]
BLANK_PAGE = "about:blank"


class RenderedPage(BaseModel):
    # A page as a browser built it: its HTML once its scripts have run, kept as a file, and
    # the address the browser ended at.
    file: Path
    address: str


class TakenPage(BaseModel):
    # The HTML of a page as its browser holds it, and the address the browser ended at.
    content: str
    address: str


class Refusal(BaseModel):
    # A connection the browser asked the proxy for and did not get, and why.
    host: str
    port: int
    reason: str


class RenderingReportKind(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"


class CompletedRendering(Tagged):
    kind: Literal[RenderingReportKind.COMPLETED] = RenderingReportKind.COMPLETED
    address: str


class AbortedRendering(Tagged):
    # Why a page was not rendered, in one line a person reads.
    kind: Literal[RenderingReportKind.ABORTED] = RenderingReportKind.ABORTED
    reason: str


# What the renderer tells the process that started it of how the rendering ended.
RenderingReport = Annotated[CompletedRendering | AbortedRendering, Field(discriminator="kind")]
RENDERING_REPORT = TypeAdapter(RenderingReport)


def is_public(address: IPv4Address | IPv6Address) -> bool:
    # An address is public when it, and every IPv4 address it carries inside it, is reachable
    # across the internet and names one host.
    carried = (
        []
        if isinstance(address, IPv4Address)
        else [
            *[inner for inner in (address.ipv4_mapped, address.sixtofour) if inner is not None],
            *(address.teredo or ()),
            *[
                IPv4Address(int(address) & IPV4_PART)
                for prefix in IPV4_CARRYING_PREFIXES
                if address in prefix
            ],
        ]
    )
    return all(each.is_global and not each.is_multicast for each in (address, *carried))


def public_connection(host: str, port: int) -> socket.socket:
    # A connection to a host, made to the address that was judged: the name is looked up once.
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG)
    for *_, place in found:
        if not is_public(ip_address(place[0])):
            raise UnreadableSource(f"{place[0]} is not a public address")
    family, kind, protocol, _, place = found[0]
    connection = socket.socket(family, kind, protocol)
    connection.settimeout(CONNECTION_LIMIT_SECONDS)
    connection.connect(place)
    connection.settimeout(None)
    return connection


def received(connection: socket.socket, size: int) -> bytes:
    content = connection.recv(size, socket.MSG_WAITALL)
    if len(content) < size:
        raise ConnectionError(f"the browser closed the connection to the proxy after {content!r}")
    return content


def relayed(source: socket.socket, sink: socket.socket) -> None:
    # Passes on what one side sends until it closes, or either side breaks the connection off.
    with suppress(OSError):
        while chunk := source.recv(RELAYED_BYTES):
            sink.sendall(chunk)
        sink.shutdown(socket.SHUT_WR)


class GuardedProxy(ThreadingTCPServer):
    # The proxy a browser connects through. It connects only to public addresses, and keeps
    # what it refused.
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__((LOOPBACK, EVERY_PORT), SocksRequest)
        self.refusals: list[Refusal] = []

    def port(self) -> int:
        return int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A connection the browser breaks off while it asks for a host ends there; the browser
        # opens connections ahead of its needs and drops those it does not use.
        if not isinstance(sys.exception(), OSError):
            super().handle_error(request, client_address)


class SocksRequest(BaseRequestHandler):
    # One connection the browser asks the proxy for, carried until either side closes it.
    server: GuardedProxy

    def handle(self) -> None:
        client: socket.socket = self.request
        _, method_count = received(client, 2)
        received(client, method_count)
        client.sendall(bytes([SOCKS_VERSION, NO_AUTHENTICATION]))
        _, command, _, kind = received(client, SOCKS_HEADER_BYTES)
        match AddressKind(kind):
            case AddressKind.IPV4:
                host = str(IPv4Address(received(client, IPV4_BYTES)))
            case AddressKind.IPV6:
                host = str(IPv6Address(received(client, IPV6_BYTES)))
            case AddressKind.NAME:
                host = received(client, received(client, 1)[0]).decode()
        port = int.from_bytes(received(client, SOCKS_PORT_BYTES))
        if command != CONNECT:
            client.sendall(bytes([SOCKS_VERSION, SocksReply.GENERAL_FAILURE, 0]))
            return
        # The browser is told only that the connection failed; the proxy keeps the reason,
        # for the report of a page that could not be opened.
        try:
            upstream = public_connection(host, port)
        except (OSError, UnreadableSource) as refused:
            self.server.refusals.append(Refusal(host=host, port=port, reason=error_text(refused)))
            client.sendall(bytes([SOCKS_VERSION, SocksReply.GENERAL_FAILURE, 0]))
            return
        with upstream:
            client.sendall(bytes([SOCKS_VERSION, SocksReply.SUCCEEDED, 0, *SOCKS_REPLY_ADDRESS]))
            answers = Thread(target=relayed, args=(upstream, client), daemon=True)
            answers.start()
            relayed(client, upstream)
            answers.join()


def browser_command(program: str, profile: Path, proxy: GuardedProxy) -> list[str]:
    return [
        program,
        *BROWSER_FLAGS,
        f"--user-data-dir={profile}",
        f"--proxy-server=socks5://{LOOPBACK}:{proxy.port()}",
        BLANK_PAGE,
    ]


def devtools_endpoint(program: str, work: Path, proxy: GuardedProxy) -> str:
    # Starts the browser in the renderer's group and waits for the port it listens on.
    profile = work / PROFILE_FOLDER
    port_file = profile / DEVTOOLS_PORT_FILE
    log = work / BROWSER_LOG_FILE
    with log.open("wb") as written:
        browser = subprocess.Popen(
            browser_command(program, profile, proxy),
            stdin=subprocess.DEVNULL,
            stdout=written,
            stderr=written,
        )
    while not port_file.exists() or len(port_file.read_text().splitlines()) < DEVTOOLS_PORT_LINES:
        if browser.poll() is not None:
            said = log.read_text(errors="replace").strip().splitlines() or [""]
            raise UnreadableSource(
                f"the browser {program} ended with status {browser.returncode} as it started: "
                f"{said[-1]}"
            )
        time.sleep(RENDERER_WATCH_SECONDS)
    return f"http://{LOOPBACK}:{port_file.read_text().split()[0]}"


def page_content(page: Page, url: str) -> str:
    # The page once it is done: its server has answered, it has loaded and fallen quiet, or
    # its time for these has passed, and its scripts have had a moment more.
    answer = page.goto(url, wait_until="commit", timeout=ANSWER_LIMIT_MS)
    if answer is not None and answer.status >= FIRST_ERROR_STATUS:
        raise UnreadableSource(f"{answer.url} answered {answer.status}")
    with suppress(WaitOverdue):
        page.wait_for_load_state("load", timeout=LOAD_LIMIT_MS)
    with suppress(WaitOverdue):
        page.wait_for_load_state("networkidle", timeout=QUIET_LIMIT_MS)
    page.wait_for_timeout(SETTLING_MS)
    return page.content()


def note_interruption(interrupted: list[str], request: Request) -> None:
    if request.failure == NETWORK_CHANGED:
        interrupted.append(request.url)


def taken_page(context: BrowserContext, url: str, hops: list[str]) -> TakenPage:
    # Chrome breaks off every request it has under way when an address of this machine
    # changes, as one does each time a container starts or stops beside it; a page whose
    # scripts were broken off so comes out as the empty shell they would have filled. Such a
    # page is rendered again, in a page of its own, so that the error page Chrome shows for an
    # address it broke off does not take the place of the next attempt. The addresses the page
    # itself was asked at, a redirect after another, are noted in `hops`.

    def note_hop(request: Request) -> None:
        if request.is_navigation_request() and request.frame.parent_frame is None:
            hops.append(request.url)

    for _ in range(RENDERING_ATTEMPTS):
        page = context.new_page()
        interrupted: list[str] = []
        page.on("request", note_hop)
        page.on("requestfailed", partial(note_interruption, interrupted))
        try:
            content = page_content(page, url)
        except PlaywrightError:
            if not interrupted:
                raise
            continue
        if not interrupted:
            return TakenPage(content=content, address=urldefrag(page.url).url)
        page.close()
    raise UnreadableSource(
        f"the network of this machine changed each time {url} was rendered, "
        f"{RENDERING_ATTEMPTS} times"
    )


def rendering(url: str, work: Path, program: str, proxy: GuardedProxy, hops: list[str]) -> str:
    # Renders the page into the work folder and gives the address the browser ended at.
    endpoint = devtools_endpoint(program, work, proxy)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        # A context of its own keeps nothing on disk; no download is taken and no service
        # worker is let in.
        context = browser.new_context(accept_downloads=False, service_workers="block")
        taken = taken_page(context, url, hops)
    # The page is kept as UTF-8 with its mark in front, which a browser and the extractor
    # both read before any charset the page declares in its head or a server in a header.
    kept = BOM_UTF8 + taken.content.encode()
    if len(kept) > RENDERED_LIMIT_BYTES:
        raise UnreadableSource(OVERSIZE_SOURCE.format(source=url))
    (work / RENDERED_FILE).write_bytes(kept)
    return taken.address


def ending_reason(ending: Exception, hops: list[str], refusals: list[Refusal]) -> str:
    # Why a rendering ended, in one line: the proxy's reason when it refused the last address
    # the page was asked at, and otherwise the first line of the error, without the call log
    # that follows an error of Playwright.
    last_hop = urlsplit(hops[-1]) if hops else None
    refused = [
        refusal.reason
        for refusal in refusals
        if last_hop is not None
        and refusal.host == last_hop.hostname
        and refusal.port == (last_hop.port or DEFAULT_PORTS.get(last_hop.scheme))
    ]
    if refused:
        return f"{hops[-1]} could not be opened: {refused[0]}"
    return error_text(ending).splitlines()[0]


def end_when_abandoned() -> None:
    # The process that started the renderer holds the other end of its standard input open
    # and writes nothing to it. When that end closes, that process is gone, so the renderer
    # ends the group it leads: itself, the browser and Playwright's driver.
    os.read(sys.stdin.fileno(), 1)
    os.killpg(os.getpid(), signal.SIGKILL)


def main() -> None:
    # What this module does when it is run as a program: it renders the page it is given
    # into the folder it is given, and writes the report. An exception does not cross from
    # one process to another, so whatever ends the rendering is reported in one line, and
    # where it arose is written to standard error, which is the starting process's own.
    url, work, program = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    # A hard limit already in force that is lower than the rendering's own cannot be
    # raised, and is kept.
    hard_limit = resource.getrlimit(resource.RLIMIT_DATA)[1]
    data_limit = (
        PROCESS_MEMORY_BYTES
        if hard_limit == resource.RLIM_INFINITY
        else min(PROCESS_MEMORY_BYTES, hard_limit)
    )
    resource.setrlimit(resource.RLIMIT_DATA, (data_limit, data_limit))
    Thread(target=end_when_abandoned, daemon=True).start()
    proxy = GuardedProxy()
    Thread(target=proxy.serve_forever, daemon=True).start()
    hops: list[str] = []
    try:
        report: RenderingReport = CompletedRendering(
            address=rendering(url, work, program, proxy, hops)
        )
    except Exception as ending:
        traceback.print_exc()
        report = AbortedRendering(reason=ending_reason(ending, hops, proxy.refusals))
    (work / RENDERING_REPORT_FILE).write_bytes(RENDERING_REPORT.dump_json(report))


def reported_rendering(url: str, work: Path, program: str) -> RenderingReport:
    # The rendering of a page as the renderer reports it. Whatever the renderer started is
    # ended with it, and gone, before this returns or raises.
    command = [*RENDERER, url, str(work), program]
    began = time.monotonic()
    with TemporaryDirectory() as temporary:
        environment = {**os.environ, TEMPORARY_FOLDER_VARIABLE: temporary}
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, start_new_session=True, env=environment
        ) as renderer:
            try:
                # The renderer is looked at without being reaped, so that its group keeps its
                # number until all of it is ended.
                while os.waitid(os.P_PID, renderer.pid, ENDED_UNREAPED) is None:
                    if time.monotonic() - began > RENDERING_LIMIT_SECONDS:
                        raise UnreadableSource(OVERDUE_SOURCE.format(source=url))
                    time.sleep(RENDERER_WATCH_SECONDS)
            finally:
                os.killpg(renderer.pid, signal.SIGKILL)
                renderer.wait()
                with suppress(ProcessLookupError):
                    while True:
                        os.killpg(renderer.pid, 0)
                        time.sleep(RENDERER_WATCH_SECONDS)
    # A renderer that ends with a failing status has written no report.
    if renderer.returncode != 0:
        raise subprocess.CalledProcessError(renderer.returncode, command)
    return RENDERING_REPORT.validate_json((work / RENDERING_REPORT_FILE).read_bytes())


def rendered_page(url: str, directory: Path, browser: Path) -> RenderedPage:
    if urlsplit(url).scheme not in WEB_ADDRESS_SCHEMES:
        raise UnreadableSource(f"infonium renders only web pages, and {url} is not one")
    program = shutil.which(browser)
    if program is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(browser))
    file = directory / RENDERED_FILE
    with TemporaryDirectory(dir=directory) as work:
        match reported_rendering(url, Path(work), program):
            case CompletedRendering(address=address):
                (Path(work) / RENDERED_FILE).replace(file)
            case AbortedRendering(reason=reason):
                raise UnreadableSource(reason)
    return RenderedPage(file=file, address=address)


if __name__ == "__main__":
    main()
