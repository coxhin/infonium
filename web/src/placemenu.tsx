import { useState } from "react";
import type { Folder } from "./client";
import { FolderNameField, type NameDone } from "./foldername";
import { MenuItem, Popup, type Opening } from "./menu";
import { usePress, type Press } from "./pressing";

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

  // A folder made is moved into; a name given up closes the menu. The keyboard's leaving the field
  // for another place in the menu leaves the field as it is; a press outside closes the menu.
  function named(done: NameDone<Folder>) {
    if (done.kind === "answered") {
      placeIn(done.answer.id);
    } else if (done.kind === "given up") {
      onLeave(true);
    }
  }

  // While it asks for a new folder's name, the menu is a dialog, which holds a field and a press.
  if (naming) {
    return (
      <Popup
        key="naming"
        opening={opening}
        role="dialog"
        label={`把“${what}”移到新分类`}
        onLeave={onLeave}
      >
        <FolderNameField asked={{ kind: "new" }} waiting={asking} onSend={onMake} onDone={named} />
        <PlacingSaid press={placing.press} />
      </Popup>
    );
  }
  return (
    <Popup opening={opening} role="menu" label={`移动“${what}”`} onLeave={onLeave}>
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
      <PlacingSaid press={placing.press} />
    </Popup>
  );
}

// What the menu says of a move: that it is under way, or why the service refused it.
function PlacingSaid({ press }: { press: Press }) {
  if (press.state === "asking") {
    return (
      <p className="status" role="status">
        正在移动…
      </p>
    );
  }
  return press.state === "refused" ? (
    <p className="status failed" role="alert">
      没有移动：{press.reason}
    </p>
  ) : null;
}
