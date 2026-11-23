import subprocess
import sys
from pathlib import PurePosixPath

import openai

from infonium import lineage, recording
from infonium.document import WORD_GAP, error_text

# The most that is kept of the reason of a failure, and what stands for the rest.
REASON_LIMIT_CHARACTERS = 500
CUT_SHORT = "\N{HORIZONTAL ELLIPSIS}"


def asked_address(error: openai.APIError) -> str:
    # Where a request went, without the parts of an address that may hold a secret.
    return str(error.request.url.copy_with(userinfo=b"", query=None))


def program_name(command: object) -> str:
    # The program a command line starts, by its name alone: the rest of the line names files
    # of the work folder, which say nothing to a person. Where Python runs a module, the
    # module is the program.
    match command:
        case [sys.executable, lineage.RUN_AS_MODULE, module, *_]:
            return str(module)
        case [first, *_]:
            return PurePosixPath(str(first)).name
        case _:
            return PurePosixPath(str(command)).name


def failure_reason(error: BaseException) -> str:
    # An error's own text is the reason, but for the kinds that leave the reason out of it.
    # A program that fails says why in what it writes, not in the error that reports how it
    # exited, so its last written line is the reason, without the mark ffmpeg begins a line
    # with; a program that ended without a word, or ran out of time, is named with how it
    # ended. A request that a service refused does not carry what the service answered, and
    # one that no service answered does not say which service that was, so the reason gives
    # the address and the answer. A reason is kept as one line of a length a person reads: a
    # service may answer with a whole page.
    if isinstance(error, subprocess.CalledProcessError) and error.stderr and error.stderr.strip():
        written = error.stderr if isinstance(error.stderr, str) else error.stderr.decode()
        said = recording.COMPLAINER.sub("", written.strip().splitlines()[-1])
    elif isinstance(error, subprocess.CalledProcessError):
        said = f"{program_name(error.cmd)} ended with status {error.returncode}"
    elif isinstance(error, subprocess.TimeoutExpired):
        said = f"{program_name(error.cmd)} took longer than {error.timeout:g} seconds"
    elif isinstance(error, openai.APIStatusError):
        said = f"{asked_address(error)} answered {error.status_code}: {error.response.text}"
    elif isinstance(error, openai.APIConnectionError):
        said = f"{asked_address(error)} did not answer: {error}"
    else:
        said = error_text(error)
    line = WORD_GAP.join(said.split())
    cut = len(line) > REASON_LIMIT_CHARACTERS
    return f"{line[:REASON_LIMIT_CHARACTERS]}{CUT_SHORT}" if cut else line
