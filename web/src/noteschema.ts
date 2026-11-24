import { Link } from "@tiptap/extension-link";
import { OrderedList } from "@tiptap/extension-list";
import { Paragraph } from "@tiptap/extension-paragraph";
import { Markdown } from "@tiptap/markdown";
import type { Node as ProseNode } from "@tiptap/pm/model";
import { NodeSelection, Plugin, PluginKey, type EditorState } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import {
  Extension,
  Node,
  mergeAttributes,
  type AnyExtension,
  type Editor,
  type MarkdownToken,
} from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { PASSAGE_LINK } from "./client";
import { inlineText, paragraphLine, passageLink } from "./notemarkdown";

// What a note is written in: the editor's kinds of text, read from Markdown and written back to
// it, and the links that lead to the note's passages, drawn as chips.

export const PASSAGE_NODE = "passageLink";
const WEB_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:"]);
const UNHELD_KINDS: Record<string, string | undefined> = {
  table: "表格",
  image: "图片",
  html: "HTML",
};

// How a passage link is drawn: as a chip of a passage the note holds, with what it is called
// and where it stands now; a link that names no passage of the note is drawn as its words.
export type ChipLook = {
  title: string;
  standing: "found" | "moved" | "lost";
  revision: string | undefined;
  pressed: boolean;
};

// What the editor asks of the note around it: to show a passage, to open what the cursor is on,
// to give the keyboard back to the controls, and to ask for a link's address.
export type Hands = {
  press: (passageId: string) => void;
  openHere: () => boolean;
  leave: () => void;
  askLink: () => void;
};

const chipsKey = new PluginKey<Chips>("passageChips");
type Chips = { looks: ReadonlyMap<string, ChipLook>; decorations: DecorationSet };

// A link may lead to a web address only: none that runs a script or opens something of the app
// itself.
export function isWebAddress(address: string): boolean {
  const url = URL.parse(address);
  return url !== null && WEB_PROTOCOLS.has(url.protocol) && url.origin !== location.origin;
}

// The words of a link's text, without its marks.
function plainText(tokens: MarkdownToken[] | undefined): string {
  return (tokens ?? [])
    .map((token) => (token.tokens === undefined ? (token.text ?? "") : plainText(token.tokens)))
    .join("");
}

// A link of a note to one of its passages. It is one piece, written over, cut, copied and pasted
// as a character is; its words are kept as the Markdown they were written in.
const PassageLink = Node.create({
  name: PASSAGE_NODE,
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      id: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-passage") ?? "",
        renderHTML: (attributes) => ({ "data-passage": attributes.id }),
      },
      // A chip pasted from the clipboard brings its words as text, never as Markdown.
      words: {
        default: "",
        parseHTML: (element) => inlineText(element.textContent ?? ""),
        rendered: false,
      },
      text: { default: "", parseHTML: (element) => element.textContent ?? "", rendered: false },
    };
  },
  parseHTML() {
    return [{ tag: "span[data-passage]" }];
  },
  renderHTML({ node, HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, { class: "passage-link" }),
      String(node.attrs.text),
    ];
  },
  renderText({ node }) {
    return String(node.attrs.text);
  },
  renderMarkdown(node) {
    return passageLink(String(node.attrs?.words ?? ""), String(node.attrs?.id ?? ""));
  },
});

const WebLink = Link.extend({
  parseMarkdown(token, helpers) {
    const address = String(token.href ?? "");
    if (address.startsWith(PASSAGE_LINK)) {
      return helpers.createNode(PASSAGE_NODE, {
        id: address.slice(PASSAGE_LINK.length),
        words: token.text ?? "",
        text: plainText(token.tokens),
      });
    }
    const content = helpers.parseInline(token.tokens ?? []);
    if (!isWebAddress(address)) {
      return content;
    }
    return helpers.applyMark("link", content, { href: address, title: token.title || null });
  },
});

// TipTap reads an ordered list by a reader of its own that misplaces an ordered list nested in
// an item of a list without numbers; Markdown's own reading of lists places it.
const NumberedList = OrderedList.extend({
  markdownTokenizer: {
    name: "orderedList",
    level: "block",
    start: () => -1,
    tokenize: () => undefined,
  },
});

// TipTap writes a paragraph's text as it is, so a paragraph whose line starts as a heading, an
// item or a rule would come back as one.
const KeptParagraph = Paragraph.extend({
  renderMarkdown(node, helpers, context) {
    const written = this.parent?.(node, helpers, context) ?? "";
    return written.split("\n").map(paragraphLine).join("\n");
  },
});

function chipsIn(doc: ProseNode, looks: ReadonlyMap<string, ChipLook>): DecorationSet {
  const decorations: Decoration[] = [];
  doc.descendants((node, position) => {
    if (node.type.name !== PASSAGE_NODE) {
      return true;
    }
    const look = looks.get(String(node.attrs.id));
    decorations.push(
      Decoration.node(
        position,
        position + node.nodeSize,
        look === undefined
          ? { class: "unlinked" }
          : {
              class: "chip",
              title: look.title,
              "data-standing": look.standing,
              "data-revision": look.revision ?? "",
              "aria-pressed": String(look.pressed),
            },
      ),
    );
    return false;
  });
  return DecorationSet.create(doc, decorations);
}

// Draws the passage links as chips, and shows a chip's passage when it is pressed.
const PassageChips = Extension.create<{ hands: Hands }>({
  name: "passageChips",
  addKeyboardShortcuts() {
    return {
      "Alt-Enter": () => this.options.hands.openHere(),
      "Mod-k": () => (this.options.hands.askLink(), true),
      Escape: () => (this.options.hands.leave(), true),
    };
  },
  addProseMirrorPlugins() {
    const { hands: held } = this.options;
    return [
      new Plugin<Chips>({
        key: chipsKey,
        state: {
          init: (_, state) => ({ looks: new Map(), decorations: chipsIn(state.doc, new Map()) }),
          apply(transaction, chips, _, state) {
            const looks = (transaction.getMeta(chipsKey) as Chips["looks"] | undefined) ?? chips.looks;
            if (looks === chips.looks && !transaction.docChanged) {
              return chips;
            }
            return { looks, decorations: chipsIn(state.doc, looks) };
          },
        },
        props: {
          decorations: (state) => chipsKey.getState(state)?.decorations,
          handleClickOn(_, __, node, ___, ____, direct) {
            if (!direct || node.type.name !== PASSAGE_NODE) {
              return false;
            }
            held.press(String(node.attrs.id));
            return true;
          },
        },
      }),
    ];
  },
});

export function noteExtensions(noteHands: Hands): AnyExtension[] {
  return [
    StarterKit.configure({
      link: false,
      orderedList: false,
      paragraph: false,
      underline: false,
    }),
    NumberedList,
    KeptParagraph,
    WebLink.configure({
      openOnClick: false,
      autolink: true,
      linkOnPaste: true,
      markdownLinks: true,
      defaultProtocol: "https",
      isAllowedUri: isWebAddress,
      shouldAutoLink: isWebAddress,
      HTMLAttributes: { target: "_blank", rel: "noreferrer noopener", class: null },
    }),
    PassageLink,
    PassageChips.configure({ hands: noteHands }),
    Markdown,
  ];
}

// The note's text as Markdown. The empty paragraphs at its end are the editor's, kept there so
// that the cursor can reach past a last list, quotation or listing; they are not the note's.
export function markdownOf(editor: Editor): string {
  const content = [...(editor.getJSON().content ?? [])];
  while (content.at(-1)?.type === "paragraph" && (content.at(-1)?.content ?? []).length === 0) {
    content.pop();
  }
  return editor.markdown?.serialize({ type: "doc", content }) ?? editor.getMarkdown();
}

// What of a note's Markdown the editor cannot hold, which an edit turns into plain text or
// drops: tables, pictures and HTML.
export function unheld(editor: Editor, markdown: string): string[] {
  const kinds = new Set<string>();
  const visit = (tokens: MarkdownToken[]) => {
    for (const token of tokens) {
      const kind = UNHELD_KINDS[token.type ?? ""];
      if (kind !== undefined) {
        kinds.add(kind);
      }
      visit([...(token.tokens ?? []), ...((token.items as MarkdownToken[] | undefined) ?? [])]);
    }
  };
  visit((editor.markdown?.instance.lexer(markdown) ?? []) as MarkdownToken[]);
  return [...kinds];
}

export function showChips(editor: Editor, looks: ReadonlyMap<string, ChipLook>): void {
  editor.view.dispatch(
    editor.state.tr.setMeta(chipsKey, looks).setMeta("addToHistory", false),
  );
}

// The passage link the cursor is on or beside, if any.
export function chipAt(state: EditorState): string | undefined {
  const { selection } = state;
  if (selection instanceof NodeSelection) {
    return selection.node.type.name === PASSAGE_NODE ? String(selection.node.attrs.id) : undefined;
  }
  if (!selection.empty) {
    return undefined;
  }
  const beside = [selection.$from.nodeBefore, selection.$from.nodeAfter].find(
    (node) => node?.type.name === PASSAGE_NODE,
  );
  return beside === undefined || beside === null ? undefined : String(beside.attrs.id);
}

// The ids of the passages the note links to, in the order of the note, each once.
export function linkedIds(doc: ProseNode): string[] {
  const ids = new Set<string>();
  doc.descendants((node) => {
    if (node.type.name === PASSAGE_NODE) {
      ids.add(String(node.attrs.id));
    }
    return true;
  });
  return [...ids];
}
