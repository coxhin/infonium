import errno
import os
import shutil
from contextlib import suppress
from pathlib import Path

from playwright.sync_api import TimeoutError as WaitOverdue
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

from infonium.document import UnreadableSource

RENDERED_FILE = "rendered"
FIRST_ERROR_STATUS = 400
# How long a page may take to load, how long it is then given to fall quiet, and how long
# what its scripts still do after that is waited for, in milliseconds.
LOAD_LIMIT_MS = 30_000
QUIET_LIMIT_MS = 8_000
SETTLING_MS = 1_500


class RenderedPage(BaseModel):
    # A page as a browser built it: its HTML once its scripts have run, kept as a file, and
    # the address the browser ended at.
    file: Path
    address: str


def rendered_page(url: str, directory: Path, browser: Path) -> RenderedPage:
    program = shutil.which(browser)
    if program is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(browser))
    file = directory / RENDERED_FILE
    with sync_playwright() as playwright:
        # Playwright starts a browser without its sandbox unless it is told otherwise.
        chrome = playwright.chromium.launch(executable_path=program, chromium_sandbox=True)
        page = chrome.new_page()
        answer = page.goto(url, wait_until="load", timeout=LOAD_LIMIT_MS)
        if answer is not None and answer.status >= FIRST_ERROR_STATUS:
            raise UnreadableSource(f"{url} answered {answer.status}")
        # A page that never falls quiet is taken as it stands when the wait ends.
        with suppress(WaitOverdue):
            page.wait_for_load_state("networkidle", timeout=QUIET_LIMIT_MS)
        page.wait_for_timeout(SETTLING_MS)
        file.write_text(page.content(), encoding="utf-8")
        address = page.url
    return RenderedPage(file=file, address=address)
