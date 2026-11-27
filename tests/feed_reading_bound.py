import json
import resource
import sys
import time
from pathlib import Path

from infonium.subscriptions.feed import read_feed

# Reads the feed in the file given, at the address given, in a process of its own whose memory
# is held to the bytes given, so that a reading that runs away ends this process and not the
# tests'. Its last line says how long the reading took and the most memory the process held,
# whether the reading gave a feed or failed.
SECONDS = "seconds"
PEAK_BYTES = "peak_bytes"
DECLARED = "application/rss+xml"
# The most memory the process has held, as Linux counts it from the program's start: unlike
# getrusage's, which keeps what the process that started it held before it.
STATUS = Path("/proc/self/status")
HIGH_WATER = "VmHWM:"


def peak_bytes() -> int:
    [line] = [line for line in STATUS.read_text().splitlines() if line.startswith(HIGH_WATER)]
    return int(line.split()[1]) * 1024


def main() -> None:
    sample, address, most_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
    resource.setrlimit(resource.RLIMIT_AS, (most_bytes, most_bytes))
    content = Path(sample).read_bytes()
    began = time.monotonic()
    try:
        read_feed(content, DECLARED, address)
    finally:
        print(json.dumps({SECONDS: time.monotonic() - began, PEAK_BYTES: peak_bytes()}), flush=True)


if __name__ == "__main__":
    main()
