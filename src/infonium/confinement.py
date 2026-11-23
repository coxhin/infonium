import os
import select
import socket
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread

import psutil
from playwright.sync_api import BrowserContext
from pydantic import BaseModel

from infonium import lineage
from infonium.egress import (
    EVERY_PORT,
    LOOPBACK,
    TABLE_INODE,
    PublicProxy,
    Refusal,
    connection_sockets,
)

# Whether a browser that has started may be given a page: its sandbox, seen in what runs, and
# proofs, made in a page of its own before the page is asked for, of where its connections go.

# Where Linux lists the UDP sockets of this machine's network, IPv4 and IPv6: a socket's own
# address and port in hexadecimal, and its number among the inodes. A socket bound on every
# interface has the address of all zeros.
UDP_TABLES = ("/proc/net/udp", "/proc/net/udp6")
TABLE_ADDRESS = 1
HEXADECIMAL = 16
# The one UDP port the browser's WebRTC is given, by a preference of its profile.
WEBRTC_PORT = 1
# The browser's sandbox, seen in what runs rather than in what the browser was told: a process
# of the browser that runs pages runs under more seccomp filters than the browser's own
# process, its nearest ancestor that carries no type of process, and stands in none of its
# namespaces. Linux shows the filters of a process in its status, from Linux 5.9 on.
PROCESS_TYPE = "--type="
PAGE_PROCESS = "--type=renderer"
PROCESS_STATUS = "/proc/{pid}/status"
PROCESS_NAMESPACE = "/proc/{pid}/ns/{kind}"
SECCOMP_FILTERS = "Seccomp_filters:"
SANDBOX_NAMESPACES = ("user", "pid", "net")
# Before the page is asked for, a page of the browser's asks for an address of this machine
# where the renderer listens: the proxy must refuse it, and no process of the rendering may
# reach the listener. The proof rests on two switches: the proxy, which loopback may not
# bypass, and the resolver rule, which leaves the browser no address to reach around the
# proxy but 127.0.0.1. And it is shown a STUN server on a UDP port the renderer holds, for
# which it must gather no candidate and to which no process of the rendering may send. What
# reaches either listener from another process of this machine, to which both ports show,
# counts for nothing. A fetch that a change of this machine's network broke off before it
# reached the proxy proves nothing either way, and is made again, a few times at most: under
# 40 changes a second one fetch in seven was broken off so. What a person is told of a browser
# that fails a proof.
NOT_SANDBOXED = "the browser {program} runs pages without its sandbox"
BYPASSES_PROXY = "the browser {program} does not send every connection through infonium's proxy"
SENDS_WEBRTC = (
    "the browser {program} sends WebRTC over UDP, which infonium cannot hold to public addresses"
)
OPENS_UDP_PORTS = "the browser {program} keeps UDP ports {ports} open on every interface"
PROOF_LIMIT_MS = 2_000
LOOPBACK_FETCHES = 5
# How long the proof's listener waits between two looks at whether the proof is over, and the
# most a datagram holds. The proof reads at most 1,024 datagrams from its STUN listener, some
# four times what its buffer holds (221 of one byte here), so that another process of this
# machine that floods it cannot hold the proof; a datagram of the browser's that such a flood
# crowds out goes unseen, and the proof then rests on the candidates the browser gathered.
LISTENER_WATCH_SECONDS = 0.05
DATAGRAM_BYTES = 65536
DATAGRAMS_READ = 1024
LOOPBACK_PROOF = """async ([address, limit]) => {
  await fetch(address, {mode: "no-cors", cache: "no-store", signal: AbortSignal.timeout(limit)})
    .catch(() => null);
}"""
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
  return candidates;
}"""


class BrowserProcess(BaseModel):
    # A process of the rendering as Linux shows it: its parent, whether it runs pages or
    # carries any type of the browser's processes, the seccomp filters it runs under, none
    # where Linux does not show them, and the namespaces it stands in.
    parent: int
    runs_pages: bool
    typed: bool
    filters: int | None
    namespaces: list[str]


def udp_bindings(sockets: set[int]) -> list[tuple[int, int]]:
    # The address, as Linux writes it, and the port of each of `sockets` that is a UDP socket.
    bindings = []
    for table in UDP_TABLES:
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            address, port = fields[TABLE_ADDRESS].split(":")
            if int(fields[TABLE_INODE]) in sockets:
                bindings.append((int(address, HEXADECIMAL), int(port, HEXADECIMAL)))
    return bindings


def note_rendering_connections(listener: socket.socket, heard: Event, over: Event) -> None:
    # Takes each connection made to the listener until the proof is over, and notes whether
    # one came from a process of the rendering, by the socket at its other end.
    listener.settimeout(LISTENER_WATCH_SECONDS)
    while not over.is_set():
        with suppress(TimeoutError):
            connection, client = listener.accept()
            with connection:
                started = lineage.descendants(os.getpid())
                held = lineage.held_sockets(lineage.held_descriptors(started))
                if connection_sockets(client, listener.getsockname()) & held:
                    heard.set()


def browser_processes(started: list[psutil.Process]) -> dict[int, BrowserProcess]:
    # The processes this renderer started, by their numbers, of those still running as they
    # are read.
    found = {}
    for process in started:
        with suppress(*lineage.LOOK_REFUSED):
            arguments = process.cmdline()
            status = Path(PROCESS_STATUS.format(pid=process.pid)).read_text().splitlines()
            filters = [int(line.split()[1]) for line in status if line.startswith(SECCOMP_FILTERS)]
            found[process.pid] = BrowserProcess(
                parent=process.ppid(),
                runs_pages=PAGE_PROCESS in arguments,
                typed=any(argument.startswith(PROCESS_TYPE) for argument in arguments),
                filters=filters[0] if filters else None,
                namespaces=[
                    os.readlink(PROCESS_NAMESPACE.format(pid=process.pid, kind=kind))
                    for kind in SANDBOX_NAMESPACES
                ],
            )
    return found


def browser_of(
    process: BrowserProcess, processes: dict[int, BrowserProcess]
) -> BrowserProcess | None:
    # The browser's own process for a process it started, where it is among `processes`.
    ancestor = processes.get(process.parent)
    while ancestor is not None and ancestor.typed:
        ancestor = processes.get(ancestor.parent)
    return ancestor


def runs_pages_sandboxed(processes: dict[int, BrowserProcess]) -> bool:
    # There is a process that runs pages, and every such process runs under more seccomp
    # filters than the browser's own process and stands in none of its namespaces.
    pages = [process for process in processes.values() if process.runs_pages]
    browsers = [browser_of(page, processes) for page in pages]
    return bool(pages) and all(
        browser is not None
        and page.filters is not None
        and browser.filters is not None
        and page.filters > browser.filters
        and set(page.namespaces).isdisjoint(browser.namespaces)
        for page, browser in zip(pages, browsers, strict=True)
    )


def proof_failures(context: BrowserContext, proxy: PublicProxy, program: str) -> list[str]:
    # Why the browser cannot be given the page, if it cannot: it runs its pages outside
    # its sandbox, it opens a connection without the proxy, its WebRTC sends over UDP, or
    # it holds a UDP port open on every interface but the one WebRTC is given. A proof that
    # cannot be made is a fault of the renderer's own, and escapes as what it is.
    refusals: list[Refusal] = []
    heard, over = Event(), Event()
    with (
        socket.create_server((LOOPBACK, EVERY_PORT)) as loopback_listener,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stun_listener,
    ):
        stun_listener.bind((LOOPBACK, EVERY_PORT))
        loopback_port = loopback_listener.getsockname()[1]
        asked = f"http://{LOOPBACK}:{loopback_port}/"
        server = f"stun:{LOOPBACK}:{stun_listener.getsockname()[1]}"
        watch = Thread(
            target=note_rendering_connections, args=(loopback_listener, heard, over), daemon=True
        )
        watch.start()
        page = context.new_page()
        refused = False
        try:
            for _ in range(LOOPBACK_FETCHES):
                page.evaluate(LOOPBACK_PROOF, [asked, PROOF_LIMIT_MS])
                refusals.extend(proxy.refusals())
                refused = any(
                    (refusal.host, refusal.port) == (LOOPBACK, loopback_port)
                    for refusal in refusals
                )
                if refused or heard.is_set():
                    break
            # The proof's WebRTC connection stays open until the page is closed, so that
            # the UDP ports it holds are seen.
            candidates = page.evaluate(WEBRTC_PROOF, [server, PROOF_LIMIT_MS])
            started = lineage.descendants(os.getpid())
            processes = browser_processes(started)
            bindings = udp_bindings(lineage.held_sockets(lineage.held_descriptors(started)))
        finally:
            over.set()
            page.close()
        watch.join()
        senders = set()
        for _ in range(DATAGRAMS_READ):
            if not select.select([stun_listener], [], [], 0)[0]:
                break
            senders.add(stun_listener.recvfrom(DATAGRAM_BYTES)[1][1])
    ports = {port for address, port in bindings if address == 0}
    failures = []
    if not runs_pages_sandboxed(processes):
        failures.append(NOT_SANDBOXED.format(program=program))
    if heard.is_set() or not refused:
        failures.append(BYPASSES_PROXY.format(program=program))
    if candidates or senders & {port for _, port in bindings}:
        failures.append(SENDS_WEBRTC.format(program=program))
    if ports - {WEBRTC_PORT}:
        failures.append(OPENS_UDP_PORTS.format(program=program, ports=sorted(ports)))
    return failures
