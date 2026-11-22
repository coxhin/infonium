import ctypes
import glob
import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Self

import psutil

# Linux's option of prctl that makes a process the reaper of the orphans among its
# descendants, so that whatever they start stays its descendant when its parent dies, a
# process that leads a session of its own as well.
PR_SET_CHILD_SUBREAPER = 36
# A look at whether a process has ended that does not wait, and leaves it unreaped.
ENDED_UNREAPED = os.WEXITED | os.WNOHANG | os.WNOWAIT
# The status a held program ends with when the process that holds it is gone.
ABANDONED_STATUS = 1
# How long the ending of a process and all it started may take, and how long passes between
# two looks at what of it is still alive.
ENDING_LIMIT_SECONDS = 10.0
ENDING_WATCH_SECONDS = 0.02
# Where Linux lists the children of each thread of a process: a walk over these lists reads
# the descendants alone, where one over every process reads all the machine runs (25 ms for
# 2,171 processes here). And where it shows the files a process holds open, and the unit of
# the blocks a file takes on disk.
TASKS = "/proc/{pid}/task"
CHILDREN_LISTS = "*/children"
DESCRIPTORS = "/proc/{pid}/fd"
EVERY_ENTRY = "*"
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


def hold_data_to(limit: int) -> None:
    # Bounds the data this process, and each process it starts after, may hold. A hard limit
    # already in force that is lower cannot be raised, and is kept.
    hard_limit = resource.getrlimit(resource.RLIMIT_DATA)[1]
    data_limit = limit if hard_limit == resource.RLIM_INFINITY else min(limit, hard_limit)
    resource.setrlimit(resource.RLIMIT_DATA, (data_limit, data_limit))


def become_reaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
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


def held_files(processes: list[psutil.Process], folder: Path) -> int:
    # What the files under a folder that processes hold open take on disk, each file once,
    # those removed from the folder while held among them, which no walk of the folder finds.
    under = os.path.join(folder, "")
    blocks: dict[tuple[int, int], int] = {}
    for process in processes:
        for descriptor in glob.glob(os.path.join(DESCRIPTORS.format(pid=process.pid), EVERY_ENTRY)):
            with suppress(*LOOK_REFUSED):
                if os.readlink(descriptor).startswith(under):
                    held = os.stat(descriptor)
                    blocks[(held.st_dev, held.st_ino)] = held.st_blocks * BLOCK_BYTES
    return sum(blocks.values())


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
    # fails.
    become_reaper()
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
        self.process = subprocess.Popen(
            command, stdin=reading, env=environment, start_new_session=True
        )
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
        for process in self.seen:
            with suppress(psutil.NoSuchProcess, ChildProcessError):
                if process.is_running() and process.ppid() == os.getpid():
                    os.waitpid(process.pid, os.WNOHANG)
