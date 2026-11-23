import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import settings

from infonium.settings import Settings
from tests import pdf_engine_standin

# The examples of a property run as fast as the extractor and the parsers they call, whose
# first calls load what they keep; a deadline per example would fail them at random.
settings.register_profile("infonium", deadline=None)
settings.load_profile("infonium")

# The programs a marked test needs, found as infonium finds them: Chrome and MinerU by the
# settings, which a variable of the environment may name, and FFmpeg's on the PATH.
APP_SETTINGS = Settings()
MARKED_PROGRAMS = {
    "chrome": (APP_SETTINGS.browser,),
    "ffmpeg": (Path("ffmpeg"), Path("ffprobe")),
    "mineru": (APP_SETTINGS.pdf_engine,),
}
SETTING_HINT = {
    "chrome": "set INFONIUM_BROWSER or put google-chrome on the PATH",
    "ffmpeg": "put ffmpeg and ffprobe on the PATH",
    "mineru": "set INFONIUM_PDF_ENGINE or put mineru on the PATH",
}


def found_program(program: Path) -> Path | None:
    found = shutil.which(program)
    return None if found is None else Path(found)


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker, programs in MARKED_PROGRAMS.items():
        missing = [str(program) for program in programs if found_program(program) is None]
        if item.get_closest_marker(marker) and missing:
            pytest.skip(f"{', '.join(missing)} not found: {SETTING_HINT[marker]}")


@pytest.fixture
def chrome() -> Path:
    found = found_program(APP_SETTINGS.browser)
    if found is None:
        pytest.fail("a test that needs Chrome is marked chrome")
    return found


@pytest.fixture
def pdf_engine() -> Path:
    found = found_program(APP_SETTINGS.pdf_engine)
    if found is None:
        pytest.fail("a test that needs MinerU is marked mineru")
    return found


@pytest.fixture
def standin_pdf_engine(tmp_path: Path) -> Callable[[str], Path]:
    # The command of a stand-in for MinerU that replays the engine's stored results for a
    # sample of the PDF fixtures.
    def engine_for(sample: str) -> Path:
        engine = tmp_path / f"mineru-{sample}"
        engine.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{pdf_engine_standin.__file__}" {sample} "$@"\n'
        )
        engine.chmod(0o755)
        return engine

    return engine_for
