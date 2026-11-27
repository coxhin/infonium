import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

# A server on a port of this machine, for the judgments of the fetcher program that runs apart:
# it really connects, so it is answered over a real socket. Each path answers as a test scripts
# it: at once, through a redirect, slowly, too much, or cut off.

HOST = "127.0.0.1"
CHUNK_BYTES = 1024**2
TRICKLE_SECONDS = 0.1


class ScriptedHandler(BaseHTTPRequestHandler):
    server: "ScriptedServer"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.heard.append(self.path)
        self.server.scripts.get(self.path, missing)(self)

    def head(self, status: int, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def log_message(self, format: str, *given: object) -> None:
        # The server says nothing of the requests it hears; the test reads them from it.
        pass


Script = Callable[[ScriptedHandler], None]


class ScriptedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int) -> None:
        super().__init__((HOST, port), ScriptedHandler)
        self.scripts: dict[str, Script] = {}
        self.heard: list[str] = []

    def address(self, path: str) -> str:
        return f"http://{HOST}:{self.server_port}{path}"


@contextmanager
def scripted_server(port: int) -> Iterator[ScriptedServer]:
    server = ScriptedServer(port)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


def missing(handler: ScriptedHandler) -> None:
    sent(b"<p>Not here.</p>", "text/html", 404)(handler)


def sent(
    content: bytes, kind: str, status: int = 200, extra: tuple[tuple[str, str], ...] = ()
) -> Script:
    # An answer of the bytes given, with the headers given beside its kind and length.
    def script(handler: ScriptedHandler) -> None:
        told = {"Content-Type": kind, "Content-Length": str(len(content))} | dict(extra)
        handler.head(status, told)
        handler.wfile.write(content)

    return script


def moved(to: str) -> Script:
    def script(handler: ScriptedHandler) -> None:
        handler.head(301, {"Location": to, "Content-Length": "0"})

    return script


def poured(size: int, kind: str) -> Script:
    # An answer of the bytes given, as many as there are, sent a megabyte at a time.
    def script(handler: ScriptedHandler) -> None:
        handler.head(200, {"Content-Type": kind, "Content-Length": str(size)})
        for start in range(0, size, CHUNK_BYTES):
            handler.wfile.write(b" " * min(CHUNK_BYTES, size - start))

    return script


def trickled(opening: bytes, kind: str, seconds: float) -> Script:
    # An answer that sends its opening, then a space every tenth of a second for the seconds
    # given, and ends with the connection; a reader that goes away ends it sooner.
    def script(handler: ScriptedHandler) -> None:
        handler.head(200, {"Content-Type": kind, "Connection": "close"})
        handler.wfile.write(opening)
        handler.close_connection = True
        ends = time.monotonic() + seconds
        while time.monotonic() < ends and not handler.wfile.closed:
            time.sleep(TRICKLE_SECONDS)
            if not written(handler, b" "):
                return

    return script


def written(handler: ScriptedHandler, piece: bytes) -> bool:
    # Whether the piece reached the reader, who may have gone away.
    try:
        handler.wfile.write(piece)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


def broken_off(opening: bytes, kind: str, promised: int) -> Script:
    # An answer that promises more bytes than it sends, and closes the connection after its
    # opening.
    def script(handler: ScriptedHandler) -> None:
        handler.head(200, {"Content-Type": kind, "Content-Length": str(promised)})
        handler.wfile.write(opening)
        handler.wfile.flush()
        handler.close_connection = True
        handler.connection.close()

    return script
