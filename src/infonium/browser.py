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
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Annotated, Literal
from urllib.parse import urldefrag, urlsplit

import psutil
from playwright.sync_api import BrowserContext, Page, Request, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as WaitOverdue
from pydantic import BaseModel, Field, TypeAdapter

from infonium import lineage
from infonium.document import (
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    Tagged,
    UnreadableSource,
    error_status,
    error_text,
)
from infonium.egress import EVERY_PORT, LOOPBACK, PublicProxy, Refusal

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
# with no other, and on disk in its work folder; a page that holds more is ended. In a survey
# of 30 sites, no page held more than 1.1 GiB, or more than 124 MiB on disk.
RENDERING_MEMORY_BYTES = 3 * 1024**3
RENDERING_DISK_BYTES = 1024**3
TOO_MUCH_MEMORY = "{source} needs more memory than infonium gives a page"

# The browser is driven by a process of its own, this module run as a program, which starts
# the proxy the browser connects through and Playwright, which starts the browser. It is
# given the address of the page, a folder for its work, the browser to start and the
# moment, on the system's monotonic clock, by which it is to be done; it leaves the page in
# the folder and a report of the rendering beside it, and then waits to be ended. It is to
# be started with a pipe for its standard input that the process which starts it holds open
# and writes nothing to: when the pipe closes, the renderer ends all it started, and itself.
RUN_AS_MODULE = "-m"
RENDERER = (sys.executable, RUN_AS_MODULE, __spec__.name)
RENDERING_REPORT_FILE = "rendering.json"
WRITTEN_REPORT_FILE = "rendering.json.part"
ABANDONED_STATUS = 1
# How long the process that started the renderer lets pass between two looks at whether it
# is done, and between two looks at what the rendering holds.
RENDERER_WATCH_SECONDS = 0.05
HOLDING_WATCH_SECONDS = 0.2
# A look at whether a process has ended that does not wait, and leaves it unreaped.
ENDED_UNREAPED = os.WEXITED | os.WNOHANG | os.WNOWAIT
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
# The profile's preferences, written before the browser starts: WebRTC sends nothing over
# UDP, which the proxy cannot carry.
PREFERENCES_FILE = Path("Default") / "Preferences"
WEBRTC_POLICY = "disable_non_proxied_udp"
PROFILE_PREFERENCES = {"webrtc": {"ip_handling_policy": WEBRTC_POLICY}}
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
# A process of the browser that runs pages, and how Linux shows a process held to the calls
# its seccomp filter lets through: the browser's sandbox, seen in what runs rather than in
# what the browser was told.
PAGE_PROCESS = "--type=renderer"
SECCOMP_FILTER = "Seccomp:\t2"
# Before the page is asked for, the browser is shown a STUN server on a UDP port the renderer
# holds, and must gather no candidate for it and send it nothing.
WEBRTC_PROOF_LIMIT_MS = 2_000
WEBRTC_PROOF = """async ([server, limit]) => {
  const connection = new RTCPeerConnection({iceServers: [{urls: server}]});
  let candidates = 0;
  connection.onicecandidate = (event) => { if (event.candidate) candidates += 1; };
  connection.createDataChannel("proof");
  await connection.setLocalDescription(await connection.createOffer());
  await new Promise((done) => {
    connection.onicegatheringstatechange = () => {
      if (connection.iceGatheringState === "complete") done();
    };
    if (connection.iceGatheringState === "complete") done();
    setTimeout(done, limit);
  });
  connection.close();
  return candidates;
}"""


class RenderedPage(BaseModel):
    # A page as a browser built it: its HTML once its scripts have run, kept as a file, and
    # the address the browser ended at.
    file: Path
    address: str


class TakenPage(BaseModel):
    # The HTML of a page as its browser holds it, and the address the browser ended at.
    content: str
    address: str


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


class AbortedRendering(Tagged):
    # Why a page was not rendered, in one line a person reads.
    kind: Literal[RenderingReportKind.ABORTED] = RenderingReportKind.ABORTED
    reason: str


# What the renderer tells the process that started it of how the rendering ended.
RenderingReport = Annotated[CompletedRendering | AbortedRendering, Field(discriminator="kind")]
RENDERING_REPORT = TypeAdapter(RenderingReport)


def browser_environment(work: Path) -> dict[str, str | float | bool]:
    home = work / HOME_FOLDER
    return {**os.environ, **{name: str(home / part) for name, part in HOME_VARIABLES.items()}}


def page_content(page: Page, url: str) -> str:
    # The page once it is done: its server has answered, it has loaded and fallen quiet, or
    # its time for these has passed, and its scripts have had a moment more.
    answer = page.goto(url, wait_until="commit", timeout=ANSWER_LIMIT_MS)
    if answer is not None and answer.status >= FIRST_ERROR_STATUS:
        raise UnreadableSource(error_status(answer.url, answer.status))
    with suppress(WaitOverdue):
        page.wait_for_load_state("load", timeout=LOAD_LIMIT_MS)
    with suppress(WaitOverdue):
        page.wait_for_load_state("networkidle", timeout=QUIET_LIMIT_MS)
    page.wait_for_timeout(SETTLING_MS)
    return page.content()


def page_process_states() -> list[list[str]]:
    # The states, as Linux shows them, of the browser's processes that run pages, of those
    # still running as they are read.
    states = []
    for process in lineage.descendants(os.getpid()):
        with suppress(psutil.NoSuchProcess, FileNotFoundError, ProcessLookupError):
            if PAGE_PROCESS in process.cmdline():
                states.append(Path(f"/proc/{process.pid}/status").read_text().splitlines())
    return states


def datagram_waits(listener: socket.socket) -> bool:
    readable, _, _ = select.select([listener], [], [], 0)
    return bool(readable)


def breaks_page(request: Request) -> bool:
    # A request whose breaking off leaves the page without what fills it.
    is_own_document = (
        request.resource_type == DOCUMENT_RESOURCE and request.frame.parent_frame is None
    )
    return request.failure == NETWORK_CHANGED and (
        is_own_document or request.resource_type in SCRIPTED_RESOURCES
    )


class PageRendering:
    # The rendering of a page in the renderer: the proxy the browser connects through, what
    # the proxy refused, and of the attempt under way, the addresses of the page itself that
    # the browser could not open and the requests that broke the page off. The browser tells
    # of these in the thread that drives it, which alone writes them.
    def __init__(self, proxy: PublicProxy, program: str) -> None:
        self.proxy = proxy
        self.program = program
        self.refusals: list[Refusal] = []
        self.failed_hops: list[FailedHop] = []
        self.broken: list[str] = []

    def note_failure(self, request: Request) -> None:
        if request.is_navigation_request() and request.frame.parent_frame is None:
            self.failed_hops.append(FailedHop(url=request.url, failure=request.failure or ""))
        if breaks_page(request):
            self.broken.append(request.url)

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
        # Why a rendering ended, in one line: the proxy's reason when the page's own address
        # was not connected by the proxy; the last line the browser wrote when it ended, or
        # that it wrote none; and otherwise the first line of the error, without the call log
        # that follows an error of Playwright.
        said = error_text(ending)
        refused = self.proxy_refusals() if PROXY_FAILURE in said else []
        logs = said.partition(BROWSER_LOGS)[2].partition(CALL_LOG)[0]
        written = [line["said"] for line in WRITTEN_LINE.finditer(logs)]
        if refused:
            return refused[0]
        if written:
            return f"the browser {self.program} ended: {written[-1]}"
        if BROWSER_LOGS in said:
            return f"the browser {self.program} ended without saying why"
        return said.splitlines()[0]

    def prove_browser(self, context: BrowserContext) -> None:
        # The browser runs its pages in its sandbox, and its WebRTC sends nothing over UDP,
        # or no page is asked for.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind((LOOPBACK, EVERY_PORT))
            server = f"stun:{LOOPBACK}:{listener.getsockname()[1]}"
            page = context.new_page()
            try:
                candidates = page.evaluate(WEBRTC_PROOF, [server, WEBRTC_PROOF_LIMIT_MS])
                states = page_process_states()
            finally:
                page.close()
            sent = datagram_waits(listener)
        if not states or not all(SECCOMP_FILTER in state for state in states):
            raise UnreadableSource(f"the browser {self.program} runs pages without its sandbox")
        if candidates or sent:
            raise UnreadableSource(
                f"the browser {self.program} sends WebRTC over UDP, which infonium cannot hold "
                "to public addresses"
            )

    def attempt(self, context: BrowserContext, url: str) -> TakenPage:
        # One attempt at the page, in a page of its own that is closed whatever comes of it.
        self.failed_hops.clear()
        self.broken.clear()
        page = context.new_page()
        page.on("requestfailed", self.note_failure)
        try:
            content = page_content(page, url)
            if urlsplit(page.url).scheme not in WEB_ADDRESS_SCHEMES:
                raise UnreadableSource(
                    (self.proxy_refusals() or [f"{url} led the browser to {page.url}"])[0]
                )
            return TakenPage(content=content, address=urldefrag(page.url).url)
        finally:
            page.close()

    def taken_page(self, context: BrowserContext, url: str, deadline: float) -> TakenPage:
        # The page once it is done. A page whose own document, scripts or the requests of
        # its scripts were broken off is rendered again, in a page of its own, as long as the
        # time left holds another attempt; of the attempts that were broken off, the one that
        # holds the most is kept. An attempt that fails for another reason once another was
        # taken ends the attempts and leaves what was taken.
        taken: list[TakenPage] = []
        failures: list[PlaywrightError | UnreadableSource] = []
        for attempt in range(RENDERING_ATTEMPTS):
            if attempt and deadline - time.monotonic() < ATTEMPT_LIMIT_SECONDS:
                break
            try:
                taken.append(self.attempt(context, url))
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
        return max(taken, key=lambda page: len(page.content))

    def render(self, url: str, work: Path, deadline: float) -> str:
        # Renders the page into the work folder and gives the address the browser ended at.
        profile = work / PROFILE_FOLDER
        (profile / PREFERENCES_FILE).parent.mkdir(parents=True)
        (profile / PREFERENCES_FILE).write_text(json.dumps(PROFILE_PREFERENCES))
        (profile / LOCAL_STATE_FILE).write_text(json.dumps(BROWSER_STATE))
        with sync_playwright() as playwright:
            # Without being told to keep it, Playwright starts the browser without its sandbox.
            context = playwright.chromium.launch_persistent_context(
                profile,
                executable_path=self.program,
                chromium_sandbox=True,
                args=[
                    *BROWSER_FLAGS,
                    f"--proxy-server=socks5://{LOOPBACK}:{self.proxy.port()}",
                ],
                env=browser_environment(work),
                accept_downloads=False,
                service_workers="block",
            )
            self.prove_browser(context)
            taken = self.taken_page(context, url, deadline)
        # The page is kept as UTF-8 with its mark in front, which a browser and the extractor
        # both read before any charset the page declares in its head or a server in a header.
        kept = BOM_UTF8 + taken.content.encode()
        if len(kept) > RENDERED_LIMIT_BYTES:
            raise UnreadableSource(OVERSIZE_SOURCE.format(source=url))
        (work / RENDERED_FILE).write_bytes(kept)
        return taken.address


def end_when_abandoned(work: Path, reported: Event) -> None:
    # The process that started the renderer holds the other end of its standard input open
    # and writes nothing to it. When that end closes, that process is gone and removes
    # nothing, so the renderer ends all it started, which ends the rendering under way; lets
    # it write its report, for a few seconds at most, so that nothing is written into the
    # folders of the rendering as they are removed; removes them; and ends itself, whatever
    # of this fails.
    os.read(sys.stdin.fileno(), 1)
    try:
        lineage.end_own_descendants()
        reported.wait(REPORTING_SECONDS)
        shutil.rmtree(work)
        shutil.rmtree(os.environ[TEMPORARY_FOLDER_VARIABLE])
    finally:
        os._exit(ABANDONED_STATUS)


def main() -> None:
    # What this module does when it is run as a program: it renders the page it is given
    # into the folder it is given, and writes the report. An exception does not cross from
    # one process to another, so a rendering that failed is reported in one line, and where
    # it arose is written to standard error, which is the starting process's own. Any other
    # exception is a fault of the renderer's own, and ends it without a report. Whatever the
    # renderer started is ended before it reports or ends, and it is the reaper of their
    # orphans, so that all of them stay its descendants until then.
    url, work, program, deadline = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    lineage.become_reaper()
    lineage.hold_data_to(PROCESS_MEMORY_BYTES)
    reported = Event()
    Thread(target=end_when_abandoned, args=(work, reported), daemon=True).start()
    proxy = PublicProxy()
    proxy.serve_in_background()
    rendering = PageRendering(proxy, program)
    try:
        report: RenderingReport = CompletedRendering(
            address=rendering.render(url, work, float(deadline))
        )
    except (PlaywrightError, UnreadableSource) as ending:
        traceback.print_exc()
        report = AbortedRendering(reason=rendering.ending_reason(ending))
    finally:
        lineage.end_own_descendants()
    (work / WRITTEN_REPORT_FILE).write_bytes(RENDERING_REPORT.dump_json(report))
    (work / WRITTEN_REPORT_FILE).replace(work / RENDERING_REPORT_FILE)
    reported.set()
    Event().wait()


def folder_bytes(folder: Path) -> int:
    # What the files of a folder hold, of those that are still there as they are counted.
    held = 0
    for file in folder.rglob("*"):
        with suppress(FileNotFoundError):
            held += file.lstat().st_size
    return held


def watch_rendering(url: str, renderer: subprocess.Popen[bytes], work: Path) -> None:
    # Waits until the renderer has reported or ended; a rendering that takes longer than its
    # limit, or holds more than its bounds, is ended there.
    began = time.monotonic()
    next_look = began
    while (
        not (work / RENDERING_REPORT_FILE).exists()
        and os.waitid(os.P_PID, renderer.pid, ENDED_UNREAPED) is None
    ):
        now = time.monotonic()
        if now - began > RENDERING_LIMIT_SECONDS:
            raise UnreadableSource(OVERDUE_SOURCE.format(source=url))
        if now >= next_look:
            if lineage.held_memory(renderer.pid) > RENDERING_MEMORY_BYTES:
                raise UnreadableSource(TOO_MUCH_MEMORY.format(source=url))
            if folder_bytes(work) > RENDERING_DISK_BYTES:
                raise UnreadableSource(OVERSIZE_SOURCE.format(source=url))
            next_look = now + HOLDING_WATCH_SECONDS
        time.sleep(RENDERER_WATCH_SECONDS)


def reported_rendering(url: str, work: Path, program: str) -> RenderingReport:
    # The rendering of a page as the renderer reports it. Whatever the renderer started is
    # ended with it, and gone, before this returns or raises.
    deadline = time.monotonic() + RENDERING_LIMIT_SECONDS - REPORTING_SECONDS
    command = [*RENDERER, url, str(work), program, repr(deadline)]
    with TemporaryDirectory() as temporary:
        environment = {**os.environ, TEMPORARY_FOLDER_VARIABLE: temporary}
        # The renderer leads a session of its own, so that a signal to this process's group
        # does not end it before it has ended all it started and removed the rendering's
        # folders, which it does when its standard input closes.
        with subprocess.Popen(
            command, stdin=subprocess.PIPE, env=environment, start_new_session=True
        ) as renderer:
            try:
                watch_rendering(url, renderer, work)
            finally:
                lineage.end_process_tree(renderer)
    # A renderer that ended before it reported has met a fault of its own.
    if not (work / RENDERING_REPORT_FILE).exists():
        raise subprocess.CalledProcessError(renderer.returncode, command)
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
            case CompletedRendering(address=address):
                (Path(work) / RENDERED_FILE).replace(file)
            case AbortedRendering(reason=reason):
                raise UnreadableSource(reason)
    return RenderedPage(file=file, address=address)


if __name__ == "__main__":
    main()
