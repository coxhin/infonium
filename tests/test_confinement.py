from enum import StrEnum

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.confinement import BrowserProcess, runs_pages_sandboxed

BROWSER_PID = 100
FIRST_PAGE_PID = 200
ABSENT_PID = 1
BROWSER_NAMESPACES = ["user:[4026531837]", "pid:[4026531836]", "net:[4026531840]"]


class PageFault(StrEnum):
    # How a process that runs pages may stand outside the browser's sandbox, if it does.
    SANDBOXED = "none"
    ORPHANED = "its parents lead to no browser"
    NO_FILTERS = "Linux shows it no seccomp filters"
    NO_MORE_FILTERS = "it runs under no more filters than the browser"
    SHARED_NAMESPACE = "it stands in a namespace of the browser's"


class ProcessTree(BaseModel):
    # The processes of a rendering, as made up for a test, and the fault of each page process.
    processes: dict[int, BrowserProcess]
    browser_filters: int | None
    faults: list[PageFault]


SANDBOXED_PAGES = st.lists(st.just(PageFault.SANDBOXED), max_size=3)
OUTSIDE_SANDBOX = st.sampled_from([fault for fault in PageFault if fault != PageFault.SANDBOXED])


@st.composite
def process_trees(
    draw: st.DrawFn,
    page_faults: st.SearchStrategy[list[PageFault]],
    browser_filters: st.SearchStrategy[int | None],
) -> ProcessTree:
    shown_filters = draw(browser_filters)
    processes = {
        BROWSER_PID: BrowserProcess(
            parent=ABSENT_PID,
            runs_pages=False,
            typed=False,
            filters=shown_filters,
            namespaces=BROWSER_NAMESPACES,
        )
    }
    # The browser's helpers, which carry a type: zygotes, each started by the one before.
    helpers = draw(st.integers(0, 3))
    for number in range(helpers):
        processes[BROWSER_PID + 1 + number] = BrowserProcess(
            parent=BROWSER_PID + number,
            runs_pages=False,
            typed=True,
            filters=draw(st.none() | st.integers(0, 6)),
            namespaces=draw(st.lists(st.sampled_from(BROWSER_NAMESPACES), max_size=3)),
        )
    faults = draw(page_faults)
    least = 0 if shown_filters is None else shown_filters
    for number, fault in enumerate(faults):
        own = [f"{kind}:[{FIRST_PAGE_PID + number}]" for kind in ("user", "pid", "net")]
        shared = draw(st.lists(st.sampled_from(BROWSER_NAMESPACES), min_size=1, max_size=3))
        processes[FIRST_PAGE_PID + number] = BrowserProcess(
            parent=ABSENT_PID + 1
            if fault == PageFault.ORPHANED
            else draw(st.integers(BROWSER_PID, BROWSER_PID + helpers)),
            runs_pages=True,
            typed=True,
            filters=None
            if fault == PageFault.NO_FILTERS
            else draw(st.integers(0, least))
            if fault == PageFault.NO_MORE_FILTERS
            else least + draw(st.integers(1, 3)),
            namespaces=own[len(shared) :] + shared if fault == PageFault.SHARED_NAMESPACE else own,
        )
    return ProcessTree(processes=processes, browser_filters=shown_filters, faults=faults)


@given(
    tree=process_trees(
        st.lists(st.just(PageFault.SANDBOXED), min_size=1, max_size=4), st.integers(0, 3)
    )
)
def test_pages_run_sandboxed_when_every_page_process_does(tree: ProcessTree) -> None:
    assert runs_pages_sandboxed(tree.processes), (
        "a browser whose every page process runs under more seccomp filters than its own "
        "process, in none of its namespaces, was judged to run pages outside its sandbox"
    )


@given(
    tree=process_trees(
        st.tuples(SANDBOXED_PAGES, OUTSIDE_SANDBOX, SANDBOXED_PAGES).map(
            lambda parts: [*parts[0], parts[1], *parts[2]]
        ),
        st.integers(0, 3),
    )
)
def test_one_page_process_outside_the_sandbox_is_enough(tree: ProcessTree) -> None:
    [fault] = [fault for fault in tree.faults if fault != PageFault.SANDBOXED]
    assert not runs_pages_sandboxed(tree.processes), (
        f"a browser was judged to run pages in its sandbox, where for one page process {fault}"
    )


@given(
    tree=st.one_of(
        process_trees(st.lists(st.sampled_from(PageFault), max_size=4), st.none()),
        process_trees(st.just([]), st.none() | st.integers(0, 3)),
    )
)
def test_a_browser_without_filters_shown_or_without_pages_is_not_sandboxed(
    tree: ProcessTree,
) -> None:
    assert not runs_pages_sandboxed(tree.processes), (
        "a browser was judged to run pages in its sandbox where Linux showed no filters for its "
        "own process, or where no process ran pages"
    )
