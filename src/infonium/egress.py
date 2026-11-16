import re
import socket
import sys
from collections.abc import Sequence
from contextlib import suppress
from enum import IntEnum
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from queue import SimpleQueue
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import BoundedSemaphore, Thread
from typing import Any

import psutil
from pydantic import BaseModel

from infonium.document import UnreadableSource, error_text

type Address = IPv4Address | IPv6Address
type Network = IPv4Network | IPv6Network

# Networks of the special-purpose registries that Python counts as global, and that reach no
# host of the internet: the 6to4 relays' anycast, IPv4 translated by SIIT, the old site-local
# addresses, the segment routing SIDs and the dummy prefix for discarding.
NOT_PUBLIC_NETWORKS: tuple[Network, ...] = (
    IPv4Network("192.88.99.0/24"),
    IPv6Network("::ffff:0:0:0/96"),
    IPv6Network("fec0::/10"),
    IPv6Network("5f00::/16"),
    IPv6Network("100:0:0:1::/64"),
)
# IPv6 prefixes whose last 32 bits are an IPv4 address that a host or a gateway connects to:
# the IPv4-compatible form and the prefix of translation by NAT64.
IPV4_CARRYING_PREFIXES = (IPv6Network("::/96"), IPv6Network("64:ff9b::/96"))
IPV4_PART = 0xFFFF_FFFF
# An interface names its IPv6 addresses with the zone they belong to after this mark.
ZONE_MARK = "%"

LOOPBACK = "127.0.0.1"
EVERY_PORT = 0
# The proxy speaks SOCKS 5, which carries every connection a browser opens, a WebSocket and a
# secure connection alike, as a plain connection to a named host and port, and through which
# Chrome sends the name of a host unresolved. No library proxy embeds in the renderer's own
# process: proxy.py, whose hook judges the resolved address, serves its connections in a
# process of its own, so that what it refused never reaches the renderer, and connects to
# the name itself when the hook gives no address.
SOCKS_VERSION = 5
NO_AUTHENTICATION = 0
CONNECT = 1
RESERVED = 0
GREETING_BYTES = 2
REQUEST_HEADER_BYTES = 4
NAME_LENGTH_BYTES = 1
PORT_BYTES = 2
IPV4_BYTES = 4
IPV6_BYTES = 16
# A host as Chrome names it: a name of letters, digits, dots, hyphens and the underscores
# some names carry, or an IPv6 address, which it sends as a name too, in brackets or bare.
HOST_NAME = re.compile(r"\[?(?P<host>[A-Za-z0-9._:-]+)\]?")
# How long the browser is given to say where it wants to go, and a host to take a
# connection; how many connections the proxy carries at once, which is more than Chrome
# opens through one proxy (32); and how much of a connection is passed on at a time.
HANDSHAKE_LIMIT_SECONDS = 10.0
CONNECTION_LIMIT_SECONDS = 15.0
CONNECTIONS_AT_ONCE = 64
RELAYED_BYTES = 64 * 1024


class AddressKind(IntEnum):
    IPV4 = 1
    NAME = 3
    IPV6 = 4


class SocksReply(IntEnum):
    SUCCEEDED = 0
    GENERAL_FAILURE = 1
    NOT_ALLOWED = 2
    COMMAND_NOT_SUPPORTED = 7
    ADDRESS_KIND_NOT_SUPPORTED = 8


class Refusal(BaseModel):
    # A connection the browser asked the proxy for and did not get, and why.
    host: str
    port: int
    reason: str


class MalformedRequest(Exception):
    # A request that is not SOCKS 5 as a browser speaks it, and the reply that says so.
    def __init__(self, reply: SocksReply) -> None:
        super().__init__(reply.name)
        self.reply = reply


def carried_addresses(address: Address) -> list[IPv4Address]:
    # The IPv4 addresses an IPv6 address carries inside it, which a host or a gateway on the
    # way connects to in its place.
    if isinstance(address, IPv4Address):
        return []
    return [
        *[inner for inner in (address.ipv4_mapped, address.sixtofour) if inner is not None],
        *(address.teredo or ()),
        *[
            IPv4Address(int(address) & IPV4_PART)
            for prefix in IPV4_CARRYING_PREFIXES
            if address in prefix
        ],
    ]


def is_public(address: Address, own_networks: Sequence[Network]) -> bool:
    # An address is public when it, and every IPv4 address it carries, is reachable across
    # the internet, names one host, and lies in no network that this machine stands in.
    refused = (*NOT_PUBLIC_NETWORKS, *own_networks)
    return all(
        each.is_global
        and not each.is_multicast
        and not any(each.version == network.version and each in network for network in refused)
        for each in (address, *carried_addresses(address))
    )


def network_of(address: str, netmask: str | None) -> Network:
    # The network an interface's address stands in, by its address and its mask; an address
    # given without a mask stands for itself alone.
    host = ip_address(address.split(ZONE_MARK)[0])
    length = host.max_prefixlen if netmask is None else bin(int(ip_address(netmask))).count("1")
    return ip_network((host, length), strict=False)


def own_networks() -> list[Network]:
    # The networks this machine stands in, as its interfaces hold them now: each address of
    # its own with its prefix, and the far end of an interface that leads to one host.
    return [
        network
        for addresses in psutil.net_if_addrs().values()
        for entry in addresses
        if entry.family in (socket.AF_INET, socket.AF_INET6)
        for network in (
            network_of(entry.address, entry.netmask),
            *([network_of(entry.ptp, None)] if entry.ptp else []),
        )
    ]


def public_connection(host: str, port: int) -> socket.socket:
    # A connection to a host, made to the address that was judged: the name is looked up once,
    # and the networks of this machine are read at each connection, since they change.
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG)
    networks = own_networks()
    for *_, place in found:
        if not is_public(ip_address(place[0]), networks):
            raise UnreadableSource(f"{place[0]} is not a public address")
    family, kind, protocol, _, place = found[0]
    connection = socket.socket(family, kind, protocol)
    connection.settimeout(CONNECTION_LIMIT_SECONDS)
    connection.connect(place)
    connection.settimeout(None)
    return connection


def socks_reply(reply: SocksReply) -> bytes:
    # A reply of SOCKS 5, which carries an address no browser reads: a zero IPv4 address
    # and port.
    return bytes(
        [SOCKS_VERSION, reply, RESERVED, AddressKind.IPV4, *bytes(IPV4_BYTES), *bytes(PORT_BYTES)]
    )


def received(connection: socket.socket, size: int) -> bytes:
    content = connection.recv(size, socket.MSG_WAITALL)
    if len(content) < size:
        raise ConnectionError(f"the browser closed the connection to the proxy after {content!r}")
    return content


def asked_destination(client: socket.socket) -> tuple[str, int]:
    # The host and port a browser asks the proxy to connect it to.
    _, method_count = received(client, GREETING_BYTES)
    received(client, method_count)
    client.sendall(bytes([SOCKS_VERSION, NO_AUTHENTICATION]))
    version, command, _, kind = received(client, REQUEST_HEADER_BYTES)
    if version != SOCKS_VERSION:
        raise MalformedRequest(SocksReply.GENERAL_FAILURE)
    if command != CONNECT:
        raise MalformedRequest(SocksReply.COMMAND_NOT_SUPPORTED)
    match kind:
        case AddressKind.IPV4:
            host = str(IPv4Address(received(client, IPV4_BYTES)))
        case AddressKind.IPV6:
            host = str(IPv6Address(received(client, IPV6_BYTES)))
        case AddressKind.NAME:
            name = HOST_NAME.fullmatch(
                received(client, received(client, NAME_LENGTH_BYTES)[0]).decode("latin-1")
            )
            if name is None:
                raise MalformedRequest(SocksReply.GENERAL_FAILURE)
            host = name["host"]
        case _:
            raise MalformedRequest(SocksReply.ADDRESS_KIND_NOT_SUPPORTED)
    return host, int.from_bytes(received(client, PORT_BYTES))


def relayed(source: socket.socket, sink: socket.socket) -> None:
    # Passes on what one side sends until it closes; a side that breaks the connection off
    # ends the relay as one that closed.
    with suppress(OSError):
        while chunk := source.recv(RELAYED_BYTES):
            sink.sendall(chunk)
        sink.shutdown(socket.SHUT_WR)


class PublicProxy(ThreadingTCPServer):
    # The proxy a browser connects through. It connects only to public addresses, and hands
    # what it refused to the one who reads them, through a queue that each connection only
    # writes to.
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__((LOOPBACK, EVERY_PORT), SocksConnection)
        self.refused: SimpleQueue[Refusal] = SimpleQueue()
        self.carried = BoundedSemaphore(CONNECTIONS_AT_ONCE)

    def port(self) -> int:
        return int(self.server_address[1])

    def serve_in_background(self) -> None:
        Thread(target=self.serve_forever, daemon=True).start()

    def refusals(self) -> list[Refusal]:
        # What the proxy refused since it was last asked.
        found: list[Refusal] = []
        while not self.refused.empty():
            found.append(self.refused.get())
        return found

    def process_request(self, request: Any, client_address: Any) -> None:
        # A connection beyond those the proxy carries at once is closed as it comes.
        if self.carried.acquire(blocking=False):
            super().process_request(request, client_address)
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.carried.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A connection the browser breaks off, or holds silent past its time, ends there; the
        # browser opens connections ahead of its needs and drops those it does not use.
        if not isinstance(sys.exception(), OSError):
            super().handle_error(request, client_address)


class SocksConnection(BaseRequestHandler):
    # One connection the browser asks the proxy for, carried until either side closes it.
    server: PublicProxy

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(HANDSHAKE_LIMIT_SECONDS)
        try:
            host, port = asked_destination(client)
        except MalformedRequest as malformed:
            client.sendall(socks_reply(malformed.reply))
            return
        # The browser is told only that the connection was refused; the proxy keeps the
        # reason, for the report of a page that could not be opened.
        try:
            upstream = public_connection(host, port)
        except (OSError, UnreadableSource) as refused:
            self.server.refused.put(Refusal(host=host, port=port, reason=error_text(refused)))
            client.sendall(socks_reply(SocksReply.NOT_ALLOWED))
            return
        with upstream:
            client.settimeout(None)
            client.sendall(socks_reply(SocksReply.SUCCEEDED))
            answers = Thread(target=relayed, args=(upstream, client), daemon=True)
            answers.start()
            relayed(client, upstream)
            answers.join()
