import { Component, useEffect, useState, type ReactNode } from "react";
import {
  listItems,
  readAgain,
  readDocument,
  readRevisionDocument,
  type ItemEntry,
  type ItemRevisions,
  type Revision,
} from "./client";
import { Library } from "./library";
import { itemLink, linkedItem, linkOf, useLink } from "./links";
import { reasonOf, useLoaded, useWatched, type Loaded } from "./loading";
import { isBeingRead } from "./entry";
import { useRevisions, useWatchedEntry } from "./itemwatch";
import { ReadItem, type ReadEntry } from "./reading";
import { EarlierLine, NOT_REREADING, RevisionHead, type Rereading } from "./revisions";
import { SeatingProvider, SideColumn, useCannotShow, useSeating } from "./side";

const STILL_READING = "这一篇还在读取，读完后才能打开这段引文。";
const READING_FAILED = "这一篇读取失败，打不开这段引文。";

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

// A revision of the item that cannot be shown, said in the reader's place and to the column,
// with the way to what the item holds now.
function UnshownRevision({ itemId, reason }: { itemId: string; reason: string }) {
  useCannotShow(itemId, reason);
  return (
    <Notice failed>
      {reason} <a href={itemLink(itemId)}>打开现在的版本</a>
    </Notice>
  );
}

// The document the item holds now. It is opened again only when another one has taken its
// place.
function HoldingPane({ entry, link, head }: { entry: ReadEntry; link: string; head: ReactNode }) {
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
          revision={{ kind: "holding" }}
          link={link}
          head={head}
        />
      );
  }
}

// An earlier revision's document, as its text alone under the line that says which it is.
function EarlierPane({
  entry,
  earlier,
  holding,
  link,
  head,
}: {
  entry: ReadEntry;
  earlier: Revision;
  holding: Revision | undefined;
  link: string;
  head: ReactNode;
}) {
  const itemDocument = useLoaded(
    () => readRevisionDocument(entry.id, earlier.number),
    `${earlier.number}:${earlier.document}`,
  );
  useCannotShow(
    entry.id,
    itemDocument.state === "failed"
      ? `打不开第 ${earlier.number} 次读取的版本：${itemDocument.reason}`
      : undefined,
  );
  switch (itemDocument.state) {
    case "loading":
      return <Notice failed={false}>正在打开…</Notice>;
    case "failed":
      return <Notice failed>{itemDocument.reason}</Notice>;
    case "ready":
      return (
        <ReadItem
          key={`${itemDocument.value.id}:earlier`}
          entry={entry}
          itemDocument={itemDocument.value}
          revision={{
            kind: "earlier",
            holding: entry.reading.document,
            line: <EarlierLine itemId={entry.id} earlier={earlier} holding={holding} />,
          }}
          link={link}
          head={head}
        />
      );
  }
}

// What the link opens of a read item: the document it holds now, which its plain link and the
// link of the revision that holds it both open, or an earlier revision.
function RevisionPane({
  entry,
  revision,
  revisions,
  head,
}: {
  entry: ReadEntry;
  revision: number | undefined;
  revisions: Loaded<ItemRevisions>;
  head: (shownDocument: string) => ReactNode;
}) {
  const link = linkOf({ itemId: entry.id, revision });
  if (revision === undefined) {
    return <HoldingPane entry={entry} link={link} head={head(entry.reading.document)} />;
  }
  switch (revisions.state) {
    case "loading":
      return <Notice failed={false}>正在打开…</Notice>;
    case "failed":
      return (
        <UnshownRevision itemId={entry.id} reason={`读不到这一篇的版本：${revisions.reason}`} />
      );
    case "ready":
      break;
  }
  const named = revisions.value.revisions.find((held) => held.number === revision);
  if (named === undefined) {
    return (
      <UnshownRevision itemId={entry.id} reason={`这一篇没有第 ${revision} 次读取的版本。`} />
    );
  }
  if (named.document === entry.reading.document) {
    return <HoldingPane entry={entry} link={link} head={head(entry.reading.document)} />;
  }
  return (
    <EarlierPane
      entry={entry}
      earlier={named}
      holding={revisions.value.revisions.find(
        (held) => held.document === entry.reading.document,
      )}
      link={link}
      head={head(named.document)}
    />
  );
}

// A read item, with its revisions and a reading of it the reader may start. The revisions are
// read again whenever the item comes to hold another document or a reading of it settles.
function ReadPane({
  entry,
  revision,
  onWatch,
}: {
  entry: ReadEntry;
  revision: number | undefined;
  onWatch: (entry: ItemEntry) => void;
}) {
  const { reading } = entry;
  const revisions = useRevisions(entry.id, `${reading.document}:${reading.attempt.kind}`);
  const [rereading, setRereading] = useState<Rereading>(NOT_REREADING);

  // A reading the reader started, once it has settled, is told once; the reader shows what the
  // item holds now, even where an earlier revision was on show.
  useEffect(() => {
    if (rereading.state !== "reading" || reading.attempt.kind === "underway") {
      return;
    }
    if (reading.attempt.kind === "failed") {
      setRereading(NOT_REREADING);
      return;
    }
    setRereading({ ...rereading, state: "settled", after: reading.document });
    if (linkedItem(location.hash)?.revision !== undefined) {
      location.hash = itemLink(entry.id);
    }
  }, [rereading, reading]);

  async function startReading() {
    const before = reading.document;
    const known =
      revisions.state === "ready" ? revisions.value.revisions.map((held) => held.document) : [];
    setRereading({ state: "asking" });
    try {
      const started = await readAgain(entry.id);
      setRereading({ state: "reading", before, known });
      onWatch(started);
    } catch (error) {
      setRereading({ state: "refused", reason: reasonOf(error) });
    }
  }

  function head(shownDocument: string): ReactNode {
    return (
      <RevisionHead
        itemId={entry.id}
        revisions={revisions.state === "ready" ? revisions.value : undefined}
        shownDocument={shownDocument}
        underway={reading.attempt.kind === "underway"}
        rereading={rereading}
        onReadAgain={() => void startReading()}
        onTold={() => setRereading(NOT_REREADING)}
      />
    );
  }

  return <RevisionPane entry={entry} revision={revision} revisions={revisions} head={head} />;
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

function ItemPane({
  itemId,
  revision,
  onReading,
}: {
  itemId: string;
  revision: number | undefined;
  onReading: () => void;
}) {
  const { loaded: entry, watchFrom } = useWatchedEntry(itemId);
  useCannotShow(itemId, unshownReason(entry));
  if (entry.state === "loading") {
    return <Notice failed={false}>正在打开…</Notice>;
  }
  if (entry.state === "failed") {
    return <Notice failed>{entry.reason}</Notice>;
  }
  const { reading } = entry.value;
  if (reading.kind === "read") {
    return (
      <ReadPane
        entry={{ ...entry.value, reading }}
        revision={revision}
        onWatch={(started) => {
          watchFrom(started);
          onReading();
        }}
      />
    );
  }
  switch (reading.attempt.kind) {
    case "underway":
      return <Notice failed={false}>正在读取，PDF 和录音可能需要几分钟…</Notice>;
    case "failed":
      return <Notice failed>读取失败：{reading.attempt.reason}</Notice>;
  }
}

export function App() {
  const linked = linkedItem(useLink());
  const selected = linked?.itemId;
  const [imports, setImports] = useState(0);
  // A reading started again in the reader is watched in the list as an import is.
  const [readings, setReadings] = useState(0);
  const entries = useWatched(listItems, `${imports}:${readings}`, (listed) =>
    listed.some(isBeingRead),
  );
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
            <ItemPane
              itemId={selected}
              revision={linked?.revision}
              onReading={() => setReadings((before) => before + 1)}
            />
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
