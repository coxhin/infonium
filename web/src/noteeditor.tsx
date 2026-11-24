import { EditorContent, useEditor, useEditorState, type Editor } from "@tiptap/react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { Opener } from "./chat";
import type { Citing } from "./citing";
import type { NoteDraft } from "./client";
import { revisionLink } from "./links";
import {
  attachWriter,
  GONE,
  handIn,
  keepAgain,
  linkedPassage,
  openNote,
  passagesOf,
  readAgain,
  textOf,
  useSession,
  type Keeping,
  type Linked,
  type Session,
} from "./notes";
import {
  chipAt,
  isWebAddress,
  linkedIds,
  markdownOf,
  noteExtensions,
  showChips,
  unheld,
  type ChipLook,
  type Hands,
} from "./noteschema";
import type { Seat } from "./seat";
import "./noteeditor.css";

// The editor a note is written in. It is fetched when a note is first opened, so that the
// reader's first load does not carry it.

type Shown = { noteId: string; seat: Seat | undefined; citing: Citing; onOpen: Opener };

// How long after the last key a change is kept.
const MOMENT_MS = 800;
const MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
const NO_WEB_ADDRESS = "链接只能指向 http 或 https 开头的网址，不能指向本应用自己。";

type Control = {
  name: string;
  glyph: string;
  keys: string | undefined;
  typed: string | undefined;
  run: (editor: Editor) => boolean;
  active: ((editor: Editor) => boolean) | undefined;
};

const CONTROLS: Control[] = [
  {
    name: "一级标题",
    glyph: "H1",
    keys: `${MOD}+Alt+1`,
    typed: "# ",
    run: (editor) => editor.chain().focus().toggleHeading({ level: 1 }).run(),
    active: (editor) => editor.isActive("heading", { level: 1 }),
  },
  {
    name: "二级标题",
    glyph: "H2",
    keys: `${MOD}+Alt+2`,
    typed: "## ",
    run: (editor) => editor.chain().focus().toggleHeading({ level: 2 }).run(),
    active: (editor) => editor.isActive("heading", { level: 2 }),
  },
  {
    name: "三级标题",
    glyph: "H3",
    keys: `${MOD}+Alt+3`,
    typed: "### ",
    run: (editor) => editor.chain().focus().toggleHeading({ level: 3 }).run(),
    active: (editor) => editor.isActive("heading", { level: 3 }),
  },
  {
    name: "加粗",
    glyph: "B",
    keys: `${MOD}+B`,
    typed: "**文字**",
    run: (editor) => editor.chain().focus().toggleBold().run(),
    active: (editor) => editor.isActive("bold"),
  },
  {
    name: "斜体",
    glyph: "I",
    keys: `${MOD}+I`,
    typed: "*文字*",
    run: (editor) => editor.chain().focus().toggleItalic().run(),
    active: (editor) => editor.isActive("italic"),
  },
  {
    name: "删除线",
    glyph: "S",
    keys: `${MOD}+Shift+S`,
    typed: "~~文字~~",
    run: (editor) => editor.chain().focus().toggleStrike().run(),
    active: (editor) => editor.isActive("strike"),
  },
  {
    name: "行内代码",
    glyph: "`",
    keys: `${MOD}+E`,
    typed: "`文字`",
    run: (editor) => editor.chain().focus().toggleCode().run(),
    active: (editor) => editor.isActive("code"),
  },
  {
    name: "无序列表",
    glyph: "•",
    keys: `${MOD}+Shift+8`,
    typed: "- ",
    run: (editor) => editor.chain().focus().toggleBulletList().run(),
    active: (editor) => editor.isActive("bulletList"),
  },
  {
    name: "有序列表",
    glyph: "1.",
    keys: `${MOD}+Shift+7`,
    typed: "1. ",
    run: (editor) => editor.chain().focus().toggleOrderedList().run(),
    active: (editor) => editor.isActive("orderedList"),
  },
  {
    name: "缩进一层",
    glyph: "⇥",
    keys: "Tab",
    typed: undefined,
    run: (editor) => editor.chain().focus().sinkListItem("listItem").run(),
    active: undefined,
  },
  {
    name: "退出一层",
    glyph: "⇤",
    keys: "Shift+Tab",
    typed: undefined,
    run: (editor) => editor.chain().focus().liftListItem("listItem").run(),
    active: undefined,
  },
  {
    name: "引用",
    glyph: "❝",
    keys: `${MOD}+Shift+B`,
    typed: "> ",
    run: (editor) => editor.chain().focus().toggleBlockquote().run(),
    active: (editor) => editor.isActive("blockquote"),
  },
  {
    name: "代码块",
    glyph: "{ }",
    keys: `${MOD}+Alt+C`,
    typed: "``` ",
    run: (editor) => editor.chain().focus().toggleCodeBlock().run(),
    active: (editor) => editor.isActive("codeBlock"),
  },
  {
    name: "分隔线",
    glyph: "—",
    keys: undefined,
    typed: "---",
    run: (editor) => editor.chain().focus().setHorizontalRule().run(),
    active: undefined,
  },
];

function controlTitle(control: Control): string {
  const ways = [
    control.keys === undefined ? undefined : `按 ${control.keys}`,
    control.typed === undefined ? undefined : `或输入 ${control.typed}`,
  ].filter((way) => way !== undefined);
  return ways.length === 0 ? control.name : `${control.name}（${ways.join("，")}）`;
}

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
    standing:
      whereabouts.kind === "lost" ? "lost" : whereabouts.exact ? "found" : "moved",
    revision: whereabouts.kind === "lost" ? `第 ${whereabouts.revision} 版` : undefined,
    pressed: whereabouts.kind === "found" && citing.isPressed(whereabouts.anchors[0]),
  };
}

function openAddress(address: string): void {
  if (isWebAddress(address)) {
    window.open(address, "_blank", "noopener,noreferrer");
  }
}

function KeepingNote({
  noteId,
  keeping,
  typing,
}: {
  noteId: string;
  keeping: Keeping;
  typing: boolean;
}) {
  if (keeping.state === "unkept") {
    return (
      <p className="keeping failed" role="alert">
        没有保存：{keeping.reason}
        <button onClick={() => keepAgain(noteId)}>重试</button>
      </p>
    );
  }
  return (
    <p className="keeping">
      {typing || keeping.state === "keeping" ? "正在保存…" : "已保存"}
    </p>
  );
}

function Back() {
  return (
    <button className="back" onClick={() => openNote(undefined)}>
      ← 笔记列表
    </button>
  );
}

function Writing({
  noteId,
  seat,
  citing,
  onOpen,
  session,
  start,
}: Shown & { session: Session; start: NoteDraft }) {
  const [title, setTitle] = useState(start.title);
  const [typing, setTyping] = useState(false);
  // The address being given to a link, and why it was refused.
  const [linking, setLinking] = useState<{ address: string; refused: boolean }>();
  const written = useRef({ title: start.title, dirty: false });
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const toolbar = useRef<HTMLDivElement>(null);
  const addressField = useRef<HTMLInputElement>(null);
  const near = useRef({ seat, citing, onOpen });
  useLayoutEffect(() => {
    near.current = { seat, citing, onOpen };
  });

  // The editor is made once; what it asks of the note reaches the note as it is now.
  const acts = useRef<Hands>({ press() {}, openHere: () => false, leave() {}, askLink() {} });
  const hands = useMemo<Hands>(
    () => ({
      press: (passageId) => acts.current.press(passageId),
      openHere: () => acts.current.openHere(),
      leave: () => acts.current.leave(),
      askLink: () => acts.current.askLink(),
    }),
    [],
  );
  const editor = useEditor({
    extensions: noteExtensions(hands),
    content: start.markdown,
    contentType: "markdown",
    immediatelyRender: true,
    shouldRerenderOnTransaction: false,
    editorProps: {
      attributes: {
        class: "note-text",
        role: "textbox",
        "aria-multiline": "true",
        "aria-label": "笔记正文",
        "aria-description": "Esc 回到格式按钮；Alt+Enter 打开光标处的引文或链接",
      },
    },
    onUpdate: () => changed(),
    onBlur: () => handInTyped(false),
  });

  function draft(): NoteDraft {
    return {
      title: written.current.title,
      markdown: markdownOf(editor),
      passages: passagesOf(noteId, linkedIds(editor.state.doc)),
    };
  }

  function handInTyped(leaving: boolean): number | undefined {
    clearTimeout(timer.current);
    if (!written.current.dirty) {
      return undefined;
    }
    written.current.dirty = false;
    setTyping(false);
    return handIn(noteId, draft(), leaving);
  }

  function changed() {
    written.current.dirty = true;
    setTyping(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => handInTyped(false), MOMENT_MS);
  }

  function press(passageId: string) {
    const linked = linkedPassage(noteId, passageId);
    if (linked === undefined) {
      return;
    }
    const { whereabouts, itemId } = linked;
    const { seat: seated, citing: pointing, onOpen: opening } = near.current;
    if (whereabouts.kind === "lost") {
      pointing.awaitOpening(whereabouts.anchors, itemId);
      location.hash = revisionLink(itemId, whereabouts.revision);
    } else if (seated?.itemId === itemId) {
      pointing.point(whereabouts.anchors);
    } else {
      opening(itemId, whereabouts.anchors);
    }
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
      toolbar.current?.querySelector("button")?.focus();
    },
    askLink() {
      setLinking({ address: String(editor.getAttributes("link").href ?? ""), refused: false });
    },
  };

  useEffect(
    () =>
      attachWriter(noteId, {
        append(markdown) {
          const end = editor.state.doc.content.size;
          editor.commands.insertContentAt(editor.isEmpty ? { from: 0, to: end } : end, markdown, {
            contentType: "markdown",
            updateSelection: false,
          });
          written.current.dirty = true;
          return handInTyped(false) ?? 0;
        },
        flush: (leaving) => {
          handInTyped(leaving);
        },
      }),
    [],
  );
  // What was typed is handed in when the note is left, and when another item comes on show.
  useEffect(() => () => void handInTyped(false), [seat?.itemId]);
  useEffect(() => {
    if (linking !== undefined) {
      addressField.current?.focus();
    }
  }, [linking === undefined]);

  const looks = new Map(
    [...session.linked].map(([passageId, linked]) => [passageId, lookOf(linked, citing)]),
  );
  const lookKey = JSON.stringify([...looks]);
  useEffect(() => showChips(editor, looks), [lookKey]);

  const active = useEditorState({
    editor,
    selector: ({ editor: shown }) => ({
      controls: CONTROLS.map((control) => control.active?.(shown)),
      sinkable: shown.can().sinkListItem("listItem"),
      liftable: shown.can().liftListItem("listItem"),
      chip: chipAt(shown.state),
      address: shown.isActive("link") ? String(shown.getAttributes("link").href ?? "") : undefined,
    }),
  });

  function retitle(text: string) {
    setTitle(text);
    written.current.title = text;
    changed();
  }

  function link(event: FormEvent) {
    event.preventDefault();
    const address = linking?.address.trim() ?? "";
    if (!isWebAddress(address)) {
      setLinking({ address, refused: true });
      return;
    }
    const chain = editor.chain().focus();
    if (editor.state.selection.empty && !editor.isActive("link")) {
      chain
        .insertContent({ type: "text", text: address, marks: [{ type: "link", attrs: { href: address } }] })
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

  const chipLinked = active.chip === undefined ? undefined : session.linked.get(active.chip);
  const [strange] = useState(() => unheld(editor, start.markdown));

  return (
    <div className="note-editor">
      <header>
        <div className="note-bar">
          <Back />
          <KeepingNote noteId={noteId} keeping={session.keeping} typing={typing} />
        </div>
        <div className="note-tools" role="toolbar" aria-label="格式" ref={toolbar}>
          {CONTROLS.map((control, index) => (
            <button
              key={control.name}
              aria-label={control.name}
              title={controlTitle(control)}
              aria-keyshortcuts={control.keys?.replace("⌘", "Meta").replace("Ctrl", "Control")}
              aria-pressed={control.active === undefined ? undefined : active.controls[index]}
              disabled={
                (control.name === "缩进一层" && !active.sinkable) ||
                (control.name === "退出一层" && !active.liftable)
              }
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => control.run(editor)}
            >
              {control.glyph}
            </button>
          ))}
          <button
            aria-label="链接"
            title={`链接（按 ${MOD}+K，或输入 [文字](网址)）`}
            aria-keyshortcuts={MOD === "⌘" ? "Meta+K" : "Control+K"}
            aria-pressed={active.address !== undefined}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => acts.current.askLink()}
          >
            链接
          </button>
        </div>
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
            {active.address !== undefined && (
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
        className="note-title"
        aria-label="标题"
        placeholder="标题"
        value={title}
        onChange={(event) => retitle(event.target.value)}
        onBlur={() => handInTyped(false)}
      />
      <EditorContent editor={editor} className="note-body" />
      {chipLinked !== undefined && active.chip !== undefined && (
        <p className="note-here">
          <span>引文：{chipLinked.title}</span>
          <button onClick={() => press(active.chip ?? "")} title="Alt+Enter">
            显示这段原文
          </button>
        </p>
      )}
      {active.address !== undefined && active.chip === undefined && (
        <p className="note-here">
          <span>链接：{active.address}</span>
          {isWebAddress(active.address) && (
            <a href={active.address} target="_blank" rel="noreferrer noopener">
              在新标签页打开
            </a>
          )}
          <button onClick={() => acts.current.askLink()}>修改</button>
        </p>
      )}
    </div>
  );
}

export default function NoteEditor({ noteId, seat, citing, onOpen }: Shown) {
  const session = useSession(noteId);
  const text = session === undefined ? undefined : textOf(session);
  if (session?.note.state === "failed") {
    return (
      <div className="note-editor">
        <Back />
        <p className="status failed">
          {session.note.reason}
          {session.note.reason !== GONE && (
            <button onClick={() => readAgain(noteId)}>重试</button>
          )}
        </p>
      </div>
    );
  }
  if (session === undefined || text === undefined) {
    return (
      <div className="note-editor">
        <Back />
        <p className="status">正在打开笔记…</p>
      </div>
    );
  }
  return (
    <Writing
      noteId={noteId}
      seat={seat}
      citing={citing}
      onOpen={onOpen}
      session={session}
      start={text}
    />
  );
}
