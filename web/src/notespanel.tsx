import type { Opener } from "./chat";
import type { Citing } from "./citing";
import type { Seat } from "./seat";

// The notes of the library, in the notebook: a list of them, and the one that is open, in its
// editor. A link of a note that leads to a passage is shown through the column's citing where
// the passage lies in the item on show, and by opening its item otherwise.
export function NotesPanel(_: { seat: Seat | undefined; citing: Citing; onOpen: Opener }) {
  return <p className="status">笔记会在这里：还没有做好。</p>;
}
