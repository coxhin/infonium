from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import httpx2
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from infonium.folder import FOLDER_NAME_LENGTH
from tests.feed_standin import FeedStandin
from tests.foldering import (
    NOWHERE_FOLDER,
    arranged_as,
    dissolve_folder,
    folders_listed,
    keep_web_item,
    kept_names,
    library_arranged,
    link_origin,
    long_names,
    made_folder,
    make_folder,
    paddings,
    place,
    placed,
    rename_folder,
    shown_now,
    subscribed_origin,
    subscription_of,
    unchanged,
)
from tests.notebook import instant, refusal
from tests.service import App, as_owner, new_owner

# The judgments of S1 to S4 and S10 of the service's brief of the sidebar, through the service's
# endpoints: folders listed, made, renamed and dissolved, and each owner's own.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
FEW_EXAMPLES = settings(max_examples=8, suppress_health_check=[HealthCheck.function_scoped_fixture])
# The most folders a drawn history may reach for by its place among those standing.
MOST_PLACES = 8
# Names that few characters make, so that drawn names often are another folder's: two letters
# that differ only in case, and two characters of a script without case, none of which an
# English sentence holds.
CLASHING_CHARACTERS = "Ωω论文"
clashing_names = st.text(alphabet=CLASHING_CHARACTERS, min_size=1, max_size=2)
# Names that hold no character, or more than a folder's name may, once trimmed.
refused_names = st.one_of(
    paddings,
    st.builds(lambda before, name, after: before + name + after, paddings, long_names, paddings),
)


class Step(StrEnum):
    MAKE = "make"
    RENAME = "rename"
    DISSOLVE = "dissolve"


def numbered(name: str, number: int) -> str:
    # A drawn name made the only one of its history by its step's number, and kept short
    # enough to be a folder's name.
    return f"{name[: FOLDER_NAME_LENGTH - 4]}#{number:03d}"


def detail_of(response: httpx2.Response) -> str:
    return str(response.json().get("detail", ""))


# S1: folders listed.


histories = st.lists(
    st.one_of(
        st.tuples(st.just(Step.MAKE), kept_names),
        st.tuples(st.just(Step.RENAME), st.integers(0, MOST_PLACES - 1), kept_names),
        st.tuples(st.just(Step.DISSOLVE), st.integers(0, MOST_PLACES - 1)),
    ),
    max_size=10,
)


@given(history=histories)
@example(history=[(Step.MAKE, "b"), (Step.MAKE, "a"), (Step.RENAME, 1, "c"), (Step.MAKE, "0")])
@EXAMPLES
def test_the_folders_standing_are_listed_the_one_made_first_first_each_as_last_answered(
    app: App, history: list[tuple[Any, ...]]
) -> None:
    """S1: after each step of a drawn history of folders made, renamed and dissolved under
    names no other folder has, the owner's folders are listed as those standing, the one
    made first first, each as last answered."""
    with as_owner(new_owner()):
        assert folders_listed(app) == [], "S1: an owner who made no folder has folders listed"
        standing: list[dict[str, Any]] = []
        for number, step in enumerate(history):
            match step:
                case (Step.MAKE, name):
                    standing.append(made_folder(app, numbered(name, number)))
                case (Step.RENAME, place_among, name) if standing:
                    at = place_among % len(standing)
                    response = rename_folder(app, standing[at]["id"], numbered(name, number))
                    assert response.status_code == HTTPStatus.OK, (
                        f"S3: renaming a folder to a name no other folder has was refused: "
                        f"{response.status_code} {response.text[:300]}"
                    )
                    standing[at] = response.json()
                case (Step.DISSOLVE, place_among) if standing:
                    gone = standing.pop(place_among % len(standing))
                    assert dissolve_folder(app, gone["id"]).status_code == HTTPStatus.NO_CONTENT, (
                        "S4: dissolving a folder of the owner's was refused"
                    )
                case _:
                    continue
            assert folders_listed(app) == standing, (
                "S1: the folders listed are not those standing, the one made first first, each as "
                f"it was last answered, after {step[0]}"
            )


# S2: a folder made.


@given(name=kept_names, before=paddings, after=paddings)
@example(name="论" * FOLDER_NAME_LENGTH, before=" ", after="\N{IDEOGRAPHIC SPACE}")
@example(name="\N{GRINNING FACE}" * FOLDER_NAME_LENGTH, before="", after="\n")
@example(name="a", before="", after="")
@example(name="a \t b", before="\N{EM SPACE}", after="\xa0")
@EXAMPLES
def test_a_folder_is_made_with_its_name_trimmed_where_that_holds_1_to_50_characters(
    app: App, name: str, before: str, after: str
) -> None:
    """S2: a name of 1 to 50 characters that neither begins nor ends with white space, sent
    with up to three characters of white space on each side, makes a folder answered with
    that name, made between the request and its answer, and listed as it was answered."""
    with as_owner(new_owner()):
        began = datetime.now(UTC)
        response = make_folder(app, f"{before}{name}{after}")
        ended = datetime.now(UTC)
        assert response.status_code == HTTPStatus.OK, (
            f"S2: a folder whose name holds {len(name)} characters once trimmed was refused: "
            f"{response.status_code} {response.text[:300]}"
        )
        view = response.json()
        assert view["name"] == name, (
            "S2: a folder's name is not kept trimmed of the white space around it: "
            f"{view['name']!r}"
        )
        assert began <= instant(view["added"]) <= ended, (
            f"S2: a folder made does not tell when it was made: {view['added']}"
        )
        assert folders_listed(app) == [view], (
            "S1, S2: a folder made is not listed as it was answered"
        )


@given(name=refused_names)
@example(name="")
@example(name="\N{IDEOGRAPHIC SPACE} \t")
@example(name="a" * (FOLDER_NAME_LENGTH + 1))
@EXAMPLES
def test_a_name_of_no_character_or_more_than_50_once_trimmed_is_refused_and_nothing_kept(
    app: App, name: str
) -> None:
    """S2: a name of white space alone, or of 51 to 80 characters once trimmed, is refused with
    422, and the owner's folders stay as they were."""
    with as_owner(new_owner()):
        kept = made_folder(app, "Kept")
        response = make_folder(app, name)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
            f"S2: a folder whose name holds {len(name.strip())} characters once trimmed was not "
            f"refused by validation: {response.status_code}"
        )
        assert folders_listed(app) == [kept], "S2: a folder refused by validation was kept"


makes_and_dissolves = st.lists(
    st.one_of(
        st.tuples(st.just(Step.MAKE), clashing_names, paddings, paddings),
        st.tuples(st.just(Step.DISSOLVE), st.integers(0, MOST_PLACES - 1)),
    ),
    min_size=1,
    max_size=12,
)


@given(history=makes_and_dissolves)
@example(
    history=[
        (Step.MAKE, "Ω", "", ""),
        (Step.MAKE, "ω", "", ""),
        (Step.MAKE, "Ω", " ", "\N{IDEOGRAPHIC SPACE}"),
        (Step.DISSOLVE, 0),
        (Step.MAKE, "Ω", "", ""),
    ]
)
@EXAMPLES
def test_a_name_is_refused_as_taken_exactly_where_another_folder_of_the_owners_has_it(
    app: App, history: list[tuple[Any, ...]]
) -> None:
    """S2: in a drawn history of makes and dissolves under names of up to two of four
    characters, a name is refused with 409 and a reason that holds it where a folder
    standing has it once trimmed, and is kept otherwise; the folders listed are those kept
    and not dissolved."""
    with as_owner(new_owner()):
        standing: list[dict[str, Any]] = []
        for step in history:
            match step:
                case (Step.MAKE, name, before, after):
                    response = make_folder(app, f"{before}{name}{after}")
                    if any(folder["name"] == name for folder in standing):
                        assert response.status_code == HTTPStatus.CONFLICT, (
                            f"S2: a name another folder of the owner's has was not refused as "
                            f"taken: {response.status_code}"
                        )
                        assert name in detail_of(response), (
                            f"S2: the refusal of a name taken does not name it: {response.text}"
                        )
                    else:
                        assert response.status_code == HTTPStatus.OK, (
                            f"S2: a name no other folder of the owner's has was refused: "
                            f"{response.status_code} {response.text[:300]}"
                        )
                        standing.append(response.json())
                case (Step.DISSOLVE, place_among) if standing:
                    gone = standing.pop(place_among % len(standing))
                    assert dissolve_folder(app, gone["id"]).status_code == HTTPStatus.NO_CONTENT, (
                        "S4: dissolving a folder of the owner's was refused"
                    )
                case _:
                    continue
            assert folders_listed(app) == standing, (
                f"S2: the folders are not those made and not refused, after {step[0]}"
            )


# S3: a folder renamed.


@given(
    names=st.lists(clashing_names, min_size=1, max_size=4, unique=True),
    renames=st.lists(
        st.tuples(st.integers(0, MOST_PLACES - 1), clashing_names, paddings, paddings),
        min_size=1,
        max_size=8,
    ),
)
@example(names=["Ω", "ω"], renames=[(0, "ω", "", ""), (0, "Ω", " ", " "), (1, "论", "", "")])
@EXAMPLES
def test_a_rename_is_refused_as_taken_exactly_where_another_folder_has_the_name_and_else_taken(
    app: App, names: list[str], renames: list[tuple[int, str, str, str]]
) -> None:
    """S3: a rename to a name that another folder of the owner's has once trimmed is refused
    with 409 and a reason that holds the name; any other is answered with the folder under
    the name trimmed, its id and when it was made as before, and the folders listed follow,
    each in its place."""
    with as_owner(new_owner()):
        standing = [made_folder(app, name) for name in names]
        for place_among, name, before, after in renames:
            at = place_among % len(standing)
            folder = standing[at]
            response = rename_folder(app, folder["id"], f"{before}{name}{after}")
            if any(other["name"] == name for other in standing if other["id"] != folder["id"]):
                assert response.status_code == HTTPStatus.CONFLICT, (
                    f"S3: renaming a folder to a name another folder of the owner's has was not "
                    f"refused as taken: {response.status_code}"
                )
                assert name in detail_of(response), (
                    f"S3: the refusal of a name taken does not name it: {response.text}"
                )
            else:
                assert response.status_code == HTTPStatus.OK, (
                    f"S3: renaming a folder to a name no other folder of the owner's has was "
                    f"refused: {response.status_code} {response.text[:300]}"
                )
                assert response.json() == {**folder, "name": name}, (
                    "S3: a folder renamed does not take the name trimmed, keeping its id and when "
                    f"it was made: {response.json()} from {folder}"
                )
                standing[at] = response.json()
            assert folders_listed(app) == standing, (
                "S1, S3: after a rename the folders are not as renamed, each in its place"
            )


@given(before=paddings, after=paddings)
@example(before="", after="")
@EXAMPLES
def test_a_folder_renamed_to_its_own_name_is_answered_unchanged(
    app: App, before: str, after: str
) -> None:
    """S3: a rename to the folder's own name, with white space around it, is answered with the
    folder as it was, and the folders listed stay as they were."""
    with as_owner(new_owner()):
        folder = made_folder(app, "论文 Ω")
        other = made_folder(app, "Other")
        response = rename_folder(app, folder["id"], f"{before}论文 Ω{after}")
        assert response.status_code == HTTPStatus.OK, (
            f"S3: renaming a folder to its own name was refused: {response.status_code} "
            f"{response.text[:300]}"
        )
        assert response.json() == folder, (
            f"S3: a folder renamed to its own name was not answered unchanged: {response.json()}"
        )
        assert folders_listed(app) == [folder, other], (
            "S3: renaming a folder to its own name changed the folders"
        )


@given(name=refused_names)
@example(name="")
@example(name="a" * (FOLDER_NAME_LENGTH + 1))
@EXAMPLES
def test_a_rename_to_a_name_of_no_character_or_more_than_50_once_trimmed_changes_nothing(
    app: App, name: str
) -> None:
    """S3: a rename to a name of white space alone, or of 51 to 80 characters once trimmed, is
    refused with 422, and the folder stays as it was."""
    with as_owner(new_owner()):
        folder = made_folder(app, "Kept")
        response = rename_folder(app, folder["id"], name)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
            f"S3: renaming a folder to a name of {len(name.strip())} characters once trimmed was "
            f"not refused by validation: {response.status_code}"
        )
        assert folders_listed(app) == [folder], "S3: a rename refused by validation changed it"


def test_renaming_or_dissolving_a_folder_the_owner_does_not_have_is_answered_404(
    app: App,
) -> None:
    """S3, S4: renaming or dissolving a folder never made, or one dissolved, is answered 404,
    and the owner's folders stay as they were."""
    with as_owner(new_owner()):
        kept = made_folder(app, "Kept")
        gone = made_folder(app, "Gone")
        assert dissolve_folder(app, gone["id"]).status_code == HTTPStatus.NO_CONTENT, (
            "S4: dissolving a folder of the owner's was refused"
        )
        for folder, what in ((NOWHERE_FOLDER, "never made"), (gone["id"], "dissolved")):
            renamed = rename_folder(app, folder, "Anything")
            assert renamed.status_code == HTTPStatus.NOT_FOUND, (
                f"S3: renaming a folder {what} was answered {renamed.status_code}, not 404"
            )
            dissolved = dissolve_folder(app, folder)
            assert dissolved.status_code == HTTPStatus.NOT_FOUND, (
                f"S4: dissolving a folder {what} was answered {dissolved.status_code}, not 404"
            )
        assert folders_listed(app) == [kept], (
            "S3, S4: renaming or dissolving a folder the owner does not have changed the folders"
        )


# S4: a folder dissolved.


FOLDERS_MADE = 3
# What the subscriptions of a drawn library hold: as many items that follow each as each number.
FOLLOWING = [1, 1]
HAND_ADDED_KEPT = 3
folders_drawn = st.none() | st.integers(0, FOLDERS_MADE - 1)


@given(
    beginning=st.lists(
        folders_drawn,
        min_size=len(FOLLOWING) + HAND_ADDED_KEPT,
        max_size=len(FOLLOWING) + HAND_ADDED_KEPT,
    ),
    dissolved=st.integers(0, FOLDERS_MADE - 1),
)
@example(beginning=[0, 1, 0, None, 0], dissolved=0)
@FEW_EXAMPLES
def test_a_dissolved_folder_is_gone_and_what_it_held_stays_in_no_folder(
    subscribing_app: App, feeds: FeedStandin, beginning: list[int | None], dissolved: int
) -> None:
    """S4: dissolving a folder of a drawn library is answered 204 and leaves the folder
    unlisted, everything it held in no folder, the items that follow a subscription in it
    among them, and everything else where it was; dissolving it again is answered 404 and
    moves nothing."""
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, beginning)
        folder = arrangement.folders[dissolved]
        response = dissolve_folder(app, folder)
        assert response.status_code == HTTPStatus.NO_CONTENT, (
            f"S4: dissolving a folder of the owner's was answered {response.status_code}"
        )
        assert shown_now(app) == arrangement.dissolving(folder).shown(), (
            "S4: once a folder is dissolved, it is still listed, or what it held is not in no "
            "folder, or something it did not hold moved, or a subscription or an item is gone"
        )
        before = shown_now(app)
        again = dissolve_folder(app, folder)
        assert again.status_code == HTTPStatus.NOT_FOUND, (
            f"S4: dissolving a folder dissolved already was answered {again.status_code}, not 404"
        )
        unchanged(app, before, "S4: dissolving a folder dissolved already")


def test_a_folder_made_with_a_dissolved_ones_name_holds_nothing_of_what_that_one_held(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    """S4: once a folder that holds a subscription, its item and a hand-added item is
    dissolved, a folder made with its name holds none of them, and all three are in no
    folder."""
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        folder = made_folder(app, "论文")["id"]
        subscription = subscription_of(app, feeds)
        following = keep_web_item(app.store, owner, subscribed_origin(subscription))
        hand_added = keep_web_item(app.store, owner, link_origin())
        placed(app, folder, [subscription], [hand_added])
        assert dissolve_folder(app, folder).status_code == HTTPStatus.NO_CONTENT, (
            "S4: dissolving a folder of the owner's was refused"
        )
        anew = made_folder(app, "论文")["id"]
        shown = shown_now(app)
        assert (shown.folders, shown.subscriptions, shown.items) == (
            [anew],
            {subscription: None},
            {following: None, hand_added: None},
        ), (
            "S4: a folder made with the name of one dissolved holds what that one held, or what "
            f"that one held is not in no folder: {shown}"
        )


# S10: owners apart.


def test_another_owners_folders_and_placements_are_neither_shown_nor_found(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    """S10: another owner lists none of an owner's folders, is shown only their own folders and
    placements, may make a folder of a name the owner uses, and is answered 404, as for an
    id nobody has, when renaming, dissolving or placing in the owner's folders, or placing
    the owner's subscription, hand-added item or item that follows its subscription; the
    owner's folders and placements stay as they were."""
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        folders = [made_folder(app, name)["id"] for name in ("论文", "Podcasts")]
        subscription = subscription_of(app, feeds)
        following = keep_web_item(app.store, owner, subscribed_origin(subscription))
        hand_added = keep_web_item(app.store, owner, link_origin())
        placed(app, folders[0], [subscription], [])
        placed(app, folders[1], [], [hand_added])
        theirs = shown_now(app)
    other = new_owner()
    with as_owner(other):
        assert folders_listed(app) == [], "S10: another owner's folders are listed"
        own_folder = made_folder(app, "论文")["id"]
        own_subscription = subscription_of(app, feeds)
        own_item = keep_web_item(app.store, other, link_origin())
        placed(app, own_folder, [own_subscription], [own_item])
        named: dict[str, Callable[[str], httpx2.Response]] = {
            "renaming a folder": lambda folder: rename_folder(app, folder, "Taken over"),
            "dissolving a folder": lambda folder: dissolve_folder(app, folder),
            "placing in a folder": lambda folder: place(app, folder, [], [own_item]),
        }
        asked = [(what, folder, ask) for what, ask in named.items() for folder in folders]
        asked += [
            ("placing a subscription", subscription, lambda found: place(app, None, [found], [])),
            ("placing an item", hand_added, lambda found: place(app, None, [], [found])),
            (
                "placing an item that follows its subscription",
                following,
                lambda found: place(app, own_folder, [], [found]),
            ),
        ]
        for what, found, ask in asked:
            of_another = refusal(ask(found), found)
            of_none = refusal(ask(NOWHERE_FOLDER), NOWHERE_FOLDER)
            assert of_another[0] == HTTPStatus.NOT_FOUND, (
                f"S10: {what} of another owner's was answered {of_another[0]}, not 404"
            )
            assert of_another == of_none, (
                f"S10: {what} of another owner's is told apart from one that is not there: "
                f"{of_another} against {of_none}"
            )
        shown = shown_now(app)
        assert (shown.folders, shown.subscriptions, shown.items) == (
            [own_folder],
            {own_subscription: own_folder},
            {own_item: own_folder},
        ), f"S10: an owner is shown another owner's folders or placements: {shown}"
    with as_owner(owner):
        assert shown_now(app) == theirs, (
            "S10: another owner's placings, renamings and dissolvings changed an owner's folders "
            "or where their things are"
        )
