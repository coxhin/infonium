import { useState } from "react";
import type { Folder } from "./client";
import { MenuItem, Popup, type Opening } from "./menu";
import { usePress } from "./pressing";

// What a folder holds, as the question before dissolving it says it.
export type Held = { subscriptions: number; items: number };

function heldText(folder: Folder, held: Held): string {
  const parts = [
    held.subscriptions > 0 ? `${held.subscriptions} 个订阅` : undefined,
    held.items > 0 ? `${held.items} 篇手动添加的文章` : undefined,
  ].filter((part) => part !== undefined);
  return parts.length === 0
    ? `解散分类“${folder.name}”？它是空的。`
    : `解散分类“${folder.name}”？其中的 ${parts.join("和 ")} 都会留下，只是不再属于任何分类。`;
}

// A folder's menu: renaming it, which the sidebar does in the folder's place, and dissolving
// it, asked once more in the menu's place with what stays. A refusal is said in the question,
// which stays for another try.
export function FolderMenu({
  opening,
  folder,
  held,
  onRename,
  onDissolve,
  onLeave,
}: {
  opening: Opening;
  folder: Folder;
  held: Held;
  onRename: () => void;
  onDissolve: () => Promise<void>;
  onLeave: (refocus: boolean) => void;
}) {
  const [asking, setAsking] = useState(false);
  const { press, ask } = usePress();
  if (!asking) {
    return (
      <Popup opening={opening} role="menu" label={`分类“${folder.name}”`} onLeave={onLeave}>
        <MenuItem onChoose={onRename}>重命名</MenuItem>
        <MenuItem onChoose={() => setAsking(true)}>解散分类…</MenuItem>
      </Popup>
    );
  }
  const waiting = press.state === "asking";
  return (
    <Popup key="question" opening={opening} role="dialog" label="解散分类" onLeave={onLeave}>
      <p>{heldText(folder, held)}</p>
      <div className="choices">
        <button
          className="danger"
          aria-disabled={waiting}
          onClick={() => ask(onDissolve, () => onLeave(true))}
        >
          {waiting ? "正在解散…" : "解散"}
        </button>
        <button data-autofocus onClick={() => onLeave(true)}>
          保留
        </button>
      </div>
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          没有解散：{press.reason}
        </p>
      )}
    </Popup>
  );
}
