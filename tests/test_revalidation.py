from http import HTTPStatus

from infonium.revalidation import ASK_BEFORE_USE
from tests.pdf_engine_standin import PDF_DATA
from tests.service import App, article

# A version that names no answer the service gave.
NO_SUCH_VERSION = 'W/"0"'
COMPRESSED = "gzip"


def answers_of(item_id: str) -> list[str]:
    # The addresses a reader that shows a read web page asks for, but its pictures, which are
    # kept a year without asking.
    return [
        "/api/items",
        f"/api/items/{item_id}",
        f"/api/items/{item_id}/document",
        f"/api/items/{item_id}/revisions",
        f"/api/items/{item_id}/highlights",
        f"/api/items/{item_id}/source",
        f"/api/items/{item_id}/page",
    ]


def test_an_answer_the_browser_holds_is_not_sent_again(app: App) -> None:
    item_id = app.read("/reader")
    for path in answers_of(item_id):
        given = app.client.get(path)
        assert given.status_code == HTTPStatus.OK and "etag" in given.headers, (
            f"{path} was answered without the version of its answer: {given.status_code}"
        )
        assert given.headers["cache-control"] == ASK_BEFORE_USE, (
            f"{path} may be kept without asking whether it changed: "
            f"{given.headers.get('cache-control')}"
        )
        version = given.headers["etag"]
        again = app.client.get(path, headers={"If-None-Match": f'W/"other", {version}'})
        assert (again.status_code, again.content) == (HTTPStatus.NOT_MODIFIED, b""), (
            f"{path} was sent again to a browser that holds its current version"
        )
        assert again.headers.get("etag") == version, f"the 304 of {path} names another version"
        other = app.client.get(path, headers={"If-None-Match": NO_SUCH_VERSION})
        assert (other.status_code, other.content) == (HTTPStatus.OK, given.content), (
            f"{path} was not sent whole to a browser that holds another version of it"
        )


def test_an_answer_that_changed_is_sent_whole_under_another_version(app: App) -> None:
    app.read("/reader")
    before = app.client.get("/api/items")
    app.read_page("second", article("Second fixture"))
    after = app.client.get("/api/items", headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == HTTPStatus.OK and len(after.json()) == len(before.json()) + 1, (
        f"the library's items were not sent again once they changed: {after.status_code}"
    )
    assert after.headers["etag"] != before.headers["etag"], "a changed answer kept its version"


def test_a_part_of_a_source_the_browser_holds_is_not_sent_again(app: App) -> None:
    path = f"/api/items/{app.read('/reader')}/source"
    version = app.client.get(path).headers["etag"]
    part = app.client.get(path, headers={"Range": "bytes=10-19", "If-None-Match": version})
    assert (part.status_code, part.content) == (HTTPStatus.NOT_MODIFIED, b""), (
        "a part of a source was sent to a browser that holds the whole of it"
    )


def test_a_304_ends_its_answer_on_a_connection_that_stays_open(app: App) -> None:
    path = f"/api/items/{app.read('/reader')}/document"
    with app.served() as reader:
        given = reader.get(path)
        again = reader.get(path, headers={"If-None-Match": given.headers["etag"]})
        after = reader.get(path)
    assert (again.status_code, again.content) == (HTTPStatus.NOT_MODIFIED, b""), (
        "a served document was sent again to a browser that holds it"
    )
    assert after.json() == given.json(), "the answer after a 304 is not the document"


def test_a_document_is_sent_compressed_and_a_pdf_as_it_is(app: App) -> None:
    pdf = (PDF_DATA / "samples" / "trap.pdf").read_bytes()
    item_id = app.settled(app.upload("trap.pdf", pdf).json()["id"])["id"]
    asked = {"Accept-Encoding": COMPRESSED}
    document = app.client.get(f"/api/items/{item_id}/document", headers=asked)
    assert document.headers.get("content-encoding") == COMPRESSED, (
        "a document was sent uncompressed to a browser that takes it compressed"
    )
    source = app.client.get(f"/api/items/{item_id}/source", headers=asked)
    assert "content-encoding" not in source.headers and source.content == pdf, (
        "a PDF was sent other than as its bytes are, which its viewer cannot fetch in parts"
    )
