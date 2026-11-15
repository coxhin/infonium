import { Component, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import {
  listItems,
  readDocument,
  readEntry,
  sourceLink,
  type Anchor,
  type ItemDocument,
  type ItemEntry,
  type PagePlace,
  type TimePlace,
} from "./client";
import { DigestPanel, type Pointer } from "./digest";
import { Library } from "./library";
import { itemLink, linkedItem } from "./links";
import { reasonOf, useLoaded, useWatched } from "./loading";
import { isBeingRead, itemTitle } from "./entry";
import { PageViewer } from "./pages";
import { pagePlaces, placesOf, surfaceOf, timePlaces } from "./places";
import { Player } from "./player";
import { hideAnchor, highlightAnchor, Reader, showAnchor } from "./reader";

// A paged source is shown either as its pages or as the text read from them.
type View = "pages" | "text";
const VIEW_NAMES: Record<View, string> = { pages: "原件", text: "文本" };

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again.
type Pointed = { anchor: Anchor; pages: PagePlace[]; times: TimePlace[] };
const NOWHERE: { pages: PagePlace[]; times: TimePlace[] } = { pages: [], times: [] };

type ReadEntry = ItemEntry & { reading: { kind: "read" } };

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

// What the latest attempt to read an item again has come to, above the document that is kept.
function RereadingNote({ entry }: { entry: ReadEntry }) {
  const attempt = entry.reading.attempt;
  switch (attempt.kind) {
    case "underway":
      return <p className="rereading">正在重新读取，下面是上一次读到的内容。</p>;
    case "failed":
      return (
        <p className="rereading failed">
          重新读取失败，下面是上一次读到的内容。原因：{attempt.reason}
        </p>
      );
    case "settled":
      return null;
  }
}

function ReadItem({ entry, itemDocument }: { entry: ReadEntry; itemDocument: ItemDocument }) {
  const surface = surfaceOf(itemDocument.blocks);
  const [view, setView] = useState<View>("pages");
  const [pointed, setPointed] = useState<Pointed>();
  const textInView = surface !== "page" || view === "text";

  const pointer: Pointer = {
    show(anchor) {
      // Text that is out of view is only marked; bringing it into view would scroll the
      // pages that are on show instead.
      (textInView ? showAnchor : highlightAnchor)(anchor);
      const places = placesOf(itemDocument.blocks, anchor);
      setPointed({ anchor, pages: pagePlaces(places), times: timePlaces(places) });
    },
    hide() {
      hideAnchor();
      setPointed(undefined);
    },
  };

  // A passage pointed at while the text was out of view is brought into view when the text
  // comes back.
  useEffect(() => {
    if (textInView && pointed !== undefined) {
      showAnchor(pointed.anchor);
    }
  }, [textInView]);

  const { pages, times } = pointed ?? NOWHERE;
  return (
    <>
      <main>
        <RereadingNote entry={entry} />
        {surface === "time" && <Player source={sourceLink(entry.id)} pointed={times} />}
        {surface === "page" && (
          <div className="views" role="tablist">
            {(Object.keys(VIEW_NAMES) as View[]).map((name) => (
              <button
                key={name}
                role="tab"
                aria-selected={view === name}
                onClick={() => setView(name)}
              >
                {VIEW_NAMES[name]}
              </button>
            ))}
          </div>
        )}
        {surface === "page" && (
          <div hidden={view !== "pages"}>
            <PageViewer source={sourceLink(entry.id)} pointed={pages} inView={view === "pages"} />
          </div>
        )}
        <div hidden={!textInView}>
          <Reader entry={entry} title={itemTitle(entry)} itemDocument={itemDocument} />
        </div>
      </main>
      <DigestPanel itemId={entry.id} pointer={pointer} />
    </>
  );
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
        <ReadItem key={itemDocument.value.id} entry={entry} itemDocument={itemDocument.value} />
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
