import socket
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_network
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from infonium import egress
from infonium.egress import Address, Network, is_public

# The IANA special-purpose address registries (IPv4 and IPv6, as of 2026), with the blocks
# they call not globally reachable, and the parts of those blocks that they call globally
# reachable. The IPv4-mapped block, the NAT64 prefix and 6to4's block are left out: an address
# in them is judged by the IPv4 address it carries.
NOT_GLOBALLY_REACHABLE = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128",
    "::1/128",
    "64:ff9b:1::/48",
    "100::/64",
    "100:0:0:1::/64",
    "2001::/23",
    "2001:db8::/32",
    "3fff::/20",
    "5f00::/16",
    "fc00::/7",
    "fe80::/10",
    # Not in the registries, but no host of the internet: multicast and IPv6's old site-local
    # addresses.
    "224.0.0.0/4",
    "ff00::/8",
    "fec0::/10",
)
GLOBALLY_REACHABLE = (
    "192.0.0.9/32",
    "192.0.0.10/32",
    "192.31.196.0/24",
    "192.52.193.0/24",
    "192.175.48.0/24",
    "2001:1::1/128",
    "2001:1::2/128",
    pytest.param(
        "2001:1::3/128",
        marks=pytest.mark.xfail(
            strict=True,
            reason="is_public refuses DNS-SD SRP anycast (RFC 9665), which the registry calls "
            "globally reachable: Python 3.13's ipaddress does not know it",
        ),
    ),
    "2001:3::/32",
    "2001:4:112::/48",
    "2001:20::/28",
    "2001:30::/28",
    "2620:4f:8000::/48",
)
REACHABLE_PARTS = [ip_network(block) for block in GLOBALLY_REACHABLE if isinstance(block, str)]
REACHABLE_PARTS.append(ip_network("2001:1::3/128"))
# Ordinary unicast addresses, outside every block above.
ORDINARY = (ip_network("8.0.0.0/7"), ip_network("2a00::/12"))
TEREDO_PREFIX = 0x2001_0000 << 96
SIX_TO_FOUR_PREFIX = 0x2002 << 112
IPV4_BITS = 32

networks = st.one_of(
    st.tuples(st.ip_addresses(v=4), st.integers(0, 32)),
    st.tuples(st.ip_addresses(v=6), st.integers(0, 128)),
).map(lambda host: ip_network(host, strict=False))
own_networks = st.lists(networks, max_size=4)


def in_part(address: Address, parts: list[Network]) -> bool:
    return any(address.version == part.version and address in part for part in parts)


def carrying_forms(inner: IPv4Address, suffix: int) -> dict[str, IPv6Address]:
    # The IPv6 addresses that carry `inner` for a host or a gateway to connect to, by the name
    # of their form; Teredo's carries the client with its bits inverted.
    value = int(inner)
    return {
        "mapped": IPv6Address(f"::ffff:{inner}"),
        "compatible": IPv6Address(value),
        "NAT64": IPv6Address(int(IPv6Address("64:ff9b::")) | value),
        "6to4": IPv6Address(SIX_TO_FOUR_PREFIX | value << 80 | suffix % 2**80),
        "Teredo": IPv6Address(
            TEREDO_PREFIX | (suffix % 2**64) << IPV4_BITS | (~value & 0xFFFF_FFFF)
        ),
    }


# The forms judged by the address they carry alone. Python counts every address of 6to4's and
# Teredo's blocks as not global, so an address of those is refused whatever it carries.
JUDGED_BY_CARRIED = ("mapped", "compatible", "NAT64")


@pytest.mark.parametrize("block", NOT_GLOBALLY_REACHABLE)
@given(data=st.data())
def test_no_address_of_a_block_set_aside_is_public(block: str, data: st.DataObject) -> None:
    address = data.draw(st.ip_addresses(network=block))
    assume(not in_part(address, REACHABLE_PARTS))
    assert not is_public(address, []), f"{address}, in {block}, which reaches no host, was admitted"


@pytest.mark.parametrize("block", GLOBALLY_REACHABLE)
@given(data=st.data())
def test_an_address_the_registries_call_reachable_is_public(
    block: str, data: st.DataObject
) -> None:
    address = data.draw(st.ip_addresses(network=block))
    assert is_public(address, []), f"{address}, in the reachable block {block}, was refused"


@given(address=st.one_of(*(st.ip_addresses(network=block) for block in ORDINARY)))
def test_an_ordinary_address_outside_this_machines_networks_is_public(address: Address) -> None:
    assume(not in_part(address, [ip_network(block) for block in NOT_GLOBALLY_REACHABLE]))
    assert is_public(address, []), f"the ordinary address {address} was refused"


@given(own=st.lists(networks, min_size=1, max_size=4), data=st.data())
def test_no_address_of_this_machines_networks_is_public(
    own: list[Network], data: st.DataObject
) -> None:
    network = data.draw(st.sampled_from(own))
    address = data.draw(st.ip_addresses(network=network))
    assert not is_public(address, own), f"{address}, in this machine's network {network}, admitted"


@given(address=st.ip_addresses(), own=own_networks, more=own_networks)
def test_more_networks_of_this_machine_admit_no_more(
    address: Address, own: list[Network], more: list[Network]
) -> None:
    if is_public(address, [*own, *more]):
        assert is_public(address, own), f"{address} was admitted only once more networks were own"


@given(address=st.ip_addresses(), own=own_networks)
def test_an_address_that_is_not_global_or_is_multicast_is_never_public(
    address: Address, own: list[Network]
) -> None:
    if not address.is_global or address.is_multicast:
        assert not is_public(address, own), f"{address}, not one host of the internet, admitted"


@given(
    inner=st.ip_addresses(v=4),
    own=own_networks,
    suffix=st.integers(0, 2**80 - 1),
)
def test_an_ipv6_address_carrying_an_address_that_is_not_public_is_not_public(
    inner: IPv4Address, own: list[Network], suffix: int
) -> None:
    assume(not is_public(inner, own))
    for carrying in carrying_forms(inner, suffix).values():
        assert not is_public(carrying, own), f"{carrying} was admitted, carrying {inner}"


@given(
    inner=st.ip_addresses(v=4),
    own=own_networks,
    form=st.sampled_from(JUDGED_BY_CARRIED),
)
def test_an_ipv6_address_carrying_a_public_address_is_public(
    inner: IPv4Address, own: list[Network], form: str
) -> None:
    carrying = carrying_forms(inner, 0)[form]
    assume(is_public(inner, own) and not in_part(carrying, own))
    assert is_public(carrying, own), (
        f"the {form} address {carrying}, outside this machine's networks, was refused though "
        f"the {inner} it carries is public"
    )


def interface_entry(network: Network, address: Address, zone: str, peer: Address | None) -> object:
    # An address of an interface as psutil gives it.
    is_ipv6 = isinstance(network, IPv6Network)
    named = f"{address}%{zone}" if is_ipv6 and zone else str(address)
    return SimpleNamespace(
        family=socket.AF_INET6 if is_ipv6 else socket.AF_INET,
        address=named,
        netmask=str(network.netmask),
        broadcast=None,
        ptp=None if peer is None else str(peer),
    )


@st.composite
def interface_entries(draw: st.DrawFn) -> tuple[object, Network, Address | None]:
    network = draw(networks)
    address = draw(st.ip_addresses(network=network))
    peer = draw(st.none() | st.ip_addresses(v=network.version))
    zone = draw(st.sampled_from(["", "eth0", "wlp2s0", "lo"]))
    return interface_entry(network, address, zone, peer), network, peer


@given(entries=st.lists(interface_entries(), min_size=1, max_size=4), data=st.data())
def test_every_network_an_interface_stands_in_is_this_machines(
    entries: list[tuple[object, Network, Address | None]], data: st.DataObject
) -> None:
    link = SimpleNamespace(family=psutil.AF_LINK, address="00:16:3e:00:00:01", netmask=None)
    interfaces = {f"if{number}": [entry, link] for number, (entry, _, _) in enumerate(entries)}
    with patch.object(psutil, "net_if_addrs", return_value=interfaces):
        own = egress.own_networks()
    for _, network, peer in entries:
        address = data.draw(st.ip_addresses(network=network))
        assert not is_public(address, own), f"{address}, in an interface's {network}, admitted"
        if peer is not None:
            assert not is_public(peer, own), f"{peer}, the far end of an interface, admitted"
