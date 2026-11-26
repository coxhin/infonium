from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class DigestStyle(StrEnum):
    # The styles a digest is written in, in the order a reader is offered them: those written
    # by a recipe, then the reader's own.
    OVERVIEW = "overview"
    GUIDE = "guide"
    BRIEF = "brief"
    PAPER = "paper"
    OUTLINE = "outline"
    FAQ = "faq"
    CUSTOM = "custom"


RecipeStyle = Literal[
    DigestStyle.OVERVIEW,
    DigestStyle.GUIDE,
    DigestStyle.BRIEF,
    DigestStyle.PAPER,
    DigestStyle.OUTLINE,
    DigestStyle.FAQ,
]


class Recipe(BaseModel):
    # A style of digest as data, the shape a recipe plugin takes: its name and the description
    # of what its digest holds, as a reader is offered them, and the part of a digest's
    # instructions that says which sections to write and what each holds.
    style: RecipeStyle
    name: str
    description: str
    sections: str


# The style whose sections the reader describes in their own words, in place of a recipe.
CUSTOM_NAME = "自定义"
CUSTOM_DESCRIPTION = "写下你想要的结构、篇幅和语气。引用、语言和标点的规则照旧。"

RECIPES = (
    Recipe(
        style=DigestStyle.OVERVIEW,
        name="概述与要点",
        description=(
            "3\N{EN DASH}5 句概述\N{FULLWIDTH COMMA}加 5\N{EN DASH}10 条值得记住的要点。"
            "原来的写法\N{FULLWIDTH COMMA}适合大多数文章。"
        ),
        sections="""\
Write two sections.

The first section is titled with the word for "Summary" in the output language, and is a \
list. It holds three to five statements, one sentence each, that together tell what the \
document is about, what it argues or reports, and what it concludes.

The second section is titled with the word for "Takeaways" in the output language, and is a \
list. It holds five to ten statements that a careful reader would want to keep: the key \
claims, findings, numbers, definitions, recommendations and caveats. Each statement stands \
on its own and is specific.
""",
    ),
    Recipe(
        style=DigestStyle.GUIDE,
        name="导读",
        description=(
            "一段概要\N{FULLWIDTH LEFT PARENTHESIS}关键词加粗\N{FULLWIDTH RIGHT PARENTHESIS}"
            "\N{FULLWIDTH COMMA}加 3\N{EN DASH}6 个关键主题。仿 Gemini Notebook 的来源导读。"
        ),
        sections="""\
Write two sections.

The first section is titled with the word for "Overview" in the output language, and is a \
paragraph. It holds three to six statements, one sentence each: what the source is, its \
kind and, where it names them, its author or speakers; its main argument or findings; and \
where it ends up.

The second section is titled with the words for "Key topics" in the output language, and is \
a list. It holds three to six statements, one for each topic the document takes up, in the \
order it takes them up. Each statement begins with the topic's name in bold, followed by \
what the document says about that topic in one or two sentences.
""",
    ),
    Recipe(
        style=DigestStyle.BRIEF,
        name="简报",
        description=(
            "一句话结论、为什么重要、关键事实、保留与接下来。"
            "仿 Axios 的 Smart Brevity 和“结论先行”\N{FULLWIDTH LEFT PARENTHESIS}BLUF"
            "\N{FULLWIDTH RIGHT PARENTHESIS}。适合新闻、公告、报告。"
        ),
        sections="""\
Write these sections, in this order.

The first section is titled with the words for "Bottom line" in the output language, and is \
a paragraph. It holds one statement: what is new, or what the document concludes, in one \
sentence a reader could stop after.

The second section is titled with the words for "Why it matters" in the output language, \
and is a paragraph. It holds one or two statements: why it matters as the document presents \
it, meaning its stakes, its impact, or the larger trend it belongs to. Leave it out when the \
document does not say.

The third section is titled with the words for "Key facts" in the output language, and is a \
list. It holds three to six statements: the facts, numbers and details that support the \
bottom line.

The fourth section is titled with the words for "Yes, but" in the output language, and is a \
list. It holds up to three statements: caveats, counterpoints or uncertainties that the \
document states. Leave it out when the document states none.

The fifth section is titled with the words for "What's next" in the output language, and is \
a list. It holds up to three statements: what the document says comes next, such as dates, \
decisions and open questions. Leave it out when the document says nothing of it.
""",
    ),
    Recipe(
        style=DigestStyle.PAPER,
        name="论文精读",
        description=(
            "一句话、问题与动机、方法、结果、局限。"
            "参考 Keshav 的三遍读论文法和 Fabric 的 summarize_paper。适合论文和技术报告。"
        ),
        sections="""\
Write these sections, in this order.

The first section is titled with the words for "In one sentence" in the output language, and \
is a paragraph. It holds one statement: the problem the work addresses, and its main \
contribution or result.

The second section is titled with the words for "Problem and motivation" in the output \
language, and is a list. It holds one to three statements: the question, why it matters, and \
the gap in earlier work that the document names.

The third section is titled with the word for "Method" in the output language, and is a \
list. It holds two to five statements: how the work goes about it, such as its key idea, its \
models, its data and its setup, and what the document says is new about it.

The fourth section is titled with the word for "Results" in the output language, and is a \
list. It holds two to six statements: the main findings, with the numbers, datasets, \
baselines and measures as the document reports them.

The fifth section is titled with the word for "Limitations" in the output language, and is a \
list. It holds up to four statements: the limitations, assumptions and open questions that \
the document itself states. Judge nothing of the work's quality yourself. Leave it out when \
the document states none.

When the document is not a paper, write the sections that apply to it and leave out the \
rest.
""",
    ),
    Recipe(
        style=DigestStyle.OUTLINE,
        name="章节速览",
        description=(
            "顺着原文的顺序分成 3\N{EN DASH}10 段\N{FULLWIDTH COMMA}每段一个标题和几条要点"
            "\N{FULLWIDTH SEMICOLON}引文能跳到那一段\N{FULLWIDTH COMMA}录音能跳到那一刻。"
            "参考通义听悟的章节速览。适合长文、讲座、播客。"
        ),
        sections="""\
Write three to ten sections that follow the document's own order and parts. Each section is \
a list of one to four statements. In a document with headings, the sections are its main \
parts, each titled with its own heading in the output language, shortened if it is long. In \
a recording, or in a document without headings, the sections are the stretches where the \
topic changes, each titled by its topic.
""",
    ),
    Recipe(
        style=DigestStyle.FAQ,
        name="问答",
        description=(
            "4\N{EN DASH}8 个读者会问的问题\N{FULLWIDTH COMMA}每个都用原文回答。"
            "仿 NotebookLM 的 FAQ 和通义听悟的问答回顾。"
        ),
        sections="""\
Write four to eight sections, each a question that a reader of the document would ask, with \
its answer. Title each section with its question, ending with a question mark. Each section \
is a paragraph of one to three statements that answer the question. Order the questions from \
the broadest, such as what the document is about and what it concludes, to the most \
specific. Ask only questions that the document answers.
""",
    ),
)


def recipe_of(style: RecipeStyle) -> Recipe:
    return next(recipe for recipe in RECIPES if recipe.style == style)
