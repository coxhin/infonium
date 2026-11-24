import { Selection } from "@tiptap/pm/state";
import { EditorContent, useEditor, useEditorState } from "@tiptap/react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type Ref } from "react";
import type { Opener } from "./chat";
import type { Citing } from "./citing";
import {
  addLinked,
  attachWriter,
  failureText,
  freshen,
  handIn,
  keepAgain,
  keepAsNew,
  letGo,
  linkedPassage,
  newPassageId,
  OVERTAKEN,
  passagesOf,
  putInPlace,
  REMOVED,
  refusedWords,
  textOf,
  TITLE_CHARACTERS,
  useOpening,
  useSession,
  type Keeping,
  type Linked,
  type Ready,
  type Text,
  type Writer,
} from "./notekeeping";
import { openNote, takeJustMade } from "./notelist";
import { isWebAddress } from "./notemarkdown";
import {
  chipAt,
  linkedIds,
  markdownOf,
  noteExtensions,
  showChips,
  unheld,
  type ChipLook,
  type Hands,
} from "./noteschema";
import { NoteTools } from "./notetools";
import type { Seat } from "./seat";
import "./noteeditor.css";

// The editor a note is written in. It is fetched when a note is first opened, so that the
// reader's first load does not carry it.

type Shown = { noteId: string; seat: Seat | undefined; citing: Citing; onOpen: Opener };

// How long after the last key a change is kept.
const MOMENT_MS = 800;
const NO_WEB_ADDRESS = "链接只能指向其他网站的 http 或 https 网址。";
const EDITOR_PROPS = {
  attributes: {
    class: "note-text",
    role: "textbox",
    "aria-multiline": "true",
    "aria-label": "笔记正文",
    "aria-description": "Esc 回到格式按钮；Alt+Enter 打开光标处的引文或链接",
  },
};

function chipTitle(linked: Linked): string {
  const lines = [
    linked.passage.anchors.map((anchor) => anchor.quote).join(" … "),
    `——${linked.title}`,
  ];
  const { whereabouts } = linked;
  if (whereabouts.kind === "found" && !whereabouts.exact) {
    lines.push("原文后来略有改动，现在的字句与链接时略有不同。");
  }
  if (whereabouts.kind === "lost") {
    lines.push(`这一篇重新读取后找不到这段了，按下打开链接时的第 ${whereabouts.revision} 版。`);
  }
  return lines.join("\n");
}

function lookOf(linked: Linked, citing: Citing): ChipLook {
  const { whereabouts } = linked;
  return {
    title: chipTitle(linked),
    standing: whereabouts.kind === "lost" ? "lost" : whereabouts.exact ? "found" : "moved",
    revision: whereabouts.kind === "lost" ? `第 ${whereabouts.revision} 版` : undefined,
    pressed: whereabouts.kind === "found" && citing.isPressed(whereabouts.anchors[0]),
  };
}

function openAddress(address: string): void {
  if (isWebAddress(address)) {
    window.open(address, "_blank", "noopener,noreferrer");
  }
}

// A passage carried on the clipboard, which may come from any page: what is not a passage of
// a note as this app writes it is not taken as one.
function carriedLinked(carried: string): Linked | undefined {
  let value: unknown;
  try {
    value = JSON.parse(carried);
  } catch {
    // Not JSON: the chip is pasted as words that link to nothing.
    return undefined;
  }
  const linked = value as Partial<Linked> | null;
  return typeof linked?.itemId === "string" &&
    typeof linked.title === "string" &&
    Array.isArray(linked.passage?.anchors) &&
    linked.passage.anchors.length > 0 &&
    (linked.whereabouts?.kind === "found" || linked.whereabouts?.kind === "lost")
    ? (linked as Linked)
    : undefined;
}

// Where the note's text stands, said to a screen reader as it changes; what was written and
// not sent is offered a way on.
// The ways on for a text not sent, of which one at a time is under way.
type Offers = {
  putInPlace: () => void;
  keepAsNew: () => void;
  letGo: () => void;
  offering: boolean;
  problem: string | undefined;
};

function KeepingNote({
  noteId,
  keeping,
  typing,
  offers,
}: {
  noteId: string;
  keeping: Keeping;
  typing: boolean;
  offers: Offers;
}) {
  const said = offers.problem === undefined ? null : <p>没有做到：{offers.problem}</p>;
  switch (keeping.state) {
    case "unkept":
      return (
        <p className="keeping failed" role="alert">
          没有保存：{keeping.reason}
          {keeping.away && "，服务回来后会自动保存"}
          <button onClick={() => keepAgain(noteId)}>重试</button>
        </p>
      );
    case "overtaken":
      return (
        <div className="keeping failed" role="alert" aria-busy={offers.offering}>
          <p>{OVERTAKEN}写下的内容都还在这里。</p>
          <button disabled={offers.offering} onClick={offers.putInPlace}>
            用这里的替换那边的
          </button>
          <button disabled={offers.offering} onClick={offers.keepAsNew}>
            另存为新笔记
          </button>
          <button disabled={offers.offering} onClick={offers.letGo}>
            放弃这里的，读入那边的
          </button>
          {said}
        </div>
      );
    case "removed":
      return (
        <div className="keeping failed" role="alert" aria-busy={offers.offering}>
          <p>{REMOVED}写下的内容都还在这里。</p>
          <button disabled={offers.offering} onClick={offers.keepAsNew}>
            另存为新笔记
          </button>
          {said}
        </div>
      );
    case "kept":
    case "keeping":
      return (
        <p className="keeping" role="status">
          {typing || keeping.state === "keeping" ? "正在保存…" : "已保存"}
        </p>
      );
  }
}

function Back({ ref }: { ref?: Ref<HTMLButtonElement> }) {
  return (
    <button ref={ref} className="back" aria-label="回到笔记列表" onClick={() => openNote(undefined)}>
      ←<span className="back-words"> 笔记列表</span>
    </button>
  );
}

function Writing({ noteId, seat, citing, onOpen, ready }: Shown & { ready: Ready }) {
  const start = textOf(ready);
  const [title, setTitle] = useState(start.title);
  const [typing, setTyping] = useState(false);
  const [offering, setOffering] = useState(false);
  const [offerProblem, setOfferProblem] = useState<string>();
  const refusals = refusedWords(ready);
  // The address being given to a link, and why it was refused.
  const [linking, setLinking] = useState<{ address: string; refused: boolean }>();
  const written = useRef({ title: start.title, dirty: false });
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const toolbar = useRef<HTMLDivElement>(null);
  const addressField = useRef<HTMLInputElement>(null);
  const titleField = useRef<HTMLInputElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  const near = useRef({ seat, citing, onOpen });
  useLayoutEffect(() => {
    near.current = { seat, citing, onOpen };
  });
  // A note just made is to be written in, so its title takes the keyboard. A note opened from
  // a control that has gone with the list gives the keyboard to the way back, which brings up
  // no screen keyboard.
  useEffect(() => {
    if (takeJustMade(noteId)) {
      titleField.current?.focus();
    } else if (document.activeElement === document.body) {
      back.current?.focus();
    }
  }, []);

  // What the editor asks of the note, and what the note asks of the editor, reach both as they
  // are at the latest render: React may make the editor anew, as it does while it checks a
  // component twice in development, and a closure of an earlier render holds the editor gone.
  const acts = useRef<Hands | undefined>(undefined);
  const writing = useRef<Writer | undefined>(undefined);
  const hands = useMemo<Hands>(
    () => ({
      press: (passageId) => acts.current?.press(passageId),
      openHere: () => acts.current?.openHere() ?? false,
      leave: () => acts.current?.leave(),
      askLink: () => acts.current?.askLink(),
      carry: (passageId) => acts.current?.carry(passageId),
      adopt: (passageId, carried) => acts.current?.adopt(passageId, carried) ?? passageId,
    }),
    [],
  );
  // The options are the same at every render, so that the editor is not set up again.
  const extensions = useMemo(() => noteExtensions(hands), []);
  const editor = useEditor({
    extensions,
    content: start.markdown,
    contentType: "markdown",
    immediatelyRender: true,
    shouldRerenderOnTransaction: false,
    editorProps: EDITOR_PROPS,
    onUpdate: () => changed(),
    onBlur: () => handInTyped(false),
  });

  function text(): Text {
    return {
      title: written.current.title,
      markdown: markdownOf(editor),
      passages: passagesOf(noteId, linkedIds(editor.state.doc)),
    };
  }

  // What was typed is handed in, unless a word is still being composed by an input method,
  // which is handed in once it is written; as the page is left it is handed in as it stands.
  // Answers whether what was typed is still not handed in.
  function handInTyped(leaving: boolean): boolean {
    clearTimeout(timer.current);
    if (!written.current.dirty) {
      return false;
    }
    if (editor.view.composing && !leaving) {
      timer.current = setTimeout(() => writing.current?.flush(false), MOMENT_MS);
      return true;
    }
    written.current.dirty = false;
    setTyping(false);
    handIn(noteId, text(), leaving, false, ready.reading);
    return false;
  }

  function changed() {
    written.current.dirty = true;
    setTyping(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => writing.current?.flush(false), MOMENT_MS);
  }

  function press(passageId: string) {
    const linked = linkedPassage(noteId, passageId);
    if (linked === undefined) {
      return;
    }
    const { whereabouts, itemId } = linked;
    const { seat: seated, citing: pointing, onOpen: opening } = near.current;
    if (whereabouts.kind === "lost") {
      pointing.openInRevision(whereabouts.anchors, itemId, whereabouts.revision);
    } else if (seated?.itemId === itemId) {
      pointing.point(whereabouts.anchors);
    } else {
      opening(itemId, whereabouts.anchors);
    }
  }

  // A pasted chip goes under the id this note holds its very passage by; one this note does
  // not hold is taken with its passage under a new id; one that carries no passage links to
  // nothing unless this note holds its id.
  function adoptPasted(passageId: string, carried: string | undefined): string {
    const held = linkedPassage(noteId, passageId);
    const linked = carried === undefined ? undefined : carriedLinked(carried);
    const taken = (id: string) => linkedPassage(noteId, id) !== undefined;
    if (linked === undefined) {
      return held !== undefined && carried === undefined ? passageId : newPassageId(taken);
    }
    const same = JSON.stringify(linked.passage);
    if (held !== undefined && JSON.stringify(held.passage) === same) {
      return passageId;
    }
    const fresh = newPassageId(taken);
    addLinked(noteId, new Map([[fresh, linked]]));
    return fresh;
  }

  acts.current = {
    press,
    openHere() {
      const passageId = chipAt(editor.state);
      if (passageId !== undefined) {
        press(passageId);
        return true;
      }
      if (editor.isActive("link")) {
        openAddress(String(editor.getAttributes("link").href ?? ""));
        return true;
      }
      return false;
    },
    leave() {
      toolbar.current?.querySelector<HTMLButtonElement>('button[tabindex="0"]')?.focus();
    },
    askLink() {
      setLinking({ address: String(editor.getAttributes("link").href ?? ""), refused: false });
    },
    carry(passageId) {
      const linked = linkedPassage(noteId, passageId);
      return linked === undefined ? undefined : JSON.stringify(linked);
    },
    adopt: adoptPasted,
  };

  writing.current = {
    text,
    append(markdown) {
      // The excerpt takes the place of the empty paragraphs at the end, which the editor keeps
      // there for the cursor and which are not the note's.
      const { doc } = editor.state;
      const end = doc.content.size;
      let from = end;
      for (let index = doc.childCount - 1; index >= 0; index -= 1) {
        const block = doc.child(index);
        if (block.type.name !== "paragraph" || block.content.size > 0) {
          break;
        }
        from -= block.nodeSize;
      }
      editor.commands.insertContentAt(from === end ? end : { from, to: end }, markdown, {
        contentType: "markdown",
        updateSelection: false,
      });
      changed();
    },
    flush: handInTyped,
  };

  useEffect(
    () =>
      attachWriter(noteId, {
        text: () => writing.current?.text() ?? start,
        append: (markdown) => writing.current?.append(markdown),
        flush: (leaving) => writing.current?.flush(leaving) ?? false,
      }),
    [],
  );
  // What was typed is handed in when the note is left, and when another item comes on show.
  useEffect(() => () => void writing.current?.flush(false), [seat?.itemId]);
  useEffect(() => {
    if (linking !== undefined) {
      addressField.current?.focus();
    }
  }, [linking === undefined]);

  const looks = new Map(
    [...ready.linked]
      .filter(([passageId]) => !ready.refused.has(passageId))
      .map(([passageId, linked]) => [passageId, lookOf(linked, citing)]),
  );
  const lookKey = JSON.stringify([...looks]);
  useEffect(() => showChips(editor, looks), [lookKey, editor]);

  const here = useEditorState({
    editor,
    selector: ({ editor: shown }) => ({
      chip: chipAt(shown.state),
      address: shown.isActive("link") ? String(shown.getAttributes("link").href ?? "") : undefined,
    }),
  });

  function retitle(words: string) {
    setTitle(words);
    written.current.title = words;
    changed();
  }

  // What is written here, overtaken or of a removed note, is kept as a new note, opened.
  // One way on at a time; while it is under way nothing can be typed, so that what the reader
  // sees is what it takes.
  async function offer(doing: () => Promise<unknown>) {
    setOffering(true);
    setOfferProblem(undefined);
    editor.setEditable(false, false);
    try {
      await doing();
    } catch (error) {
      setOfferProblem(failureText(error));
    } finally {
      if (!editor.isDestroyed) {
        editor.setEditable(true, false);
      }
      setOffering(false);
    }
  }

  const offers: Offers = {
    putInPlace: () => void offer(() => putInPlace(noteId)),
    keepAsNew: () =>
      void offer(async () => {
        const note = await keepAsNew(noteId);
        openNote(note.id);
      }),
    letGo: () => void offer(() => letGo(noteId)),
    offering,
    problem: offerProblem,
  };

  function link(event: FormEvent) {
    event.preventDefault();
    const address = linking?.address.trim() ?? "";
    if (!isWebAddress(address)) {
      setLinking({ address, refused: true });
      return;
    }
    const chain = editor.chain().focus();
    if (editor.state.selection.empty && !editor.isActive("link")) {
      // What is typed after the address is not part of the link.
      chain
        .insertContent({
          type: "text",
          text: address,
          marks: [{ type: "link", attrs: { href: address } }],
        })
        .unsetMark("link")
        .run();
    } else {
      chain.extendMarkRange("link").setLink({ href: address }).run();
    }
    setLinking(undefined);
  }

  function unlink() {
    editor.chain().focus().extendMarkRange("link").unsetLink().run();
    setLinking(undefined);
  }

  const chipLinked = here.chip === undefined ? undefined : ready.linked.get(here.chip);
  const [strange] = useState(() => unheld(editor, start.markdown));

  return (
    <div className="note-editor">
      <header>
        <Back ref={back} />
        <KeepingNote
          noteId={noteId}
          keeping={ready.keeping}
          typing={typing}
          offers={offers}
        />
        {refusals.length > 0 && (
          <p className="keeping failed" role="alert">
            服务没有收下引文{refusals.join("、")}，它在笔记里只剩文字，可以找到后删去。
          </p>
        )}
        <NoteTools
          ref={toolbar}
          editor={editor}
          linking={here.address !== undefined}
          onLink={() => acts.current?.askLink()}
        />
        {linking !== undefined && (
          <form className="note-linking" onSubmit={link}>
            <input
              ref={addressField}
              type="url"
              aria-label="链接的网址"
              placeholder="https://"
              value={linking.address}
              onChange={(event) => setLinking({ address: event.target.value, refused: false })}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setLinking(undefined);
                  editor.commands.focus();
                }
              }}
            />
            <button>确定</button>
            {here.address !== undefined && (
              <button type="button" onClick={unlink}>
                去掉链接
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setLinking(undefined);
                editor.commands.focus();
              }}
            >
              取消
            </button>
            {linking.refused && (
              <p className="status failed" role="alert">
                {NO_WEB_ADDRESS}
              </p>
            )}
          </form>
        )}
      </header>
      {strange.length > 0 && (
        <p className="status note-unheld">
          这篇笔记的 Markdown 里有编辑器不能显示的{strange.join("、")}；编辑后它们会变成纯文字或丢失。
        </p>
      )}
      <input
        ref={titleField}
        className="note-title"
        aria-label="标题"
        placeholder="标题"
        maxLength={TITLE_CHARACTERS}
        value={title}
        onChange={(event) => retitle(event.target.value)}
        onKeyDown={(event) => {
          // Enter ends the title, as Tab does, unless it ends a word an input method composes.
          if (event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault();
            // Focused at once, so that the keys that follow go into the text, not the title.
            editor.view.focus();
            editor.commands.setTextSelection(Selection.atStart(editor.state.doc).from);
          }
        }}
        onBlur={() => handInTyped(false)}
      />
      <EditorContent editor={editor} className="note-body" />
      {chipLinked !== undefined && here.chip !== undefined && (
        <p className="note-here">
          <span>引文：{chipLinked.title}</span>
          <button onClick={() => press(here.chip ?? "")} title="Alt+Enter">
            显示这段原文
          </button>
        </p>
      )}
      {here.address !== undefined && here.chip === undefined && (
        <p className="note-here">
          <span>链接：{here.address}</span>
          {isWebAddress(here.address) && (
            <a href={here.address} target="_blank" rel="noreferrer noopener">
              在新标签页打开
            </a>
          )}
          <button onClick={() => acts.current?.askLink()}>修改</button>
        </p>
      )}
    </div>
  );
}

export default function NoteEditor({ noteId, seat, citing, onOpen }: Shown) {
  useOpening(noteId);
  const session = useSession(noteId);
  if (session?.state === "failed") {
    return (
      <div className="note-editor">
        <header>
          <Back />
        </header>
        <p className="status failed">
          {session.reason}
          {!session.gone && (
            <button onClick={() => void freshen(noteId).catch(() => undefined)}>重试</button>
          )}
        </p>
      </div>
    );
  }
  if (session?.state !== "ready") {
    return (
      <div className="note-editor">
        <header>
          <Back />
        </header>
        <p className="status">正在打开笔记…</p>
      </div>
    );
  }
  // Each reading of the note is written in an editor of its own.
  return (
    <Writing
      key={session.reading}
      noteId={noteId}
      seat={seat}
      citing={citing}
      onOpen={onOpen}
      ready={session}
    />
  );
}
