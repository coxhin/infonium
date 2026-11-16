import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import trafilatura
from pydantic import BaseModel

from infonium.document import LITTLE_TEXT_WORDS, WORD_GAP

# A guard is a service that stands before a site and answers a program with a page of its own
# in place of the one asked for: a check for a person, or a notice that the reader is held
# off. What follows is what infonium knows of guards, each sign seen on such pages.

# Headers a guard sets on its check, and on no page it lets through: AWS WAF's on its
# challenge, and that of Alibaba's guard on its "punish" page.
GUARD_HEADERS = ("x-amzn-waf-action", "bxpunish")
# Words that the address of a guard's check holds, as the address of Tencent's check of WeChat
# pages and that of Baidu's check do.
CHECK_ADDRESS_WORDS = ("captcha",)
# Titles that guards give their checks: PerimeterX's, Reddit's, Anubis's and Fastly's. A
# title may join one to the site's name, with a separator between.
CHECK_TITLES = frozenset(
    {"robot or human?", "prove your humanity", "making sure you're not a bot!", "client challenge"}
)
TITLE_SEPARATOR = re.compile(r"\s+[-|]\s+")
TITLE_PATH = "//head/title"
# Elements that only a guard's check holds: Anubis's challenge and PerimeterX's.
CHECK_ELEMENTS = "//*[@id='anubis_challenge' or @id='px-captcha']"


class ServedPage(BaseModel):
    # A page as it came from an address: the address asked for, the address that served it,
    # its HTML, and how many words the extractor found in it.
    asked: str
    served: str
    content: bytes
    words: int


def is_guarded(headers: Mapping[str, str]) -> bool:
    return any(header in headers for header in GUARD_HEADERS)


def address_part(address: str) -> str:
    # The host and the path of an address, where a check is named; the query is left out, as
    # it may carry the address that was asked for.
    parts = urlsplit(address)
    return f"{parts.netloc}{parts.path}".casefold()


def names_check(asked: str, served: str) -> bool:
    # An address that leads to a check names it where the address asked for does not, so a
    # page about checks that is asked for by its own address is not taken for one.
    return any(
        word in address_part(served) and word not in address_part(asked)
        for word in CHECK_ADDRESS_WORDS
    )


def has_check_title(title: str) -> bool:
    parts = TITLE_SEPARATOR.split(WORD_GAP.join(title.split()))
    return any(part.casefold() in CHECK_TITLES for part in parts)


def is_check_page(page: ServedPage) -> bool:
    # A page with the text of an article is the article, whatever else it holds. A page that
    # holds little is a check when the address that served it, its title, or its elements bear
    # a guard's sign. A widget that asks for a captcha in a form is no sign: a page of a site
    # may hold one.
    if page.words >= LITTLE_TEXT_WORDS:
        return False
    tree = trafilatura.load_html(page.content)
    if tree is None:
        raise ValueError(f"the page served from {page.served} holds no HTML")
    return (
        names_check(page.asked, page.served)
        or any(has_check_title(title.text_content()) for title in tree.xpath(TITLE_PATH))
        or bool(tree.xpath(CHECK_ELEMENTS))
    )
