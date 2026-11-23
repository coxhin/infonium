import json
import sys
from pathlib import Path
from typing import Any

import pypdfium2
import pytest

from infonium import pdf
from infonium.document import Extracted
from infonium.mineru import UnparsedFile

TESTS = Path(__file__).resolve().parent
PDF_DATA = TESTS / "fixtures" / "pdf"
STANDIN = TESTS / "pdf_engine_standin.py"
# The samples were drawn for infonium; the reference is what commit 6facef0 read from them,
# which a fresh run of the engine gave again on 2026-11-21. It is what was read then, not what
# is right: in turned_digital a heading reads "accou nt", a known limit of turned pages.
SAMPLES = sorted(sample.stem for sample in (PDF_DATA / "samples").glob("*.pdf"))
REFERENCE = json.loads((PDF_DATA / "handed.json").read_text("utf-8"))["made_by"]


def standin_engine(folder: Path, sample: str) -> Path:
    engine = folder / "mineru"
    engine.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STANDIN}" {sample} "$@"\n')
    engine.chmod(0o755)
    return engine


def reading_of(sample: str, engine: Path, folder: Path) -> Extracted:
    try:
        return pdf.extract_pdf(PDF_DATA / "samples" / f"{sample}.pdf", sample, engine, folder)
    except UnparsedFile as refused:
        pytest.fail(
            f"the stored engine results of {sample} do not apply, so it cannot be judged "
            f"without the engine (run the tests marked mineru): {refused}"
        )


def page_count(sample: str) -> int:
    document = pypdfium2.PdfDocument(PDF_DATA / "samples" / f"{sample}.pdf")
    try:
        return len(document)
    finally:
        document.close()


def as_stored(extracted: Extracted) -> dict[str, Any]:
    return {
        "title": extracted.title,
        "blocks": [block.model_dump(mode="json") for block in extracted.blocks],
    }


def differences(expected: dict[str, Any], read: dict[str, Any]) -> str:
    if len(expected["blocks"]) != len(read["blocks"]):
        return f"{len(expected['blocks'])} blocks became {len(read['blocks'])}"
    changed = [
        number
        for number, (before, after) in enumerate(
            zip(expected["blocks"], read["blocks"], strict=True)
        )
        if before != after
    ]
    title = "" if expected["title"] == read["title"] else f"the title became {read['title']!r}; "
    return f"{title}blocks {changed} differ"


@pytest.mark.parametrize("sample", SAMPLES)
def test_a_pdf_reads_as_the_reference_from_the_engines_stored_results(
    sample: str, tmp_path: Path
) -> None:
    extracted = reading_of(sample, standin_engine(tmp_path, sample), tmp_path)
    pages = page_count(sample)
    unplaced = [number for number, block in enumerate(extracted.blocks) if not block.places]
    off_pages = {place.page for block in extracted.blocks for place in block.places} - set(
        range(pages)
    )
    assert not unplaced, f"blocks {unplaced} of {sample} were read without a place in the file"
    assert not off_pages, f"{sample} has {pages} pages, and places were put on pages {off_pages}"
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample} is read otherwise than {REFERENCE} read it: {differences(expected, read)}"
    )


@pytest.mark.mineru
@pytest.mark.parametrize("sample", ["trap", "marks_digital", "turned_digital"])
def test_a_pdf_reads_as_the_reference_with_the_engine(
    sample: str, pdf_engine: Path, tmp_path: Path
) -> None:
    extracted = pdf.extract_pdf(
        PDF_DATA / "samples" / f"{sample}.pdf", sample, pdf_engine, tmp_path
    )
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample} is read by the engine otherwise than {REFERENCE} read it: "
        f"{differences(expected, read)}"
    )
