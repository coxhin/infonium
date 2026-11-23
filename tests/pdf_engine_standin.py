import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

# Stands in for MinerU while the PDF reader is tested: called as the engine is, with the name
# of a sample in front, it writes what the engine wrote for that sample when the reference
# was made, but only when it is handed the file the reference handed the engine, with the
# same options. Two files are the same when their bytes are, but for the identifier in the
# trailer, which PDFium draws anew each time it saves a file. Otherwise it says why it
# refuses and leaves, and the reader takes that line for the engine's reason.
# A sample may be named with the results to write in place of the stored ones, after a colon:
# the engine's results for that sample as a test changed them.
# usage: pdf_engine_standin.py SAMPLE[:RESULTS] -p FILE -o FOLDER OPTION...
PDF_DATA = Path(__file__).resolve().parent / "fixtures" / "pdf"
TRAILER_REACH = 4096
IDENTIFIER = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")
RESULTS = {"middle": "{stem}_middle.json", "model": "{stem}_model.json"}
OUTPUT_FOLDER = "hybrid_auto"
REFUSED = 3
RESULTS_SEPARATOR = ":"


def unnamed_digest(file: Path) -> str:
    content = file.read_bytes()
    body, tail = content[:-TRAILER_REACH], content[-TRAILER_REACH:]
    found = [*IDENTIFIER.finditer(tail)]
    if found:
        tail = tail[: found[-1].start()] + tail[found[-1].end() :]
    return hashlib.sha256(body + tail).hexdigest()


def main() -> int:
    named, *arguments = sys.argv[1:]
    sample, _, changed = named.partition(RESULTS_SEPARATOR)
    at_file, at_folder = arguments.index("-p"), arguments.index("-o")
    file, folder = Path(arguments[at_file + 1]), Path(arguments[at_folder + 1])
    placed = {at_file, at_file + 1, at_folder, at_folder + 1}
    options = [argument for at, argument in enumerate(arguments) if at not in placed]
    handed = json.loads((PDF_DATA / "handed.json").read_text("utf-8"))
    if options != handed["options"]:
        called = handed["options"]
        print(f"the engine is called with {options}, and the reference called it with {called}")
        return REFUSED
    if unnamed_digest(file) != handed["samples"][sample]:
        print(f"the file handed to the engine for {sample} is not the one the reference handed it")
        return REFUSED
    stored = Path(changed) if changed else PDF_DATA / "engine" / f"{sample}.json.gz"
    written = json.loads(gzip.decompress(stored.read_bytes()))
    results = folder / file.stem / OUTPUT_FOLDER
    results.mkdir(parents=True)
    for kind, result in written.items():
        (results / RESULTS[kind].format(stem=file.stem)).write_text(json.dumps(result), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
