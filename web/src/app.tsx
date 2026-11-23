import { Component, useState, useSyncExternalStore, type ReactNode } from "react";
import { listItems, readDocument, readEntry, type ItemEntry } from "./client";
import { Library } from "./library";
import { itemLink, linkedItem } from "./links";
import { reasonOf, useLoaded, useWatched } from "./loading";
import { isBeingRead } from "./entry";
import { ReadItem, type ReadEntry } from "./reading";

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
          opening={undefined}
        />
      );
  }
}

function ItemPane({ itemId }: { itemId: string }) {
  const { loaded: entry } = useWatched(() => readEntry(itemId), itemId, isBeingRead);
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

  function openImported(entry: ItemEntry) {
    setImports(imports + 1);
    location.hash = itemLink(entry.id);
  }

  return (
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
    </div>
  );
}
