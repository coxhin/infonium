import { useState } from "react";
import type { Folder } from "./client";
import { FolderNameField } from "./foldername";
import { MenuItem, Popup, type Opening } from "./menu";
import { usePress } from "./pressing";

// The menu of a subscription or of a hand-added item that moves it: to one of the folders, the
// one it is in marked; to a folder made here, whose name the menu asks for in its place; or out
// of the folder it is in. The menu stays open while the service is asked, and says there why it
// refused, so that another choice can be made.
export function PlaceMenu({
  opening,
  what,
  folders,
  current,
  onPlace,
  onMake,
  onLeave,
}: {
  opening: Opening;
  // What is moved, as the reader knows it.
  what: string;
  folders: Folder[];
  current: string | null;
  onPlace: (folderId: string | null) => Promise<void>;
  onMake: (name: string) => Promise<Folder>;
  onLeave: (refocus: boolean) => void;
}) {
  const [naming, setNaming] = useState(false);
  const placing = usePress();
  const asking = placing.press.state === "asking";

  function placeIn(folderId: string | null) {
    if (folderId === current) {
      onLeave(true);
      return;
    }
    placing.ask(
      () => onPlace(folderId),
      () => onLeave(true),
    );
  }

  return (
    <Popup opening={opening} role="menu" label={`移动“${what}”`} onLeave={onLeave}>
      {naming ? (
        <FolderNameField
          asked={{ kind: "new" }}
          waiting={asking}
          onSend={onMake}
          onDone={(done) => (done.kind === "answered" ? placeIn(done.answer.id) : onLeave(true))}
          onBlur={() => undefined}
        />
      ) : (
        <>
          <p className="menu-heading" aria-hidden="true">
            移到分类…
          </p>
          <div role="group" aria-label="移到分类">
            {folders.map((folder) => (
              <MenuItem
                key={folder.id}
                checked={folder.id === current}
                disabled={asking}
                onChoose={() => placeIn(folder.id)}
              >
                <span className="menu-name" dir="auto" title={folder.name}>
                  {folder.name}
                </span>
              </MenuItem>
            ))}
            <MenuItem disabled={asking} onChoose={() => setNaming(true)}>
              新建分类…
            </MenuItem>
          </div>
          {current !== null && (
            <>
              <div role="separator" />
              <MenuItem disabled={asking} onChoose={() => placeIn(null)}>
                移出分类
              </MenuItem>
            </>
          )}
        </>
      )}
      {asking && (
        <p className="status" role="status">
          正在移动…
        </p>
      )}
      {placing.press.state === "refused" && (
        <p className="status failed" role="alert">
          没有移动：{placing.press.reason}
        </p>
      )}
    </Popup>
  );
}
