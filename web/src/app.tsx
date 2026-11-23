import { Component, useState, useSyncExternalStore, type ReactNode } from "react";
import { listItems, readDocument, readEntry, type ItemEntry } from "./client";
import { Library } from "./library";
import { itemLink, linkedItem } from "./links";
import { reasonOf, useLoaded, useWatched, type Loaded } from "./loading";
import { isBeingRead } from "./entry";
import { ReadItem, type ReadEntry } from "./reading";
import { SeatingProvider, SideColumn, useCannotShow, useSeating } from "./side";

const STILL_READING = "这一篇还在读取，读完后才能打开这段引文。";
const READING_FAILED = "这一篇读取失败，打不开这段引文。";

function onLinkChange(notify: () => void): () => void {
  addEventListener("hashchange", notify);
  return () => removeEventListener("hashchange", notify);
}

function selectedItem(): string | undefined {
  return linkedItem(location.hash);
}

function Notice({ failed, children }: { failed: boolean; children: ReactNode }) {
  return (
    <main>
      <p className={failed ? "status failed" : "status"}>{children}</p>
    </main>
  );
}

// A view of an item that fails while it is on show is replaced by the reason, and the rest of
// the page stays in use.
class PaneBoundary extends Component<{ children: ReactNode }, { reason: string | undefined }> {
  state: { reason: string | undefined } = { reason: undefined };

  static getDerivedStateFromError(error: unknown): { reason: string } {
    return { reason: reasonOf(error) };
  }

  render(): ReactNode {
    return this.state.reason === undefined ? (
      this.props.children
    ) : (
      <Notice failed>显示出错：{this.state.reason}</Notice>
    );
  }
}

function DocumentPane({ entry }: { entry: ReadEntry }) {
  // The document is opened again only when another one has taken its place.
  const itemDocument = useLoaded(() => readDocument(entry.id), entry.reading.document);
  useCannotShow(
    entry.id,
    itemDocument.state === "failed" ? `打不开这一篇的原文：${itemDocument.reason}` : undefined,
  );
  switch (itemDocument.state) {
    case "loading":
      return <Notice failed={false}>正在打开…</Notice>;
    case "failed":
      return <Notice failed>{itemDocument.reason}</Notice>;
    case "ready":
      return (
        <ReadItem
          key={itemDocument.value.id}
          entry={entry}
          itemDocument={itemDocument.value}
        />
      );
  }
}

// Why an item, as far as it is loaded, cannot be shown; nothing while it may yet be.
function unshownReason(entry: Loaded<ItemEntry>): string | undefined {
  if (entry.state === "failed") {
    return `打不开这一篇：${entry.reason}`;
  }
  if (entry.state === "loading" || entry.value.reading.kind === "read") {
    return undefined;
  }
  return entry.value.reading.attempt.kind === "failed" ? READING_FAILED : STILL_READING;
}

function ItemPane({ itemId }: { itemId: string }) {
  const { loaded: entry } = useWatched(() => readEntry(itemId), itemId, isBeingRead);
  useCannotShow(itemId, unshownReason(entry));
  if (entry.state === "loading") {
    return <Notice failed={false}>正在打开…</Notice>;
  }
  if (entry.state === "failed") {
    return <Notice failed>{entry.reason}</Notice>;
  }
  const { reading } = entry.value;
  if (reading.kind === "read") {
    return <DocumentPane entry={{ ...entry.value, reading }} />;
  }
  switch (reading.attempt.kind) {
    case "underway":
      return <Notice failed={false}>正在读取，PDF 和录音可能需要几分钟…</Notice>;
    case "failed":
      return <Notice failed>读取失败：{reading.attempt.reason}</Notice>;
  }
}

export function App() {
  const selected = useSyncExternalStore(onLinkChange, selectedItem);
  const [imports, setImports] = useState(0);
  const entries = useWatched(listItems, String(imports), (listed) => listed.some(isBeingRead));
  const { seating, seated, unshown, pointer } = useSeating();

  // The column shows the passage a citation opens the item for, once the item's reader is on
  // show.
  function openItem(itemId: string) {
    location.hash = itemLink(itemId);
  }

  function openImported(entry: ItemEntry) {
    setImports(imports + 1);
    location.hash = itemLink(entry.id);
  }

  return (
    <SeatingProvider value={seating}>
      <div className="app">
        <Library
          entries={entries.loaded}
          interrupted={entries.interrupted}
          selected={selected}
          onImported={openImported}
        />
        {selected === undefined ? (
          <Notice failed={false}>粘贴一个网址或上传一个文件，或从左侧打开一篇。</Notice>
        ) : (
          // An import may have started the open item's reading again, so the pane is rebuilt
          // after every import and reads the item again.
          <PaneBoundary key={`${selected}:${imports}`}>
            <ItemPane itemId={selected} />
          </PaneBoundary>
        )}
        <SideColumn
          selected={selected}
          seated={seated}
          unshown={unshown}
          pointer={pointer}
          onOpen={openItem}
        />
      </div>
    </SeatingProvider>
  );
}
