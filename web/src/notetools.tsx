import { useEditorState, type Editor } from "@tiptap/react";
import { useState, type KeyboardEvent, type Ref } from "react";

// The controls above a note's text: each kind of text by a press, with its keys and the
// Markdown that makes it named in its title.

export const MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
const LINK_TOOL = "链接";
const SINK_TOOL = "缩进一层";
const LIFT_TOOL = "退出一层";

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
    name: SINK_TOOL,
    glyph: "⇥",
    keys: "Tab",
    typed: undefined,
    run: (editor) => editor.chain().focus().sinkListItem("listItem").run(),
    active: undefined,
  },
  {
    name: LIFT_TOOL,
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

const ROVING_KEYS: Record<string, ((at: number, count: number) => number) | undefined> = {
  ArrowRight: (at, count) => (at + 1) % count,
  ArrowLeft: (at, count) => (at - 1 + count) % count,
  Home: () => 0,
  End: (_, count) => count - 1,
};

// The toolbar is one stop of the keyboard's Tab; the arrows, Home and End move within it.
function rove(event: KeyboardEvent<HTMLDivElement>) {
  const move = ROVING_KEYS[event.key];
  if (move === undefined) {
    return;
  }
  const buttons = [
    ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
  ];
  const at = buttons.findIndex((button) => button === document.activeElement);
  buttons[move(Math.max(at, 0), buttons.length)]?.focus();
  event.preventDefault();
}

function controlTitle(control: Control): string {
  const ways = [
    control.keys === undefined ? undefined : `按 ${control.keys}`,
    control.typed === undefined ? undefined : `或输入 ${control.typed}`,
  ].filter((way) => way !== undefined);
  return ways.length === 0 ? control.name : `${control.name}（${ways.join("，")}）`;
}

function keyNames(keys: string | undefined): string | undefined {
  return keys?.replace("⌘", "Meta").replace("Ctrl", "Control");
}

export function NoteTools({
  editor,
  linking,
  onLink,
  ref,
}: {
  editor: Editor;
  linking: boolean;
  onLink: () => void;
  ref: Ref<HTMLDivElement>;
}) {
  const [toolAt, setToolAt] = useState(CONTROLS[0]?.name ?? LINK_TOOL);
  const active = useEditorState({
    editor,
    selector: ({ editor: shown }) => ({
      controls: CONTROLS.map((control) => control.active?.(shown)),
      sinkable: shown.can().sinkListItem("listItem"),
      liftable: shown.can().liftListItem("listItem"),
    }),
  });

  function disabled(tool: string): boolean {
    return (tool === SINK_TOOL && !active.sinkable) || (tool === LIFT_TOOL && !active.liftable);
  }
  // The control the keyboard's Tab stops at: the one last focused, while it can be used.
  const stop = disabled(toolAt) ? CONTROLS[0]?.name : toolAt;

  return (
    <div className="note-tools" role="toolbar" aria-label="格式" ref={ref} onKeyDown={rove}>
      {CONTROLS.map((control, index) => (
        <button
          key={control.name}
          aria-label={control.name}
          title={controlTitle(control)}
          aria-keyshortcuts={keyNames(control.keys)}
          aria-pressed={control.active === undefined ? undefined : active.controls[index]}
          disabled={disabled(control.name)}
          tabIndex={control.name === stop ? 0 : -1}
          onFocus={() => setToolAt(control.name)}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => control.run(editor)}
        >
          {control.glyph}
        </button>
      ))}
      <button
        aria-label={LINK_TOOL}
        title={`链接（按 ${MOD}+K，或输入 [文字](网址)）`}
        aria-keyshortcuts={keyNames(`${MOD}+K`)}
        aria-pressed={linking}
        tabIndex={LINK_TOOL === stop ? 0 : -1}
        onFocus={() => setToolAt(LINK_TOOL)}
        onMouseDown={(event) => event.preventDefault()}
        onClick={onLink}
      >
        链接
      </button>
    </div>
  );
}
