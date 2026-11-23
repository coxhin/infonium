import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from starlette.types import ASGIApp

SOCKET_NAME = "server.sock"
START_SECONDS = 10.0
POLL_SECONDS = 0.01
# A connection left open by a test is closed this long after the server is told to stop.
CLOSING_SECONDS = 1


@contextmanager
def unix_server(app: ASGIApp, lifespan: Literal["on", "off"]) -> Iterator[Path]:
    # An ASGI application served by uvicorn in this process over a Unix socket, which the
    # default run allows: a client of it speaks HTTP over a real connection, and closing that
    # connection is a client leaving. The folder is short-named because a socket's path is
    # held to about a hundred bytes.
    with tempfile.TemporaryDirectory(prefix="infonium-") as folder:
        path = Path(folder) / SOCKET_NAME
        config = uvicorn.Config(
            app,
            uds=str(path),
            lifespan=lifespan,
            log_level="warning",
            timeout_graceful_shutdown=CLOSING_SECONDS,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + START_SECONDS
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"uvicorn did not start on {path}")
            time.sleep(POLL_SECONDS)
        try:
            yield path
        finally:
            server.should_exit = True
            thread.join()
