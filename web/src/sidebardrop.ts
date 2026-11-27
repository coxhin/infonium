import { useEffect, useState, type DragEvent } from "react";
import { requestReason } from "./client";
import { draggedOver, endDrag, type Dragged } from "./dragging";
import type { Placer } from "./folders";
import { selectionOfKey } from "./sidebarselection";

// A drag of the page's over the sidebar: where what is dragged would go, why the service refused
// the last drop, and the handlers of the sidebar's element.
export type SidebarDrop = {
  // The folder what is dragged would go into, none where it would leave its folder, and nothing
  // where no drag is over the sidebar.
  over: string | null | undefined;
  // Why the service refused the last drop, said at the sidebar's head, which stays in view
  // whether the folder dropped on does or not.
  refusal: string | undefined;
  onDragOver: (event: DragEvent<HTMLElement>) => void;
  onDragLeave: (event: DragEvent<HTMLElement>) => void;
  onDrop: (event: DragEvent<HTMLElement>) => void;
};

// Where what is dragged over the sidebar goes: into the folder it is over, a subscription under a
// folder counting as that folder, and anywhere else out of its folder; nowhere where it is
// already.
function dropOf(
  event: DragEvent<HTMLElement>,
): { dragged: Dragged; folderId: string | null } | undefined {
  const dragged = draggedOver(event);
  if (dragged === undefined || !(event.target instanceof Element)) {
    return undefined;
  }
  const over = selectionOfKey(event.target.closest<HTMLElement>("li.folder")?.dataset.key ?? "");
  const folderId = over?.kind === "folder" ? over.folderId : null;
  return dragged.folder === folderId ? undefined : { dragged, folderId };
}

// A subscription or an item dragged onto the sidebar: dropped on a folder, it goes into that
// folder; dropped anywhere else, out of the folder it is in.
export function useSidebarDrop(placer: Placer): SidebarDrop {
  const [over, setOver] = useState<string | null>();
  const [refusal, setRefusal] = useState<string>();

  // A drag that ends anywhere leaves nothing marked.
  useEffect(() => {
    const ended = () => setOver(undefined);
    addEventListener("dragend", ended);
    addEventListener("drop", ended);
    return () => {
      removeEventListener("dragend", ended);
      removeEventListener("drop", ended);
    };
  }, []);

  function onDragOver(event: DragEvent<HTMLElement>) {
    const drop = dropOf(event);
    setOver(drop?.folderId);
    if (drop !== undefined) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    }
  }

  function onDragLeave(event: DragEvent<HTMLElement>) {
    const next = event.relatedTarget;
    if (!(next instanceof Node && event.currentTarget.contains(next))) {
      setOver(undefined);
    }
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    const drop = dropOf(event);
    setOver(undefined);
    endDrag();
    if (drop === undefined) {
      return;
    }
    event.preventDefault();
    const { dragged, folderId } = drop;
    const doing =
      folderId === null
        ? "没有移出分类"
        : `没有移到“${placer.folders.find((folder) => folder.id === folderId)?.name ?? ""}”`;
    setRefusal(undefined);
    placer
      .place({
        folder: folderId,
        subscriptions: dragged.kind === "subscription" ? [dragged.id] : [],
        items: dragged.kind === "item" ? [dragged.id] : [],
      })
      .catch((error: unknown) => setRefusal(`${doing}：${requestReason(error)}`));
  }

  return { over, refusal, onDragOver, onDragLeave, onDrop };
}
