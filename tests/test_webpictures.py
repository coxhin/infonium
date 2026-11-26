import asyncio
import gzip
import logging
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from ipaddress import ip_address
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from infonium import egress, intake, webpictures
from infonium.document import Block, Extracted, Figure, Paragraph
from tests.unix_server import unix_server

# The pictures of a web page's figures, downloaded through the transport that judges every
# connection. The pictures are served by a site of this machine, which the real judgment
# refuses, so a stand-in for the judgment takes the names of the test's sites for public and
# leads them to that site; every other host is judged as egress judges it. The default run
# refuses to look up any name, so names are looked up as a resolver of this machine would,
# without the network: an address stands for itself, and localhost for this machine.
PUBLIC_LOOKING = frozenset({"pictures.test", "news.test", "cdn.test"})
LOCALHOST = ("127.0.0.1", "::1")
PAGE = "http://news.test/night-trains?part=2#comments"
# A server is given this long to say something, the time a test waits for one that does not.
SILENCE = 5.0
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
    b'<rect width="400" height="300" fill="teal"/></svg>'
)
HTML = b"<!doctype html><html><body><p>This is no picture.</p></body></html>"
# A name that a resolver gives an address of a home network for, beside a public one, and one
# that it finds no address for.
REBOUND_NAME = "rebound.example"
REBOUND_ADDRESSES = ("93.184.215.14", "192.168.1.20")
UNKNOWN_NAME = "nowhere.example"
NAMED = {"localhost": LOCALHOST, REBOUND_NAME: REBOUND_ADDRESSES}
# A host that none of the test's sites is, and the port a site answers over TLS on.
OTHER_HOST = "other.test"
HTTPS_PORT = 443
# How the certificate authority of a test session, and the certificates it signs for servers,
# are made, with the extensions that Python's strict checking of certificates asks for. The
# authority and its certificates are made anew for each session and trusted by its tests alone.
AUTHORITY_CONFIG = """[req]
distinguished_name = name
[name]
[authority]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""
SERVER_EXTENSIONS = """basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
subjectAltName = {names}
"""
NEW_KEY = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
VALID_DAYS = "2"
EXAMPLES = settings(max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture])


def picture(kind: str, width: int = 240, height: int = 160, noisy: bool = False) -> bytes:
    # A picture of the kind Pillow writes by that name; a noisy one hardly compresses, and an
    # MPO is a JPEG that holds a second picture.
    content = (
        Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        if noisy
        else Image.new("RGB", (width, height), (90, 120, 200))
    )
    further = [Image.new("RGB", (width, height), (200, 120, 90))] if kind == "MPO" else []
    with BytesIO() as written:
        content.save(
            written, kind, **({"save_all": True, "append_images": further} if further else {})
        )
        return written.getvalue()


PNG = picture("PNG")


class Answer(BaseModel):
    # How the site answers a path: a status, headers, and a body sent in as many pieces as
    # given, after a wait before the head and a pause after each piece. A body is sent with its
    # length unless it is chunked, or claimed to be of another length.
    status: int = 200
    headers: dict[str, str] = {}
    body: bytes = b""
    pieces: int = 1
    wait: float = 0
    pause: float = 0
    chunked: bool = False
    claimed: int | None = None


def moved(address: str) -> Answer:
    return Answer(status=302, headers={"location": address})


class PictureSite:
    # The site of this machine, answering in the order it is asked: what it heard, by host and
    # path, and the most requests it answered at once. A host may be led to a server of a
    # socket of its own instead.
    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.heard: list[tuple[str, dict[str, str]]] = []
        self.answering = 0
        self.most_at_once = 0
        self.servers: dict[str, Path] = {}
        # Where the site answers over TLS, while it does.
        self.secure: Path | None = None

    def reset(self) -> None:
        self.answers.clear()
        self.heard.clear()
        self.most_at_once = 0

    def asked(self) -> list[str]:
        # What was asked for, in no order: pictures are asked for several at a time.
        return sorted(address for address, _ in self.heard)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = {
            name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]
        }
        self.heard.append((f"{headers['host']}{scope['path']}", headers))
        self.answering += 1
        self.most_at_once = max(self.most_at_once, self.answering)
        try:
            answer = self.answers.get(scope["path"], Answer(status=404))
            await asyncio.sleep(answer.wait)
            head = [(name.encode(), value.encode()) for name, value in answer.headers.items()]
            if not answer.chunked:
                length = len(answer.body) if answer.claimed is None else answer.claimed
                head.append((b"content-length", str(length).encode()))
            await send({"type": "http.response.start", "status": answer.status, "headers": head})
            size = max(1, -(-len(answer.body) // answer.pieces))
            for start in range(0, len(answer.body), size):
                piece = answer.body[start : start + size]
                await send({"type": "http.response.body", "body": piece, "more_body": True})
                await asyncio.sleep(answer.pause)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.answering -= 1


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Iterator[PictureSite]:
    served = PictureSite()
    judged = egress.public_connection
    with unix_server(served, lifespan="off") as path:

        def connection(host: str, port: int) -> socket.socket:
            if host not in PUBLIC_LOOKING | served.servers.keys():
                return judged(host, port)
            secure = served.secure if port == HTTPS_PORT else None
            local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            local.connect(str(served.servers.get(host, secure or path)))
            return local

        def lookup(host: str, port: int, *arguments: Any, **options: Any) -> list[Any]:
            addresses = NAMED.get(host, (host,))
            try:
                found = [ip_address(address) for address in addresses]
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
            return [
                (
                    socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (str(address), port, 0, 0) if address.version == 6 else (str(address), port),
                )
                for address in found
            ]

        monkeypatch.setattr(egress, "public_connection", connection)
        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        yield served


class Certified(BaseModel):
    # A server's certificate and its key.
    certificate: Path
    key: Path


class Authority(BaseModel):
    # A certificate authority made for the test session, and the certificates it signed: one
    # that names the hosts of the test's sites, and one that names another host.
    certificate: Path
    sites: Certified
    other: Certified


def made(command: list[str | Path]) -> None:
    subprocess.run(command, check=True, capture_output=True)


@pytest.fixture(scope="session")
def authority(tmp_path_factory: pytest.TempPathFactory) -> Authority:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not found: put it on the PATH, to make the tests' certificates")
    folder = tmp_path_factory.mktemp("authority")
    config = folder / "authority.cnf"
    config.write_text(AUTHORITY_CONFIG)
    certificate, key = folder / "authority.pem", folder / "authority.key"
    subject = "/CN=infonium test authority"
    made([openssl, "req", "-x509", "-config", config, "-extensions", "authority", *NEW_KEY,
          "-keyout", key, "-out", certificate, "-days", VALID_DAYS, "-subj", subject])  # fmt: skip
    signed: dict[str, Certified] = {}
    for serial, (name, hosts) in enumerate(
        (("sites", sorted(PUBLIC_LOOKING)), ("other", [OTHER_HOST])), start=1
    ):
        server = Certified(certificate=folder / f"{name}.pem", key=folder / f"{name}.key")
        request, extensions = folder / f"{name}.csr", folder / f"{name}.ext"
        extensions.write_text(
            SERVER_EXTENSIONS.format(names=",".join(f"DNS:{host}" for host in hosts))
        )
        made([openssl, "req", "-new", "-config", config, *NEW_KEY, "-keyout", server.key,
              "-out", request, "-subj", f"/CN={hosts[0]}"])  # fmt: skip
        made([openssl, "x509", "-req", "-in", request, "-CA", certificate, "-CAkey", key,
              "-set_serial", str(serial), "-days", VALID_DAYS, "-extfile", extensions,
              "-out", server.certificate])  # fmt: skip
        signed[name] = server
    return Authority(certificate=certificate, sites=signed["sites"], other=signed["other"])


@contextmanager
def secured(site: PictureSite, server: Certified) -> Iterator[None]:
    # The site answers on the port of HTTPS too, over TLS, with the certificate given.
    with unix_server(site, lifespan="off", certificate=server.certificate, key=server.key) as path:
        site.secure = path
        try:
            yield
        finally:
            site.secure = None


def trusting(monkeypatch: pytest.MonkeyPatch, authority: Authority) -> None:
    # Pictures are downloaded over a transport that trusts the test session's authority alone.
    context = ssl.create_default_context(cafile=authority.certificate)
    monkeypatch.setattr(
        webpictures, "public_transport", lambda: webpictures.PublicTransport(context)
    )


def page_with(*sources: str) -> Extracted:
    blocks: list[Block] = [Paragraph(text="Night trains cross the valley.", marks=[])]
    blocks += [
        Figure(source=source, text=f"Picture {number}") for number, source in enumerate(sources)
    ]
    return Extracted(title="Night trains", blocks=blocks)


def downloaded(
    extracted: Extracted, folder: Path, page: str = PAGE, silence: float = SILENCE
) -> Extracted:
    return webpictures.with_pictures(extracted, page, folder, intake.USER_AGENT, silence)


def pictured(extracted: Extracted) -> list[str]:
    # The sources of the figures that were given a picture, in the order of the blocks.
    sources = []
    for handed in extracted.pictures:
        figure = extracted.blocks[handed.block]
        assert isinstance(figure, Figure), "a picture was handed over for a block that is no figure"
        sources.append(figure.source)
    return sources


def reasons(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    # Why each picture that keeps its source keeps it, as the log says, by its address.
    return {
        str(record.args[0]): str(record.args[2])
        for record in caplog.records
        if record.msg == webpictures.KEEPS_SOURCE and isinstance(record.args, tuple)
    }


@pytest.fixture
def logged(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=webpictures.__name__)
    return caplog


def test_each_address_is_downloaded_once_and_kept_as_served_by_the_kind_its_bytes_are(
    site: PictureSite, tmp_path: Path
) -> None:
    kinds = {"JPEG": "image/jpeg", "MPO": "image/jpeg", "PNG": "image/png", "GIF": "image/gif"}
    kinds |= {"WEBP": "image/webp", "AVIF": "image/avif"}
    for kind in kinds:
        # Served as bytes of no kind, which the bytes themselves overrule.
        site.answers[f"/{kind}"] = Answer(
            body=picture(kind), headers={"content-type": "application/octet-stream"}
        )
    sources = [f"http://pictures.test/{kind}" for kind in kinds]
    extracted = downloaded(page_with(*sources, sources[0]), tmp_path)
    assert pictured(extracted) == [*sources, sources[0]], "a picture of a kind kept was not kept"
    for handed, kind in zip(extracted.pictures, kinds, strict=False):
        assert handed.media_type == kinds[kind], (
            f"a {kind} picture is said to be {handed.media_type}"
        )
        assert handed.file.read_bytes() == site.answers[f"/{kind}"].body, (
            f"a {kind} picture was not kept as it was served"
        )
        assert (handed.width, handed.height) == (240, 160), f"a {kind} picture has another size"
    assert extracted.pictures[-1].file == extracted.pictures[0].file, (
        "two figures of one address were not given one file"
    )
    assert site.asked() == sorted(f"pictures.test/{kind}" for kind in kinds), (
        "an address was asked for more than once, or another was asked for"
    )
    assert extracted.blocks == page_with(*sources, sources[0]).blocks, "the blocks were changed"


def test_a_picture_is_asked_for_as_a_browser_that_shows_the_page_asks_for_it(
    site: PictureSite, tmp_path: Path
) -> None:
    site.answers["/same"] = Answer(body=PNG)
    site.answers["/other"] = Answer(body=PNG)
    downloaded(page_with("http://news.test/same", "http://cdn.test/other"), tmp_path)
    heard = dict(site.heard)
    assert heard["news.test/same"]["referer"] == "http://news.test/night-trains?part=2", (
        "a picture of the page's own origin was not given the page's address as its referrer"
    )
    assert heard["cdn.test/other"]["referer"] == "http://news.test/", (
        "a picture of another origin was not given the page's origin as its referrer"
    )
    for address, headers in heard.items():
        assert headers["user-agent"] == intake.USER_AGENT, f"{address} heard another user agent"
        assert headers["accept"] == webpictures.PICTURE_ACCEPT, f"{address} was not asked a picture"
        assert headers["accept-encoding"] == "identity", f"{address} was asked to compress it"
    site.reset()
    site.answers["/other"] = Answer(body=PNG)
    downloaded(page_with("http://cdn.test/other"), tmp_path, page="night trains.html")
    assert "referer" not in dict(site.heard)["cdn.test/other"], (
        "a picture of an uploaded page was given a referrer"
    )


@pytest.mark.parametrize(
    ("page", "address", "referrer"),
    [
        ("https://sspai.com/post/1#top", "https://sspai.com/a.png", "https://sspai.com/post/1"),
        ("https://sspai.com/post/1", "https://cdnfile.sspai.com/a.png", "https://sspai.com/"),
        (
            "https://a:b@sspai.com:8443/p?q=1",
            "https://sspai.com:8443/a",
            "https://sspai.com:8443/p?q=1",
        ),
        ("https://sspai.com/post/1", "http://sspai.com/a.png", None),
        ("http://example.com/post", "https://cdn.example.com/a.png", "http://example.com/"),
        ("night trains.html", "https://cdn.example.com/a.png", None),
    ],
)
def test_a_picture_is_given_the_referrer_a_browser_gives_it_unless_a_page_says_otherwise(
    page: str, address: str, referrer: str | None
) -> None:
    assert webpictures.referrer_of(page, address) == referrer, (
        f"a picture at {address} of the page {page} was given another referrer"
    )


def test_a_picture_over_tls_is_kept_where_a_trusted_authority_certified_its_host(
    site: PictureSite, tmp_path: Path, authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusting(monkeypatch, authority)
    site.answers["/secure.png"] = Answer(body=PNG)
    with secured(site, authority.sites):
        extracted = downloaded(page_with("https://pictures.test/secure.png"), tmp_path)
    assert pictured(extracted) == ["https://pictures.test/secure.png"], (
        "a picture over TLS from a host its certificate names was not kept"
    )
    assert site.asked() == ["pictures.test/secure.png"], "the picture was not asked for once"


@pytest.mark.parametrize(
    ("certified", "trusted", "said"),
    [
        ("other", True, "Hostname mismatch"),
        ("sites", False, "unable to get local issuer certificate"),
    ],
)
def test_a_picture_over_tls_is_refused_where_its_certificate_names_another_host_or_is_untrusted(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    authority: Authority,
    monkeypatch: pytest.MonkeyPatch,
    certified: str,
    trusted: bool,
    said: str,
) -> None:
    if trusted:
        trusting(monkeypatch, authority)
    site.answers["/secure.png"] = Answer(body=PNG)
    with secured(site, authority.other if certified == "other" else authority.sites):
        extracted = downloaded(page_with("https://pictures.test/secure.png"), tmp_path)
    assert pictured(extracted) == [], "a picture over TLS was kept from a host not proved"
    told = reasons(logged)["https://pictures.test/secure.png"]
    assert told.startswith("ConnectError: ") and "certificate verify failed" in told, told
    assert said in told, f"a certificate was refused otherwise than for {said!r}: {told}"
    assert site.asked() == [], "a picture was asked of a host that did not prove it was named"


def test_each_address_a_redirect_leads_to_is_given_the_referrer_a_browser_gives_it(
    site: PictureSite, tmp_path: Path, authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A page whose address holds a secret, with a picture that redirects from the page's own
    # site to another, and with one that redirects from a secure address to one that is not.
    shared = "news.test/shared/doc?token=SECRET-123"
    trusting(monkeypatch, authority)
    site.answers |= {
        "/same.png": moved("http://cdn.test/moved.png"),
        "/moved.png": Answer(body=PNG),
        "/secure.png": moved("http://cdn.test/plain.png"),
        "/plain.png": Answer(body=PNG),
    }
    with secured(site, authority.sites):
        kept = [
            *pictured(
                downloaded(page_with("http://news.test/same.png"), tmp_path, f"http://{shared}")
            ),
            *pictured(
                downloaded(
                    page_with("https://pictures.test/secure.png"), tmp_path, f"https://{shared}"
                )
            ),
        ]
    assert kept == ["http://news.test/same.png", "https://pictures.test/secure.png"], (
        "a picture behind a redirect was not kept"
    )
    assert {address: headers.get("referer") for address, headers in site.heard} == {
        "news.test/same.png": f"http://{shared}",
        "cdn.test/moved.png": "http://news.test/",
        "pictures.test/secure.png": "https://news.test/",
        "cdn.test/plain.png": None,
    }, "an address a redirect led to was given the referrer of the address before it"


def test_svg_and_bytes_of_no_kind_kept_leave_their_figures_at_their_sources(
    site: PictureSite, tmp_path: Path, logged: pytest.LogCaptureFixture
) -> None:
    answers = {
        "/drawing.svg": Answer(body=SVG, headers={"content-type": "image/svg+xml"}),
        "/page": Answer(body=HTML, headers={"content-type": "text/html"}),
        "/scan.tiff": Answer(body=picture("TIFF"), headers={"content-type": "image/tiff"}),
        "/old.bmp": Answer(body=picture("BMP")),
        "/cut.png": Answer(body=PNG[:20], headers={"content-type": "image/png"}),
        "/empty": Answer(),
    }
    site.answers |= answers
    sources = [f"http://pictures.test{path}" for path in answers]
    assert pictured(downloaded(page_with(*sources), tmp_path)) == [], "no picture was kept"
    assert reasons(logged) == {
        f"http://pictures.test{path}": webpictures.OTHER_KIND.format(
            served=webpictures.served_as(answer.headers.get("content-type", ""))
        )
        for path, answer in answers.items()
    }, "bytes of a kind not kept were not told as such"


def test_a_picture_served_compressed_is_refused_with_none_of_its_bytes_decoded(
    site: PictureSite, tmp_path: Path, logged: pytest.LogCaptureFixture
) -> None:
    # A picture compressed once or twice that would be a picture kept once undone, one said to
    # be compressed as it is not, and 64 MB of nothing compressed into 64 KB.
    nothing = bytes(64 * 1024**2)
    bomb = gzip.compress(nothing, 9)
    del nothing
    answers = {
        "/once": Answer(body=gzip.compress(PNG), headers={"content-encoding": "gzip"}),
        "/twice": Answer(
            body=gzip.compress(gzip.compress(PNG)), headers={"content-encoding": "gzip, gzip"}
        ),
        "/claimed": Answer(body=PNG, headers={"content-encoding": "br"}),
        "/bomb": Answer(body=bomb, headers={"content-encoding": "GZIP"}),
        "/plain": Answer(body=PNG, headers={"content-encoding": "identity"}),
    }
    site.answers |= answers
    sources = [f"http://pictures.test{path}" for path in answers]
    tracemalloc.start()
    try:
        extracted = downloaded(page_with(*sources), tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert pictured(extracted) == sources[-1:], "a picture served compressed was kept"
    assert reasons(logged) == {
        f"http://pictures.test{path}": webpictures.ENCODED.format(
            coding=answer.headers["content-encoding"]
        )
        for path, answer in answers.items()
        if path != "/plain"
    }, "a picture served compressed was not refused as such"
    assert peak < 16 * 1024**2, f"downloading the pictures took {peak / 1024**2:.0f} MB at most"


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_small_pictures_and_decompression_bombs_leave_their_figures_at_their_sources(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 20_000)
    sizes = {"icon": (40, 40), "spacer": (95, 95), "banner": (96, 20)}
    sizes |= {"warned": (200, 120), "bomb": (300, 200)}
    for name, (width, height) in sizes.items():
        site.answers[f"/{name}"] = Answer(body=picture("PNG", width, height))
    extracted = downloaded(page_with(*(f"http://pictures.test/{name}" for name in sizes)), tmp_path)
    assert pictured(extracted) == ["http://pictures.test/banner"], (
        "a picture that reaches the size of one kept on one side was not kept, or another was"
    )
    told = reasons(logged)
    assert told["http://pictures.test/icon"] == webpictures.SMALL.format(width=40, height=40)
    assert told["http://pictures.test/spacer"] == webpictures.SMALL.format(width=95, height=95)
    assert told["http://pictures.test/warned"] == webpictures.BOMB.format(width=200, height=120)
    assert "decompression bomb" in told["http://pictures.test/bomb"], (
        f"a picture Pillow refuses as a bomb was told otherwise: {told['http://pictures.test/bomb']}"
    )


def test_a_picture_that_holds_more_than_a_picture_may_leaves_its_figure_at_its_source(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PICTURE_LIMIT_BYTES", 64 * 1024)
    large = picture("PNG", 300, 300, noisy=True)
    assert len(large) > 2 * 64 * 1024, "the large picture is small: a test's fault"
    site.answers["/declared"] = Answer(body=large)
    site.answers["/undeclared"] = Answer(body=large, chunked=True, pieces=8)
    site.answers["/fits"] = Answer(body=PNG)
    sources = [f"http://pictures.test/{name}" for name in ("declared", "undeclared", "fits")]
    assert pictured(downloaded(page_with(*sources), tmp_path)) == [sources[2]], (
        "a picture of more bytes than a picture may hold was kept, or one of fewer was not"
    )
    assert reasons(logged) == dict.fromkeys(sources[:2], webpictures.TOO_LARGE), (
        "a picture of too many bytes was not told as such"
    )
    assert [file.name for file in tmp_path.rglob("*") if file.is_file()] == ["2"], (
        "the bytes of a picture that was not kept were left in the reading's folder"
    )


def test_the_pictures_of_a_page_together_hold_no_more_than_a_page_may_bring(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PICTURES_AT_ONCE", 1)
    noisy = [picture("PNG", 120, 110, noisy=True) for _ in range(3)]
    monkeypatch.setattr(webpictures, "PAGE_PICTURES_LIMIT_BYTES", len(noisy[0]) + len(noisy[1]))
    for number, content in enumerate(noisy):
        site.answers[f"/{number}"] = Answer(body=content, chunked=True, pieces=4)
    sources = [f"http://pictures.test/{number}" for number in range(3)]
    assert pictured(downloaded(page_with(*sources), tmp_path)) == sources[:2], (
        "the pictures of a page that fit what it may bring were not kept, or more were"
    )
    assert reasons(logged) == {sources[2]: webpictures.PAGE_TOO_LARGE}, (
        "a picture beyond what its page may bring was not told as such"
    )


def test_a_silent_or_slow_server_leaves_its_picture_at_its_source_without_being_waited_for(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PICTURE_LIMIT_SECONDS", 1.0)
    site.answers["/silent"] = Answer(body=PNG, wait=3)
    site.answers["/trickling"] = Answer(body=PNG, pieces=12, pause=0.25)
    site.answers["/quick"] = Answer(body=PNG)
    sources = [f"http://pictures.test/{name}" for name in ("silent", "trickling", "quick")]
    began = time.monotonic()
    extracted = downloaded(page_with(*sources), tmp_path, silence=0.5)
    took = time.monotonic() - began
    assert pictured(extracted) == [sources[2]], "a picture that took too long was kept"
    told = reasons(logged)
    assert told[sources[0]].startswith("ReadTimeout"), f"a silent server was told {told}"
    assert told[sources[1]] == webpictures.OVERDUE, f"a trickling server was told {told}"
    assert took < 2.5, f"the pictures of a page were waited for {took:.1f} s"


class DribbledHead(socketserver.BaseRequestHandler):
    # A server that sends the head of its answer a byte at a time, each well within the silence
    # a server is allowed.
    def handle(self) -> None:
        self.request.recv(64 * 1024)
        for byte in b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 4\r\n\r\nPNG!":
            try:
                self.request.sendall(bytes([byte]))
            except OSError:
                return
            time.sleep(0.2)


class DribblingServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


@contextmanager
def dribbling() -> Iterator[Path]:
    # The folder is short-named because a socket's path is held to about a hundred bytes.
    with tempfile.TemporaryDirectory(prefix="infonium-") as folder:
        path = Path(folder) / "dribbling.sock"
        with DribblingServer(str(path), DribbledHead) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                yield path
            finally:
                server.shutdown()


def test_a_server_that_dribbles_its_answer_is_given_up_when_the_time_of_its_picture_is(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PICTURE_LIMIT_SECONDS", 1.0)
    with dribbling() as path:
        site.servers["slow.test"] = path
        began = time.monotonic()
        extracted = downloaded(page_with("http://slow.test/a.png"), tmp_path, silence=0.5)
        took = time.monotonic() - began
    assert pictured(extracted) == [], "a picture whose answer took too long was kept"
    assert reasons(logged) == {"http://slow.test/a.png": webpictures.OVERDUE}, (
        "a picture whose answer took too long was told otherwise"
    )
    assert took < 2, f"a picture of 1 second was waited for {took:.1f} s"


def test_pictures_not_done_when_the_time_of_their_page_is_up_keep_their_sources(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PAGE_PICTURES_LIMIT_SECONDS", 1.0)
    monkeypatch.setattr(webpictures, "PICTURES_AT_ONCE", 1)
    for number in range(4):
        site.answers[f"/{number}"] = Answer(body=PNG, wait=0.7)
    sources = [f"http://pictures.test/{number}" for number in range(4)]
    began = time.monotonic()
    extracted = downloaded(page_with(*sources), tmp_path)
    took = time.monotonic() - began
    assert pictured(extracted) == sources[:1], "a picture done in its page's time was not kept"
    assert reasons(logged) == dict.fromkeys(sources[1:], webpictures.PAGE_OVERDUE), (
        "a picture under way when its page's time was up, or one not begun by then, was told "
        "otherwise"
    )
    assert took < 2, f"the pictures of a page were waited for {took:.1f} s"


def test_no_more_than_six_pictures_are_downloaded_at_a_time(
    site: PictureSite, tmp_path: Path
) -> None:
    for number in range(14):
        site.answers[f"/{number}"] = Answer(body=PNG, wait=0.3)
    sources = [f"http://pictures.test/{number}" for number in range(14)]
    assert pictured(downloaded(page_with(*sources), tmp_path)) == sources, "a picture was lost"
    assert site.most_at_once == webpictures.PICTURES_AT_ONCE == 6, (
        f"{site.most_at_once} pictures were downloaded at once"
    )


def test_only_the_first_pictures_of_a_page_are_downloaded(
    site: PictureSite,
    tmp_path: Path,
    logged: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webpictures, "PICTURES_A_PAGE", 3)
    for number in range(5):
        site.answers[f"/{number}"] = Answer(body=PNG)
    sources = [f"http://pictures.test/{number}" for number in range(5)]
    extracted = downloaded(page_with(*sources, sources[0]), tmp_path)
    assert pictured(extracted) == [*sources[:3], sources[0]], (
        "the first pictures of a page, each wherever it is shown, were not the ones kept"
    )
    assert site.asked() == [f"pictures.test/{number}" for number in range(3)], (
        "a picture beyond those a page has downloaded was asked for"
    )
    assert reasons(logged) == dict.fromkeys(sources[3:], webpictures.BEYOND)


def test_a_picture_is_followed_through_a_few_redirects_at_most(
    site: PictureSite, tmp_path: Path, logged: pytest.LogCaptureFixture
) -> None:
    site.answers["/moved"] = moved("http://cdn.test/picture")
    site.answers["/picture"] = Answer(body=PNG)
    hops = webpictures.REDIRECTS + 1
    for hop in range(hops):
        site.answers[f"/far-{hop}"] = moved(f"http://pictures.test/far-{hop + 1}")
    site.answers[f"/far-{hops}"] = Answer(body=PNG)
    sources = ["http://pictures.test/moved", "http://pictures.test/far-0"]
    assert pictured(downloaded(page_with(*sources), tmp_path)) == sources[:1], (
        "a picture behind a redirect was not kept, or one behind too many was"
    )
    assert reasons(logged)[sources[1]] == webpictures.TOO_MANY_REDIRECTS, (
        "a picture behind too many redirects was told otherwise"
    )
    assert f"pictures.test/far-{hops}" not in site.asked(), (
        "a picture behind too many redirects was asked for"
    )


def test_the_judgment_refuses_this_machine_and_its_networks_also_after_a_redirect(
    site: PictureSite, tmp_path: Path, logged: pytest.LogCaptureFixture
) -> None:
    refused = [
        "http://127.0.0.1:8080/a.png",
        "http://localhost/a.png",
        "http://10.1.2.3/a.png",
        "http://172.16.5.4/a.png",
        "http://192.168.1.20/a.png",
        "http://169.254.169.254/latest/meta-data",
        f"http://{REBOUND_NAME}/a.png",
    ]
    redirects = [f"http://pictures.test/via-{number}" for number in range(len(refused))]
    for number, address in enumerate(refused):
        site.answers[f"/via-{number}"] = moved(address)
    extracted = downloaded(page_with(*refused, *redirects), tmp_path)
    assert pictured(extracted) == [], "a picture of a local address was kept"
    told = reasons(logged)
    for address in [*refused, *redirects]:
        assert told[address].startswith("NotPublicAddress: "), (
            f"{address} was not refused by the judgment: {told[address]}"
        )
    assert site.asked() == sorted(address.removeprefix("http://") for address in redirects), (
        "the site of this machine heard more than the redirects"
    )


BEHAVIOURS = {
    "picture": Answer(body=PNG),
    "svg": Answer(body=SVG, headers={"content-type": "image/svg+xml"}),
    "missing": Answer(status=404),
    "failing": Answer(status=500, body=HTML),
    "cut": Answer(body=PNG[:20]),
    "broken-off": Answer(body=PNG[:100], claimed=len(PNG)),
    "looping": moved("http://pictures.test/looping"),
    "local": moved("http://127.0.0.1/a.png"),
    "unknown": moved(f"http://{UNKNOWN_NAME}/a.png"),
    "elsewhere": moved("ftp://pictures.test/a.png"),
}


@given(behaviours=st.lists(st.sampled_from(sorted(BEHAVIOURS)), min_size=1, max_size=8))
@EXAMPLES
def test_nothing_that_befalls_a_picture_fails_its_page_or_keeps_what_is_no_picture(
    site: PictureSite, tmp_path: Path, behaviours: list[str]
) -> None:
    site.reset()
    site.answers |= {f"/{name}": answer for name, answer in BEHAVIOURS.items()}
    sources = [f"http://pictures.test/{name}" for name in behaviours]
    extracted = downloaded(page_with(*sources), tmp_path)
    assert pictured(extracted) == [source for source in sources if source.endswith("/picture")], (
        f"of pictures that met {behaviours}, other than the pictures were kept"
    )
