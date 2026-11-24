import { useMemo, useRef, type ReactNode, type RefObject } from "react";
import { anchorsOf, type Highlight, type ItemHighlights as HeldHighlights } from "./client";
import type { Opener } from "./chat";
import type { Citing } from "./citing";
import { choseInList, HighlightEntry, type EntryActions } from "./highlightentry";
import {
  openHighlight,
  useHighlights,
  useLibraryHighlights,
  useOpenedHighlight,
  type Opened,
} from "./highlights";
import { itemLink, revisionLink } from "./links";
import type { Loaded } from "./loading";
import type { Seat } from "./seat";

const HOW_TO_MARK =
  "在原文里选中一段文字，按出现的「高亮」就能标上；标上的段落会列在这里，可以写评论，也可以记到笔记。";

// What an entry asks of its list, made once and reading the latest of what it needs, so that
// a list drawn again does not draw its entries again. A list given an opener opens the item a
// highlight lies in; one without shows the highlight in the item on show.
function useEntryActions(
  current: () => { citing: Citing; onOpen: Opener | undefined },
  list: RefObject<HTMLElement | null>,
): EntryActions {
  const latest = useRef(current);
  latest.current = current;
  return useMemo<EntryActions>(
    () => ({
      press(highlight) {
        const { citing, onOpen } = latest.current();
        if (highlight.standing.kind !== "found") {
          return;
        }
        const anchors = anchorsOf(highlight.standing.passage);
        openHighlight(
          choseInList({ itemId: highlight.item, highlightId: highlight.id, commenting: false }),
        );
        if (onOpen === undefined) {
          citing.point(anchors);
        } else {
          onOpen(highlight.item, anchors);
        }
      },
      openRevision(highlight) {
        const { citing } = latest.current();
        openHighlight(
          choseInList({ itemId: highlight.item, highlightId: highlight.id, commenting: false }),
        );
        citing.awaitOpening(anchorsOf(highlight.passage), highlight.item);
        location.hash = revisionLink(highlight.item, highlight.revision);
      },
      removed() {
        list.current?.focus();
      },
    }),
    [],
  );
}

// The entries of one item's highlights, in the order the service gives them.
function Entries({
  highlights,
  citing,
  pressable,
  opened,
  shown,
  actions,
}: {
  highlights: Highlight[];
  citing: Citing;
  pressable: boolean;
  opened: Opened | undefined;
  shown: boolean;
  actions: EntryActions;
}) {
  return highlights.map((highlight) => {
    const isOpened = opened?.itemId === highlight.item && opened.highlightId === highlight.id;
    const first =
      highlight.standing.kind === "found" ? highlight.standing.passage.anchors[0] : undefined;
    return (
      <HighlightEntry
        key={highlight.id}
        highlight={highlight}
        pressed={pressable && first !== undefined && citing.isPressed(first)}
        opened={isOpened}
        opening={isOpened && shown ? opened : undefined}
        actions={actions}
      />
    );
  });
}

// A list's place in the notebook, which keeps the keyboard when an entry in it goes away.
function ListPlace({
  label,
  list,
  children,
}: {
  label: string;
  list: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  return (
    <div className="highlight-list" ref={list} tabIndex={-1} role="region" aria-label={label}>
      {children}
    </div>
  );
}

function ItemHighlightsBody({
  documentId,
  highlights,
  children,
}: {
  documentId: string | undefined;
  highlights: Loaded<Highlight[]>;
  children: (highlights: Highlight[]) => ReactNode;
}) {
  if (documentId === undefined) {
    return <p className="status">原文打开后，这里列出它的高亮。</p>;
  }
  switch (highlights.state) {
    case "loading":
      return <p className="status">正在读取高亮…</p>;
    case "failed":
      return <p className="status failed">读不到这一篇的高亮：{highlights.reason}</p>;
    case "ready":
      return highlights.value.length === 0 ? (
        <p className="status">这一篇还没有高亮。{HOW_TO_MARK}</p>
      ) : (
        children(highlights.value)
      );
  }
}

// The highlights of the item on show. Pressing one shows it in the reader.
export function ItemHighlights({
  itemId,
  seat,
  citing,
  shown,
}: {
  itemId: string;
  seat: Seat | undefined;
  citing: Citing;
  shown: boolean;
}) {
  // Until the reader shows the item, the document its highlights stand in is not known.
  const documentId = seat?.itemId === itemId ? seat.documentId : undefined;
  const highlights = useHighlights(itemId, documentId);
  const opened = useOpenedHighlight();
  const list = useRef<HTMLDivElement>(null);
  const actions = useEntryActions(() => ({ citing, onOpen: undefined }), list);
  return (
    <ListPlace label="这一篇的高亮" list={list}>
      <ItemHighlightsBody documentId={documentId} highlights={highlights}>
        {(value) => (
          <ul className="highlights">
            <Entries
              highlights={value}
              citing={citing}
              pressable
              opened={opened}
              shown={shown}
              actions={actions}
            />
          </ul>
        )}
      </ItemHighlightsBody>
    </ListPlace>
  );
}

function LibraryHighlightsBody({
  library,
  children,
}: {
  library: Loaded<HeldHighlights[]>;
  children: (library: HeldHighlights[]) => ReactNode;
}) {
  switch (library.state) {
    case "loading":
      return <p className="status">正在读取高亮…</p>;
    case "failed":
      return <p className="status failed">读不到库里的高亮：{library.reason}</p>;
    case "ready":
      return library.value.length === 0 ? (
        <p className="status">库里还没有高亮。打开一篇，{HOW_TO_MARK}</p>
      ) : (
        children(library.value)
      );
  }
}

// Every item's highlights, by item, with no item open; read again each time it comes on show.
// Pressing a highlight opens its item at it.
export function LibraryHighlights({
  citing,
  shown,
  onOpen,
}: {
  citing: Citing;
  shown: boolean;
  onOpen: Opener;
}) {
  const library = useLibraryHighlights(shown);
  const opened = useOpenedHighlight();
  const list = useRef<HTMLDivElement>(null);
  const actions = useEntryActions(() => ({ citing, onOpen }), list);
  return (
    <ListPlace label="库里各篇的高亮" list={list}>
      <LibraryHighlightsBody library={library}>
        {(value) =>
          value.map((held) => (
            <section key={held.item} aria-labelledby={`highlights-of-${held.item}`}>
              <h3 id={`highlights-of-${held.item}`}>
                <a href={itemLink(held.item)}>{held.title}</a>
                <small>{held.highlights.length} 条</small>
              </h3>
              <ul className="highlights">
                <Entries
                  highlights={held.highlights}
                  citing={citing}
                  pressable={false}
                  opened={opened}
                  shown={shown}
                  actions={actions}
                />
              </ul>
            </section>
          ))
        }
      </LibraryHighlightsBody>
    </ListPlace>
  );
}
