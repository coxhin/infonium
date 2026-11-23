import ctypes
import glob
import os
import re
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import IO, Self

import psutil

# How Python is told to run a module as a program, as infonium runs the programs it starts
# of its own.
RUN_AS_MODULE = "-m"
# Each program infonium starts takes of infonium's environment the variables its rule names: a
# pattern that a whole name matches, made of these parts where they serve the program. The
# PATH, on which a program is found and finds the programs it starts; the locale and the time
# zone, in which a program writes; and the proxies, which Python's urllib takes from every
# variable whose name ends in _proxy, in any case.
SEARCH_PATH = "PATH"
LOCALE = r"LANG|LANGUAGE|LC_\w+|TZ"
PROXIES = r"(?i:\w+_proxy)"
# Whatever its rule names, no program takes infonium's own settings, under the prefix Settings
# reads them by, or those of the model client it holds, nor a variable named like a key, a
# token, a password or another secret.
WITHHELD = re.compile(
    r"(?:^|_)(?:INFONIUM|OPENAI)_|KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|AUTH", re.IGNORECASE
)
# Linux's option of prctl that makes a process the reaper of the orphans among its
# descendants, so that whatever they start stays its descendant when its parent dies, a
# process that leads a session of its own as well; and the one that has a process sent a
# signal when the thread that started it ends.
PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1
# A look at whether a process has ended that does not wait, and leaves it unreaped.
ENDED_UNREAPED = os.WEXITED | os.WNOHANG | os.WNOWAIT
# The status a held program ends with when the process that holds it is gone.
ABANDONED_STATUS = 1
# How long the ending of a process and all it started may take, and how long passes between
# two looks at what of it is still alive.
ENDING_LIMIT_SECONDS = 10.0
ENDING_WATCH_SECONDS = 0.02
# How long a killed process that shows as ended is given to finish: its other threads end
# within milliseconds, and one that takes longer is stuck, its children left to it.
REAPING_LIMIT_SECONDS = 1.0
# Where Linux lists the children of each thread of a process: a walk over these lists reads
# the descendants alone, where one over every process reads all the machine runs (25 ms for
# 2,171 processes here). And where it shows the files a process holds open, and the unit of
# the blocks a file takes on disk.
TASKS = "/proc/{pid}/task"
CHILDREN_LISTS = "*/children"
DESCRIPTORS = "/proc/{pid}/fd"
EVERY_ENTRY = "*"
SOCKET = re.compile(r"socket:\[(\d+)\]")
BLOCK_BYTES = 512
# A process that has ended cannot be looked at, nor, for a moment, one that is starting: it
# refuses a look at what it holds until its new program is in place. Either is passed over,
# and the next look finds the one that started.
LOOK_REFUSED = (
    FileNotFoundError,
    ProcessLookupError,
    PermissionError,
    psutil.NoSuchProcess,
    psutil.AccessDenied,
)


def program_environment(
    environment: Mapping[str, str], taken: re.Pattern[str], given: Mapping[str, str]
) -> dict[str, str]:
    # The environment a program is started with: of infonium's, the variables its rule takes
    # and none withheld, and what infonium gives the program, in place of any of these. One
    # program escapes it: loading yt-dlp has Python run file with all of infonium's environment,
    # on Python's own executable alone, so that no page or file reaches the keys through it.
    return {
        **{
            name: value
            for name, value in environment.items()
            if taken.fullmatch(name) and not WITHHELD.search(name)
        },
        **given,
    }


@contextmanager
def started(
    command: Sequence[str | Path],
    environment: Mapping[str, str],
    *,
    stdin: int | IO[bytes] | None,
    stdout: int | IO[bytes] | None,
    stderr: int | IO[bytes] | None,
) -> Iterator[subprocess.Popen[bytes]]:
    # A program started with the environment built for it, and ended when its user is done
    # with it. Given None for its standard input, it reads this process's own.
    with subprocess.Popen(
        command, stdin=stdin, stdout=stdout, stderr=stderr, env=environment
    ) as program:
        try:
            yield program
        finally:
            program.kill()


@contextmanager
def started_in_own_session(
    command: Sequence[str], environment: Mapping[str, str]
) -> Iterator[subprocess.Popen[bytes]]:
    # A program started with the environment built for it, as the leader of a session of its
    # own, so that all it starts is ended with it, and with a standard input that this process
    # holds open and writes nothing to, which closes when this process is gone. A program
    # still running when its user is done with it is ended with all its session.
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, start_new_session=True, env=environment
    ) as program:
        try:
            yield program
        finally:
            if program.returncode is None:
                os.killpg(program.pid, signal.SIGKILL)


def hold_data_to(limit: int) -> None:
    # Bounds the data this process, and each process it starts after, may hold. A hard limit
    # already in force that is lower cannot be raised, and is kept.
    hard_limit = resource.getrlimit(resource.RLIMIT_DATA)[1]
    data_limit = limit if hard_limit == resource.RLIM_INFINITY else min(limit, hard_limit)
    resource.setrlimit(resource.RLIMIT_DATA, (data_limit, data_limit))


def set_process_option(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def is_alive(process: psutil.Process) -> bool:
    # A process that has ended but is not reaped yet holds no memory and runs nothing.
    with suppress(psutil.NoSuchProcess):
        return process.status() != psutil.STATUS_ZOMBIE
    return False


def held_memory(processes: list[psutil.Process]) -> int:
    # The memory processes hold resident and share with no other: what they make of their
    # own, without the files and shared memory they map.
    held = 0
    for process in processes:
        with suppress(*LOOK_REFUSED):
            memory = process.memory_info()
            held += memory.rss - memory.shared
    return held


def held_descriptors(processes: list[psutil.Process]) -> list[tuple[str, str]]:
    # What processes hold open: each descriptor's place in /proc, and what it leads to.
    found = []
    for process in processes:
        for descriptor in glob.glob(os.path.join(DESCRIPTORS.format(pid=process.pid), EVERY_ENTRY)):
            with suppress(*LOOK_REFUSED):
                found.append((descriptor, os.readlink(descriptor)))
    return found


def held_files(descriptors: list[tuple[str, str]], folder: Path) -> int:
    # What the files under a folder that descriptors lead to take on disk, each file once,
    # those removed from the folder while held among them, which no walk of the folder finds.
    under = os.path.join(folder, "")
    blocks: dict[tuple[int, int], int] = {}
    for descriptor, target in descriptors:
        if target.startswith(under):
            with suppress(*LOOK_REFUSED):
                held = os.stat(descriptor)
                blocks[(held.st_dev, held.st_ino)] = held.st_blocks * BLOCK_BYTES
    return sum(blocks.values())


def held_sockets(descriptors: list[tuple[str, str]]) -> set[int]:
    # The sockets descriptors lead to, by the numbers Linux gives them.
    return {int(found[1]) for _, target in descriptors if (found := SOCKET.fullmatch(target))}


def descendants(pid: int) -> list[psutil.Process]:
    # The descendants of a process as they stand, those that have ended among them, as Linux
    # lists them by the threads that started them. A list read while processes start or end
    # may leave one out, which a walk made again finds.
    found: list[psutil.Process] = []
    parents = [pid]
    while parents:
        for listing in Path(TASKS.format(pid=parents.pop())).glob(CHILDREN_LISTS):
            with suppress(*LOOK_REFUSED):
                children = [psutil.Process(int(child)) for child in listing.read_text().split()]
                found += children
                parents += [child.pid for child in children]
    return found


def end_all(standing: Callable[[], list[psutil.Process]]) -> None:
    # Kills the processes `standing` gives, asked again after each round, until none of them
    # is left alive. A process that has not ended by the limit is one this process cannot end.
    began = time.monotonic()
    while living := [process for process in standing() if is_alive(process)]:
        if time.monotonic() - began > ENDING_LIMIT_SECONDS:
            raise TimeoutError(f"processes {[process.pid for process in living]} did not end")
        for process in living:
            with suppress(psutil.NoSuchProcess):
                process.kill()
        time.sleep(ENDING_WATCH_SECONDS)


def end_own_descendants() -> None:
    # Ends all this process has started, and reaps those it is the parent or the reaper of.
    end_all(lambda: descendants(os.getpid()))
    with suppress(ChildProcessError):
        while os.waitpid(-1, os.WNOHANG) != (0, 0):
            pass


def end_when_abandoned(abandoned: Callable[[], None]) -> None:
    # The process that holds this one keeps the other end of its standard input open and
    # writes nothing to it; the end closes when that process is gone.
    os.read(sys.stdin.fileno(), 1)
    try:
        end_own_descendants()
        abandoned()
    finally:
        os._exit(ABANDONED_STATUS)


def hold_descendants(abandoned: Callable[[], None]) -> None:
    # What a held program does first. It becomes the reaper of the orphans of all it starts,
    # so that they stay its descendants until it is ended; and once the process that holds it
    # is gone, it ends all it started, calls `abandoned`, and ends itself, whatever of this
    # fails. A holder that dies after it has stopped the program, to end it, has the program
    # go on, so that it finds its holder gone.
    set_process_option(PR_SET_CHILD_SUBREAPER, 1)
    set_process_option(PR_SET_PDEATHSIG, signal.SIGCONT)
    Thread(target=end_when_abandoned, args=(abandoned,), daemon=True).start()


def wait_to_be_ended() -> None:
    # A held program never ends by itself: the process that holds it stops it first, so that
    # nothing it started slips out of its descent, and then ends it.
    Event().wait()


class HeldProgram:
    # A program that is held with all it starts until they are ended: one that calls
    # hold_descendants first. It leads a session of its own, so that no signal to this
    # process's group reaches it, and its standard input is a pipe this process holds open.
    # It is watched without being reaped, so that its number stays its own; each look notes
    # the processes it has started, with their start times, so that each can be found again
    # wherever it is reparented.
    def __init__(self, command: list[str], environment: dict[str, str]) -> None:
        reading, self.holding = os.pipe()
        try:
            self.process = subprocess.Popen(
                command, stdin=reading, env=environment, start_new_session=True
            )
        except BaseException:
            os.close(self.holding)
            raise
        finally:
            os.close(reading)
        self.seen: set[psutil.Process] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.end()

    def has_ended(self) -> bool:
        return os.waitid(os.P_PID, self.process.pid, ENDED_UNREAPED) is not None

    def status(self) -> int:
        # How the program ended, once it is ended.
        return self.process.wait(timeout=ENDING_LIMIT_SECONDS)

    def look(self) -> list[psutil.Process]:
        # The program and its descendants as they stand, the descendants noted. A process
        # is the same process when its number and its start time are.
        standing = descendants(self.process.pid)
        self.seen |= set(standing)
        return [psutil.Process(self.process.pid), *standing]

    def held(self) -> list[psutil.Process]:
        # The processes the program started that are still there, wherever they are now.
        self.look()
        return [process for process in self.seen if process.is_running()]

    def end(self) -> None:
        # The program is stopped first, so that it can neither end nor let a descendant slip
        # out of its descent, and killed and reaped last, also when a descendant could not be
        # ended: stopped, it would never end. Where this process inherits the dead, as process
        # 1 of a container or a reaper of orphans does, the ended that became its children are
        # reaped by their numbers, each known to be the one that was ended, and no other child
        # of this process is touched.
        os.kill(self.process.pid, signal.SIGSTOP)
        try:
            end_all(self.held)
        finally:
            self.process.kill()
            self.status()
            os.close(self.holding)
            self.reap_ended()

    def reap_ended(self) -> None:
        # A process whose first thread has ended shows as ended while its other threads still
        # end, and can be reaped, or hands its children on, only after them: the noted processes
        # that ended are looked at again while one of them waits for that, for a while. An
        # ended process whose parent goes on is that parent's to reap.
        began = time.monotonic()
        while time.monotonic() - began < REAPING_LIMIT_SECONDS:
            waiting = False
            for process in self.seen:
                with suppress(psutil.NoSuchProcess, ChildProcessError):
                    if is_alive(process):
                        continue
                    parent = process.ppid()
                    if parent == os.getpid():
                        waiting |= os.waitpid(process.pid, os.WNOHANG) == (0, 0)
                    elif not is_alive(psutil.Process(parent)):
                        waiting = True
            if not waiting:
                return
            time.sleep(ENDING_WATCH_SECONDS)
