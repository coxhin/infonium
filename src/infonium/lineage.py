import ctypes
import os
import resource
import signal
import subprocess
import time
from contextlib import suppress

import psutil

# Linux's option of prctl that makes a process the reaper of the orphans among its
# descendants, so that whatever they start stays its descendant when its parent dies, a
# process that leads a session of its own as well.
PR_SET_CHILD_SUBREAPER = 36
# How long the ending of a process and all it started may take, and how long passes between
# two looks at what of it is still alive.
ENDING_LIMIT_SECONDS = 10.0
ENDING_WATCH_SECONDS = 0.02


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


def held_memory(pid: int) -> int:
    # The memory a process and all it started hold resident and share with no other: what
    # they make of their own, without the files and shared memory they map.
    held = 0
    for process in (psutil.Process(pid), *descendants(pid)):
        with suppress(psutil.NoSuchProcess):
            memory = process.memory_info()
            held += memory.rss - memory.shared
    return held


def descendants(pid: int) -> list[psutil.Process]:
    # The descendants of a process as they stand, those that have ended among them.
    return psutil.Process(pid).children(recursive=True)


def descendants_of(pid: int, found: dict[int, psutil.Process]) -> list[psutil.Process]:
    # The descendants of a process as they stand, noted in `found` with those seen before.
    standing = descendants(pid)
    found |= {process.pid: process for process in standing}
    return standing


def ended_descendants(pid: int) -> list[psutil.Process]:
    # Kills every descendant of a process until none is left alive, and gives all it found.
    # A process that has not ended by the limit is one this process cannot end.
    found: dict[int, psutil.Process] = {}
    began = time.monotonic()
    while living := [process for process in descendants_of(pid, found) if is_alive(process)]:
        if time.monotonic() - began > ENDING_LIMIT_SECONDS:
            raise TimeoutError(f"processes {[process.pid for process in living]} did not end")
        for process in living:
            with suppress(psutil.NoSuchProcess):
                process.kill()
        time.sleep(ENDING_WATCH_SECONDS)
    return list(found.values())


def end_own_descendants() -> None:
    # Ends all this process has started, and reaps those it is the parent or the reaper of.
    ended_descendants(os.getpid())
    with suppress(ChildProcessError):
        while os.waitpid(-1, os.WNOHANG) != (0, 0):
            pass


def end_process_tree(process: subprocess.Popen[bytes]) -> None:
    # Ends a process that reaps the orphans of all it started, together with all of them. It
    # is stopped first, so that it can neither end nor let a descendant slip out of its tree,
    # and ended and reaped last, also when a descendant could not be ended: stopped, it would
    # never end. Where this process inherits the dead, as process 1 of a container or a
    # reaper of orphans does, the ended become its children when that process is gone; they
    # are reaped by their numbers, each known to be the one that was ended, and no other child
    # of this process is touched.
    os.kill(process.pid, signal.SIGSTOP)
    try:
        ended = ended_descendants(process.pid)
    finally:
        process.kill()
        process.wait(timeout=ENDING_LIMIT_SECONDS)
    for descendant in ended:
        with suppress(psutil.NoSuchProcess, ChildProcessError):
            if descendant.is_running() and descendant.ppid() == os.getpid():
                os.waitpid(descendant.pid, os.WNOHANG)
