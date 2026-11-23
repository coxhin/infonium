import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import traceback
from codecs import BOM_UTF8
from collections.abc import Iterator
from contextlib import suppress
from enum import StrEnum
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Annotated, Literal
from urllib.parse import urldefrag, urlsplit

from playwright.sync_api import BrowserContext, Page, Request, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as WaitOverdue
from pydantic import BaseModel, Field, TypeAdapter

from infonium import confinement, lineage
from infonium.document import (
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    Tagged,
    UnreadableSource,
    error_status,
    error_text,
)
from infonium.egress import LOOPBACK, PROXY_SCHEME, PublicProxy, Refusal

RENDERED_FILE = "rendered"
# A page's scripts can leave half of a UTF-16 pair in its text, which UTF-8 cannot hold. A
# browser that encodes such text puts U+FFFD in its place, and so does the renderer, with what
# UTF-16 pairs kept as one character, before anything else touches a string taken from a page.
SURROGATE_ENCODING = "utf-16-le"
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
MS_PER_SECOND = 1000
# The longest one attempt at a page takes: no attempt begins that the time left cannot hold.
ATTEMPT_LIMIT_SECONDS = (ANSWER_LIMIT_MS + LOAD_LIMIT_MS + QUIET_LIMIT_MS + SETTLING_MS) / (
    MS_PER_SECOND
)
# Chrome breaks off every request it has under way when an address of this machine changes,
# as one does each time a container starts or stops beside it, and names such a request so.
# A page is rendered again, a few times at most, when what broke off was its own document,
# a script, or a request its scripts made: what fills a page that its scripts build.
NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED"
RENDERING_ATTEMPTS = 5
# Such changes come in bursts, and it is a change of an address that Chrome breaks its
# requests off at: each time it did so here, an address had changed within 0.83 s before, 0.04 s
# at the median, where a moment taken at random lay 0.33 s after one; the links of this
# machine change ten times as often. A page is rendered again only once no address has changed
# for a second, or when the time left holds one attempt more and no longer: under 43 changes a
# second, such a second came every 9 s, and one without a change of any kind every 25 s. Linux
# tells of each change on a routing socket, to the groups of the IPv4 and the IPv6 addresses.
QUIET_NETWORK_SECONDS = 1.0
IPV4_ADDRESS_CHANGES = 0x10
IPV6_ADDRESS_CHANGES = 0x100
ADDRESS_CHANGES = IPV4_ADDRESS_CHANGES | IPV6_ADDRESS_CHANGES
CHANGE_BYTES = 65536
DOCUMENT_RESOURCE = "document"
SCRIPTED_RESOURCES = frozenset({"script", "xhr", "fetch"})
# How Chrome names a request the proxy did not connect.
PROXY_FAILURE = "net::ERR_SOCKS_CONNECTION_FAILED"
# What Playwright adds to an error of a browser that has ended, as it started or later: the
# lines the browser wrote, each marked with its process, between the first mark and the
# steps Playwright took; Chrome begins a line of its log with where it was written.
BROWSER_LOGS = "Browser logs:"
CALL_LOG = "Call log:"
WRITTEN_LINE = re.compile(
    r"^\[pid=\d+\]\[(?:err|out)\] (?:\[[^\]]*\] )?(?P<said>.+)$", re.MULTILINE
)
# What a person is told of a page that met a limit of infonium's own, or an error that Chrome
# names: by the mark in Playwright's error, looked for in the error without the addresses it
# names, which a page chooses; or for another error of Chrome's, its name.
NO_ANSWER = "{source} did not answer within {seconds:g} s"
WEB_ADDRESS = re.compile(r"\S+://\S*")
CHROME_ERRORS = {
    "crashed": "the page at {source} crashed in the browser",
    "net::ERR_NETWORK_CHANGED": "the network of this machine changed while {source} was loading",
    "net::ERR_UNSAFE_REDIRECT": "{source} led the browser to an address that is not a web page",
    "net::ERR_ABORTED": "loading {source} was broken off",
    "Download is starting": "{source} is a file to download, not a page",
    "has been closed": "the browser {program} ended while {source} was loading",
}
CHROME_ERROR = re.compile(r"net::ERR_\w+")
OTHER_CHROME_ERROR = "the browser could not open {source}: {error}"
# The longest a rendering may take in all, from the start of the browser to the kept file:
# the waits above, the start of the browser, and the page's own work while it is read. The
# renderer begins no attempt that would end later than a few seconds before it, which it
# leaves to the report. A page Chrome broke off twice was taken after 30.3 s.
RENDERING_LIMIT_SECONDS = 90.0
REPORTING_SECONDS = 5.0
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
# The most a whole rendering may hold, in the memory of all its processes that they share
# with no other, and on disk in its work folder, both with the files the browser shares its
# memory through; a page that holds more is ended. itmedia-jp, the page of a survey of 30
# sites that held the most, held 1.3 GiB, and 344 MiB on disk.
RENDERING_MEMORY_BYTES = 3 * 1024**3
RENDERING_DISK_BYTES = 1024**3
TOO_MUCH_MEMORY = "{source} needs more memory than infonium gives a page"

# The browser is driven by a process of its own, this module run as a program and held by
# lineage, which starts the proxy the browser connects through and Playwright, which starts
# the browser. It is given a folder for its work, the browser to start and the moment, on the
# system's monotonic clock, by which it is to be done, and finds the address of the page in a
# file of the folder, where no other user of this machine sees it and no length limits it; it
# leaves the page in the folder and a report of the rendering beside it, and then waits to be
# ended.
RENDERER = (sys.executable, lineage.RUN_AS_MODULE, __spec__.name)
ADDRESS_FILE = "address"
RENDERING_REPORT_FILE = "rendering.json"
WRITTEN_REPORT_FILE = "rendering.json.part"
# How long the process that started the renderer lets pass from one look to the next at
# whether it is done and at what the rendering holds; and how many entries of the work folder
# each look walks for the disk bound, so that no look takes long however many files a page
# makes the browser keep: a whole walk of a fresh profile takes one look, of 20,000 files 40.
RENDERER_WATCH_SECONDS = 0.05
FOLDER_ENTRIES_PER_LOOK = 500
# The browser makes the socket that keeps it single in the folder for temporary files, whose
# path must stay short enough for a socket's address, so that folder is one of its own made
# where the system keeps temporary files, apart from the work folder.
TEMPORARY_FOLDER_VARIABLE = "TMPDIR"
# The browser keeps its profile, and writes what it keeps beside a profile, its reports of
# crashes among them, under a home of its own in the work folder.
PROFILE_FOLDER = "profile"
HOME_FOLDER = "home"
HOME_VARIABLES = {
    "HOME": ".",
    "XDG_CONFIG_HOME": ".config",
    "XDG_CACHE_HOME": ".cache",
    "XDG_DATA_HOME": ".local/share",
}
# The renderer, and the browser it starts, are given an environment of their own: of
# infonium's, only where programs are found, the language and the time zone; besides these,
# the home in the work folder and the folder for temporary files. Nothing else of infonium's,
# its keys least, reaches the processes that run a page.
PASSED_VARIABLES = frozenset({"PATH", "TZ", "LANG", "LANGUAGE"})
LOCALE_PREFIX = "LC_"
# The profile's preferences, written before the browser starts: WebRTC sends nothing over
# UDP, which the proxy cannot carry, and binds no UDP port but 1, which a process without
# privilege cannot bind: each WebRTC connection a page opened held a UDP port open on every
# interface for as long as it lasted. Where every process may bind every port, as in a
# container, one socket stands on port 1.
PREFERENCES_FILE = Path("Default") / "Preferences"
WEBRTC_POLICY = "disable_non_proxied_udp"
PROFILE_PREFERENCES = {
    "webrtc": {
        "ip_handling_policy": WEBRTC_POLICY,
        "udp_port_range": f"{confinement.WEBRTC_PORT}-{confinement.WEBRTC_PORT}",
    }
}
# The browser's own state, written before it starts: WebRTC puts no names in place of this
# machine's addresses, names the browser would listen for on the networks of all its
# interfaces as soon as a page opens a WebRTC connection. It is turned off as chrome://flags
# turns it off (option 2), since a switch of features replaces the one Playwright passes.
LOCAL_STATE_FILE = "Local State"
BROWSER_STATE = {
    "browser": {"enabled_labs_experiments": ["enable-webrtc-hide-local-ips-with-mdns@2"]}
}

# What the browser is started with besides what Playwright gives it. Every connection it
# opens goes through the proxy, those to loopback too, which a browser otherwise opens
# itself; it looks up no name itself, so a name is looked up only by the proxy; and it sends
# no WebRTC over UDP.
BROWSER_FLAGS = (
    "--proxy-bypass-list=<-loopback>",
    f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {LOOPBACK}",
    f"--webrtc-ip-handling-policy={WEBRTC_POLICY}",
    f"--js-flags=--max-old-space-size={SCRIPT_MEMORY_MIB}",
)
# The proxy the browser connects through, named with the port it took as it started.
PROXY_SERVER_FLAG = "--proxy-server={scheme}://{host}:{port}"


class RenderedPage(BaseModel):
    # A page as a browser built it: its HTML once its scripts have run, kept as a file, the
    # address the browser ended at, and whether a change of this machine's network broke off
    # the attempt that was kept, as it broke off every attempt that was taken. Such a page may
    # hold less than the page does, or nothing to read.
    file: Path
    address: str
    broken_off_by_network: bool


class TakenPage(BaseModel):
    # The HTML of a page as its browser holds it, the address the browser ended at, and
    # whether a change of this machine's network broke the attempt off.
    content: str
    address: str
    broken_off_by_network: bool = False


class FailedHop(BaseModel):
    # An address the page itself was asked at that the browser could not open, and why.
    url: str
    failure: str


class RenderingReportKind(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"


class CompletedRendering(Tagged):
    kind: Literal[RenderingReportKind.COMPLETED] = RenderingReportKind.COMPLETED
    address: str
    broken_off_by_network: bool


class AbortedRendering(Tagged):
    # Why a page was not rendered, in one line a person reads.
    kind: Literal[RenderingReportKind.ABORTED] = RenderingReportKind.ABORTED
    reason: str


# What the renderer tells the process that started it of how the rendering ended.
RenderingReport = Annotated[CompletedRendering | AbortedRendering, Field(discriminator="kind")]
RENDERING_REPORT = TypeAdapter(RenderingReport)


def renderer_environment(work: Path, temporary: Path) -> dict[str, str]:
    home = work / HOME_FOLDER
    return {
        **{
            name: value
            for name, value in os.environ.items()
            if name in PASSED_VARIABLES or name.startswith(LOCALE_PREFIX)
        },
        **{name: str(home / part) for name, part in HOME_VARIABLES.items()},
        TEMPORARY_FOLDER_VARIABLE: str(temporary),
    }


def wellformed(text: str) -> str:
    return text.encode(SURROGATE_ENCODING, "surrogatepass").decode(SURROGATE_ENCODING, "replace")


def page_content(page: Page, url: str) -> str:
    # The page once it is done: its server has answered, it has loaded and fallen quiet, or
    # its time for these has passed, and its scripts have had a moment more.
    answer = page.goto(url, wait_until="commit", timeout=ANSWER_LIMIT_MS)
    if answer is not None and answer.status >= FIRST_ERROR_STATUS:
        raise UnreadableSource(error_status(wellformed(answer.url), answer.status))
    with suppress(WaitOverdue):
        page.wait_for_load_state("load", timeout=LOAD_LIMIT_MS)
    with suppress(WaitOverdue):
        page.wait_for_load_state("networkidle", timeout=QUIET_LIMIT_MS)
    page.wait_for_timeout(SETTLING_MS)
    return wellformed(page.content())


def wait_for_quiet_network(until: float) -> None:
    # Waits, until the moment given on the monotonic clock at most, for a stretch in which no
    # address of this machine changes.
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as changes:
        changes.bind((0, ADDRESS_CHANGES))
        while (left := until - time.monotonic()) > 0 and select.select(
            [changes], [], [], min(QUIET_NETWORK_SECONDS, left)
        )[0]:
            changes.recv(CHANGE_BYTES)


def breaks_page(request: Request) -> bool:
    # A request whose breaking off leaves the page without what fills it.
    is_own_document = (
        request.resource_type == DOCUMENT_RESOURCE and request.frame.parent_frame is None
    )
    return request.failure == NETWORK_CHANGED and (
        is_own_document or request.resource_type in SCRIPTED_RESOURCES
    )


class PageRendering:
    # The rendering of a page in the renderer: its address, the browser, the proxy the
    # browser connects through, what the proxy refused, and of the attempt under way, the
    # addresses of the page itself that the browser could not open and the requests that broke
    # the page off. The browser tells of these in the thread that drives it, which alone
    # writes them.
    def __init__(self, url: str, program: str, proxy: PublicProxy) -> None:
        self.url = url
        self.program = program
        self.proxy = proxy
        self.refusals: list[Refusal] = []
        self.failed_hops: list[FailedHop] = []
        self.broken: list[str] = []

    def note_failure(self, request: Request) -> None:
        url = wellformed(request.url)
        if request.is_navigation_request() and request.frame.parent_frame is None:
            self.failed_hops.append(FailedHop(url=url, failure=request.failure or ""))
        if breaks_page(request):
            self.broken.append(url)

    def proxy_refusals(self) -> list[str]:
        # Why the proxy refused the last address the page itself was asked at, when the
        # browser could not open it because the proxy did not connect it.
        self.refusals.extend(self.proxy.refusals())
        hops = [urlsplit(hop.url) for hop in self.failed_hops[-1:] if hop.failure == PROXY_FAILURE]
        return [
            f"{hop.geturl()} could not be opened: {refusal.reason}"
            for hop in hops
            for refusal in self.refusals[::-1]
            if (refusal.host, refusal.port)
            == (hop.hostname, hop.port or DEFAULT_PORTS.get(hop.scheme))
        ][:1]

    def ending_reason(self, ending: PlaywrightError | UnreadableSource) -> str:
        # Why a rendering ended, in one line: the module's own words where it raised; the
        # proxy's reason when the page's own address was not connected by the proxy; the last
        # line the browser wrote when it ended, or that it wrote none; infonium's limits and
        # the errors of Chrome's that a page meets, named; and otherwise the first line of the
        # error, without the call log that follows an error of Playwright.
        said = wellformed(error_text(ending))
        first = said.splitlines()[0]
        marked = WEB_ADDRESS.sub("", first)
        refused = self.proxy_refusals() if PROXY_FAILURE in marked else []
        written = [
            line["said"]
            for line in WRITTEN_LINE.finditer(
                said.partition(BROWSER_LOGS)[2].partition(CALL_LOG)[0]
            )
        ]
        named = [words for mark, words in CHROME_ERRORS.items() if mark in marked]
        other = CHROME_ERROR.search(marked)
        if isinstance(ending, UnreadableSource) or refused:
            return (refused or [first])[0]
        if written or BROWSER_LOGS in said:
            return (
                f"the browser {self.program} ended: {written[-1]}"
                if written
                else f"the browser {self.program} ended without saying why"
            )
        if isinstance(ending, WaitOverdue):
            return NO_ANSWER.format(source=self.url, seconds=ANSWER_LIMIT_MS / MS_PER_SECOND)
        if named:
            return named[0].format(source=self.url, program=self.program)
        return OTHER_CHROME_ERROR.format(source=self.url, error=other.group()) if other else first

    def aborted(self, ending: PlaywrightError | UnreadableSource) -> AbortedRendering:
        # A rendering that failed, its reason in one line; where it arose goes to standard
        # error, which is that of the process that started the renderer.
        traceback.print_exception(ending)
        return AbortedRendering(reason=self.ending_reason(ending))

    def attempt(self, context: BrowserContext) -> TakenPage:
        # One attempt at the page, in a page of its own that is closed whatever comes of it.
        self.failed_hops.clear()
        self.broken.clear()
        page = context.new_page()
        page.on("requestfailed", self.note_failure)
        try:
            content = page_content(page, self.url)
            address = wellformed(page.url)
            if urlsplit(address).scheme not in WEB_ADDRESS_SCHEMES:
                raise UnreadableSource(
                    (self.proxy_refusals() or [f"{self.url} led the browser to {address}"])[0]
                )
            return TakenPage(content=content, address=urldefrag(address).url)
        finally:
            page.close()

    def taken_page(self, context: BrowserContext, deadline: float) -> TakenPage:
        # The page once it is done. A page whose own document, scripts or the requests of
        # its scripts were broken off is rendered again, in a page of its own, once the network
        # of this machine holds still, as long as the time left holds another attempt; of the
        # attempts that were broken off, the one that holds the most is kept. An attempt that
        # fails for another reason once another was taken ends the attempts and leaves what was
        # taken.
        taken: list[TakenPage] = []
        failures: list[PlaywrightError | UnreadableSource] = []
        latest_start = deadline - ATTEMPT_LIMIT_SECONDS
        for attempt in range(RENDERING_ATTEMPTS):
            if attempt and time.monotonic() > latest_start:
                break
            if attempt:
                wait_for_quiet_network(latest_start)
            try:
                taken.append(self.attempt(context))
            except (PlaywrightError, UnreadableSource) as failure:
                if self.broken:
                    failures.append(failure)
                    continue
                if taken:
                    break
                raise
            if not self.broken:
                return taken[-1]
        if not taken:
            raise failures[-1]
        fullest = max(taken, key=lambda page: len(page.content))
        return fullest.model_copy(update={"broken_off_by_network": True})

    def report(self, work: Path, deadline: float) -> RenderingReport:
        # How the rendering of the page ends: completed, with the page kept in the work
        # folder, or aborted, with why, where the browser does not start or fails a proof,
        # or the page cannot be taken or kept. Whatever else arises escapes as what it is.
        profile = work / PROFILE_FOLDER
        (profile / PREFERENCES_FILE).parent.mkdir(parents=True)
        (profile / PREFERENCES_FILE).write_text(json.dumps(PROFILE_PREFERENCES))
        (profile / LOCAL_STATE_FILE).write_text(json.dumps(BROWSER_STATE))
        with sync_playwright() as playwright:
            try:
                # Without being told to keep it, Playwright starts the browser without its
                # sandbox.
                context = playwright.chromium.launch_persistent_context(
                    profile,
                    executable_path=self.program,
                    chromium_sandbox=True,
                    args=[
                        *BROWSER_FLAGS,
                        PROXY_SERVER_FLAG.format(
                            scheme=PROXY_SCHEME, host=LOOPBACK, port=self.proxy.port()
                        ),
                    ],
                    accept_downloads=False,
                    service_workers="block",
                )
            except PlaywrightError as ending:
                return self.aborted(ending)
            failures = confinement.proof_failures(context, self.proxy, self.program)
            if failures:
                return AbortedRendering(reason=failures[0])
            try:
                taken = self.taken_page(context, deadline)
            except (PlaywrightError, UnreadableSource) as ending:
                return self.aborted(ending)
        # The page is kept as UTF-8 with its mark in front, which a browser and the extractor
        # both read before any charset the page declares in its head or a server in a header.
        kept = BOM_UTF8 + taken.content.encode()
        if len(kept) > RENDERED_LIMIT_BYTES:
            return AbortedRendering(reason=OVERSIZE_SOURCE.format(source=self.url))
        (work / RENDERED_FILE).write_bytes(kept)
        return CompletedRendering(
            address=taken.address, broken_off_by_network=taken.broken_off_by_network
        )


def remove_rendering(work: Path, reported: Event) -> None:
    # What a renderer whose caller is gone does once it has ended all it started, which ends
    # the rendering under way: it lets the rendering write its report, for a few seconds at
    # most, so that nothing is written into the folders of the rendering as they are removed,
    # and removes them.
    reported.wait(REPORTING_SECONDS)
    shutil.rmtree(work)
    shutil.rmtree(os.environ[TEMPORARY_FOLDER_VARIABLE])


def main() -> None:
    # What this module does when it is run as a program: it renders the page it is given
    # into the folder it is given, and writes the report of how the rendering ended. An
    # exception does not cross from one process to another: a fault of the renderer's own
    # ends it without a report. Whatever the renderer started is ended before it reports or
    # ends.
    work, program, deadline = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    url = (work / ADDRESS_FILE).read_text()
    lineage.hold_data_to(PROCESS_MEMORY_BYTES)
    reported = Event()
    lineage.hold_descendants(partial(remove_rendering, work, reported))
    proxy = PublicProxy()
    Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        report = PageRendering(url, program, proxy).report(work, float(deadline))
    finally:
        lineage.end_own_descendants()
    (work / WRITTEN_REPORT_FILE).write_bytes(RENDERING_REPORT.dump_json(report))
    (work / WRITTEN_REPORT_FILE).replace(work / RENDERING_REPORT_FILE)
    reported.set()
    lineage.wait_to_be_ended()


def folder_sizes(folder: Path) -> Iterator[int | None]:
    # What the files of a folder hold, of those still there as they are counted, from walks
    # made one after another a few entries at a time: the step that ends a walk yields its sum,
    # and every other step nothing.
    while True:
        held, entries, pending = 0, 0, [str(folder)]
        while pending:
            with suppress(FileNotFoundError), os.scandir(pending.pop()) as listing:
                for entry in listing:
                    with suppress(FileNotFoundError):
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            held += entry.stat(follow_symlinks=False).st_size
                    entries += 1
                    if entries % FOLDER_ENTRIES_PER_LOOK == 0:
                        yield None
        yield held


def watch_rendering(url: str, renderer: lineage.HeldProgram, work: Path, temporary: Path) -> None:
    # Waits until the renderer has reported or ended; a rendering that takes longer than its
    # limit, or holds more than its bounds, is ended there. The browser keeps the memory its
    # processes share in files under its temporary folder, removed as soon as they are made,
    # and these count in both bounds; the work folder counts in the disk bound as its last
    # whole walk found it.
    began = next_look = time.monotonic()
    sizes, folder = folder_sizes(work), 0
    while not (work / RENDERING_REPORT_FILE).exists() and not renderer.has_ended():
        if time.monotonic() - began > RENDERING_LIMIT_SECONDS:
            raise UnreadableSource(OVERDUE_SOURCE.format(source=url))
        processes = renderer.look()
        shared = lineage.held_files(lineage.held_descriptors(processes), temporary)
        if lineage.held_memory(processes) + shared > RENDERING_MEMORY_BYTES:
            raise UnreadableSource(TOO_MUCH_MEMORY.format(source=url))
        if (walked := next(sizes)) is not None:
            folder = walked
        if folder + shared > RENDERING_DISK_BYTES:
            raise UnreadableSource(OVERSIZE_SOURCE.format(source=url))
        next_look = max(next_look + RENDERER_WATCH_SECONDS, time.monotonic())
        time.sleep(max(0.0, next_look - time.monotonic()))


def reported_rendering(url: str, work: Path, program: str) -> RenderingReport:
    # The rendering of a page as the renderer reports it. Whatever the renderer started is
    # ended with it, and gone, before this returns or raises.
    deadline = time.monotonic() + RENDERING_LIMIT_SECONDS - REPORTING_SECONDS
    command = [*RENDERER, str(work), program, repr(deadline)]
    (work / ADDRESS_FILE).write_text(url)
    with TemporaryDirectory() as temporary:
        environment = renderer_environment(work, Path(temporary))
        with lineage.HeldProgram(command, environment) as renderer:
            watch_rendering(url, renderer, work, Path(temporary))
    # A renderer that ended before it reported has met a fault of its own.
    if not (work / RENDERING_REPORT_FILE).exists():
        raise subprocess.CalledProcessError(renderer.status(), command)
    return RENDERING_REPORT.validate_json((work / RENDERING_REPORT_FILE).read_bytes())


def rendered_page(url: str, directory: Path, browser: Path) -> RenderedPage:
    if urlsplit(url).scheme not in WEB_ADDRESS_SCHEMES:
        raise UnreadableSource(f"infonium renders only web pages, and {url} is not one")
    program = shutil.which(browser)
    if program is None:
        raise UnreadableSource(f"the browser {browser} was not found")
    file = directory / RENDERED_FILE
    with TemporaryDirectory(dir=directory) as work:
        match reported_rendering(url, Path(work), program):
            case CompletedRendering(address=address, broken_off_by_network=broken):
                (Path(work) / RENDERED_FILE).replace(file)
            case AbortedRendering(reason=reason):
                raise UnreadableSource(reason)
    return RenderedPage(file=file, address=address, broken_off_by_network=broken)


if __name__ == "__main__":
    main()
