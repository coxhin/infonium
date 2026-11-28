import { Component, useEffect, useRef, useState, type ReactNode } from "react";
import {
  isUnanswered,
  listItems,
  readAgain,
  readDocument,
  readEntry,
  readRevisionDocument,
  seeItem,
  type ItemDocument,
  type ItemEntry,
  type ItemRevisions,
  type Revision,
} from "./client";
import { ArrivedPages } from "./arrival";
import { fetchFormulas, formulasReadyFor } from "./formula";
import { fetchRenderer } from "./pages";
import { ListColumn } from "./listcolumn";
import { itemLink, linkedItem, linkOf, useLink } from "./links";
import { LIST_INTERVAL_MS, reasonOf, useWatched, type Loaded, type Watched } from "./loading";
import { isBeingRead } from "./entry";
import { useRevisions } from "./itemrevisions";
import { useBrowsing } from "./browsing";
import { ReadItem, type ReadEntry } from "./reading";
import { EarlierLine, NOT_REREADING, RevisionHead, type Rereading } from "./revisions";
import type { Seat } from "./seat";
import { SeatingProvider, SideColumn, useCannotShow, useSeating } from "./side";
import { Sidebar } from "./sidebar";
import { useSidebarLayout } from "./sidebarlayout";
import { PDF_MEDIA_TYPE } from "./views";

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

// What a link opens of a read item: the document the item holds now, which its plain link and
// the link of the revision that holds it both open; an earlier revision; a revision that cannot
// be shown; or nothing yet, while the revisions the link names a number of are read.
type Shown =
  | { kind: "holding"; document: string; link: string }
  | { kind: "earlier"; earlier: Revision; holding: Revision | undefined; link: string };
type Wanted = Shown | { kind: "unshown"; reason: string } | { kind: "pending" };

function wantedOf(
  entry: ReadEntry,
  revision: number | undefined,
  revisions: Loaded<ItemRevisions>,
): Wanted {
  const link = linkOf({ itemId: entry.id, revision });
  const holding: Wanted = { kind: "holding", document: entry.reading.document, link };
  if (revision === undefined) {
    return holding;
  }
  switch (revisions.state) {
    case "loading":
      return { kind: "pending" };
    case "failed":
      return { kind: "unshown", reason: `读不到这一篇的版本：${revisions.reason}` };
    case "ready":
      break;
  }
  const named = revisions.value.revisions.find((held) => held.number === revision);
  if (named === undefined) {
    return { kind: "unshown", reason: `这一篇没有第 ${revision} 次读取的版本。` };
  }
  if (named.document === entry.reading.document) {
    return holding;
  }
  return {
    kind: "earlier",
    earlier: named,
    holding: revisions.value.revisions.find((held) => held.document === entry.reading.document),
    link,
  };
}

function shownKey(shown: Shown): string {
  return shown.kind === "holding"
    ? `holding:${shown.document}`
    : `earlier:${shown.earlier.number}:${shown.earlier.document}`;
}

function shownDocument(shown: Shown): string {
  return shown.kind === "holding" ? shown.document : shown.earlier.document;
}

// A document of the item that the reader has opened, which of the item's documents it is, and
// the document the item held when it was opened.
type Opened = { shown: Shown; itemDocument: ItemDocument; holding: string };

// The reader of a read item. A document of another revision, or the document the item comes
// to hold after a new reading, the same one included, is loaded while the reader on show
// stays, so that the column beside it never sees the reader gone: the seat changes its
// document and the document the item holds together, when the new one is on show. Meanwhile
// the head already lists the revisions there are now, and names the document on show by its
// place among them. A reader opened by one link and shown again by another link to the same
// document, such as the plain link and that of the revision the item holds now, is seated
// again with the link the window is at, for a passage awaited there.
function DocumentPane({
  entry,
  wanted,
  head,
}: {
  entry: ReadEntry;
  wanted: Shown | undefined;
  head: (shownDocument: string, earlier: boolean) => ReactNode;
}) {
  const [opened, setOpened] = useState<Opened>();
  const [failed, setFailed] = useState<{ key: string; reason: string }>();
  const key = wanted === undefined ? undefined : shownKey(wanted);
  // A reading that settles into the document on show, as one of a page whose text is unchanged,
  // may have given figures of it the pictures they lacked, so the document is loaded again once
  // a reading settles; while one is under way, the document on show stays.
  const underway = entry.reading.attempt.kind === "underway";
  useEffect(() => {
    const onShow = opened !== undefined && shownKey(opened.shown) === key;
    if (wanted === undefined || (underway && onShow)) {
      return undefined;
    }
    let current = true;
    const holding = entry.reading.document;
    // A PDF's pages are drawn by pdf.js, and a paper often holds formulas, so both are fetched
    // as its document is, not after it. A failure is told by the pages.
    if (entry.media === "pdf") {
      fetchRenderer().catch(() => undefined);
      void fetchFormulas();
    }
    const loading = (
      wanted.kind === "holding"
        ? readDocument(entry.id)
        : readRevisionDocument(entry.id, wanted.earlier.number)
    ).then(async (itemDocument) => {
      await formulasReadyFor(itemDocument);
      return itemDocument;
    });
    loading.then(
      (itemDocument) => current && setOpened({ shown: wanted, itemDocument, holding }),
      (error: unknown) =>
        current &&
        setFailed({
          key: shownKey(wanted),
          reason:
            wanted.kind === "holding"
              ? `打不开这一篇的原文：${reasonOf(error)}`
              : `打不开第 ${wanted.earlier.number} 次读取的版本：${reasonOf(error)}`,
        }),
    );
    return () => {
      current = false;
    };
  }, [key, underway]);
  const failure = failed !== undefined && failed.key === key ? failed.reason : undefined;
  useCannotShow(entry.id, failure);
  if (failure !== undefined) {
    return <Notice failed>{failure}</Notice>;
  }
  if (opened === undefined) {
    return <Notice failed={false}>正在打开…</Notice>;
  }
  const { shown, itemDocument, holding } = opened;
  const link =
    wanted !== undefined && shownKey(wanted) === shownKey(shown) ? wanted.link : shown.link;
  return (
    <ReadItem
      key={shownKey(shown)}
      entry={entry}
      itemDocument={itemDocument}
      revision={
        shown.kind === "holding"
          ? { kind: "holding" }
          : {
              kind: "earlier",
              holding,
              line: (
                <EarlierLine itemId={entry.id} earlier={shown.earlier} holding={shown.holding} />
              ),
            }
      }
      link={link}
      head={head(shownDocument(shown), shown.kind === "earlier")}
    />
  );
}

// Where a reading of the item stands when the reader opens it: one found under way is watched
// as one the reader started, though what the item held before it is told only as far as the
// revisions read meanwhile tell it.
function rereadingAtOpening(reading: ReadEntry["reading"]): Rereading {
  return reading.attempt.kind === "underway"
    ? { state: "reading", before: reading.document, known: undefined }
    : NOT_REREADING;
}

// A read item, with its revisions and a reading of it the reader may start. The revisions are
// read again whenever the item comes to hold another document or a reading of it settles.
function ReadPane({
  entry,
  revision,
  interrupted,
  onWatch,
}: {
  entry: ReadEntry;
  revision: number | undefined;
  interrupted: boolean;
  onWatch: (entry: ItemEntry) => void;
}) {
  const { reading } = entry;
  const revisions = useRevisions(entry.id, `${reading.document}:${reading.attempt.kind}`);
  const [rereading, setRereading] = useState<Rereading>(() => rereadingAtOpening(reading));

  // A reading found under way learns the revisions there were before it, once they are read.
  useEffect(() => {
    if (
      rereading.state === "reading" &&
      rereading.known === undefined &&
      reading.attempt.kind === "underway" &&
      revisions.state === "ready"
    ) {
      setRereading({ ...rereading, known: revisions.value.revisions.map((held) => held.document) });
    }
  }, [rereading, reading, revisions]);

  // A reading that has settled is told once; the reader shows what the item holds now, even
  // where an earlier revision was on show.
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
      revisions.state === "ready"
        ? revisions.value.revisions.map((held) => held.document)
        : undefined;
    setRereading({ state: "asking" });
    try {
      const started = await readAgain(entry.id);
      setRereading({ state: "reading", before, known });
      onWatch(started);
    } catch (error) {
      setRereading({ state: "refused", reason: reasonOf(error) });
    }
  }

  function head(shown: string, earlier: boolean): ReactNode {
    return (
      <RevisionHead
        itemId={entry.id}
        revisions={revisions}
        shownDocument={shown}
        earlier={earlier}
        underway={reading.attempt.kind === "underway"}
        interrupted={interrupted}
        rereading={rereading}
        onReadAgain={() => void startReading()}
        onTold={() => setRereading(NOT_REREADING)}
      />
    );
  }

  const wanted = wantedOf(entry, revision, revisions);
  if (wanted.kind === "unshown") {
    return <UnshownRevision itemId={entry.id} reason={wanted.reason} />;
  }
  return (
    <DocumentPane
      entry={entry}
      wanted={wanted.kind === "pending" ? undefined : wanted}
      head={head}
    />
  );
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
  const {
    loaded: entry,
    interrupted,
    watchFrom,
  } = useWatched(() => readEntry(itemId), itemId, isBeingRead);
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
        interrupted={interrupted}
        onWatch={(started) => {
          watchFrom(started);
          onReading();
        }}
      />
    );
  }
  switch (reading.attempt.kind) {
    case "underway":
      // A PDF read for the first time is shown as it arrived while it is read.
      return reading.attempt.arrival?.media_type === PDF_MEDIA_TYPE ? (
        <ArrivedPages itemId={itemId} />
      ) : (
        <Notice failed={false}>正在读取，PDF 和录音可能需要几分钟…</Notice>
      );
    case "failed":
      return <Notice failed>读取失败：{reading.attempt.reason}</Notice>;
  }
}

// Tells the service, once for each item, when a read item's document is first on show in the
// reader, which seats only a document; an item the list says was seen already is not told again.
// What the service answers is put in the list at once, and the items told are handed to the list
// column, for what it shows as new to change with it.
function useSeeing(seated: Seat | undefined, entries: Watched<ItemEntry[]>): ReadonlySet<string> {
  const [seen, setSeen] = useState<ReadonlySet<string>>(new Set());
  const told = useRef(new Set<string>());
  const { loaded, answer } = entries;
  useEffect(() => {
    if (seated === undefined || told.current.has(seated.itemId)) {
      return;
    }
    const { itemId } = seated;
    const listed =
      loaded.state === "ready" ? loaded.value.find((entry) => entry.id === itemId) : undefined;
    if (listed !== undefined && listed.seen !== null) {
      return;
    }
    told.current.add(itemId);
    seeItem(itemId).then(
      (entry) => {
        answer((before) => before.map((held) => (held.id === entry.id ? entry : held)));
        setSeen((before) => new Set(before).add(itemId));
      },
      // Not told this time; the next time the item is on show, it is told again.
      () => told.current.delete(itemId),
    );
  }, [seated?.itemId, seated?.life]);
  return seen;
}

// The sidebar and the list column, which share the state of browsing: what the sidebar has
// selected, and what it is drawn from. It is kept here, apart from the reader's, so that a press
// in either draws these two columns alone.
function BrowsingColumns({
  entries,
  seen,
  selected,
  onImported,
}: {
  entries: Watched<ItemEntry[]>;
  seen: ReadonlySet<string>;
  selected: string | undefined;
  onImported: (entry: ItemEntry) => void;
}) {
  const [openedAtStart] = useState(selected);
  const browsing = useBrowsing(entries, seen, openedAtStart);
  const layout = useSidebarLayout();
  return (
    <>
      <Sidebar browsing={browsing} layout={layout} />
      <ListColumn
        entries={entries.loaded}
        interrupted={entries.interrupted}
        refusal={entries.refusal}
        selected={selected}
        seen={seen}
        browsing={browsing}
        layout={layout}
        onImported={onImported}
        onChanged={entries.readNow}
      />
    </>
  );
}

export function App() {
  const linked = linkedItem(useLink());
  const selected = linked?.itemId;
  const [imports, setImports] = useState(0);
  // The list is read again at once after an import, a reading started again in the reader, and
  // a press in the list column that changes what the library holds; and every minute while
  // nothing in it is under way, for what subscriptions brought meanwhile.
  const entries = useWatched(listItems, "items", (listed) => listed.some(isBeingRead), {
    interval: () => LIST_INTERVAL_MS,
    unanswered: isUnanswered,
  });
  const { seating, seated, unshown, pointer } = useSeating();
  const seen = useSeeing(seated?.itemId === selected ? seated : undefined, entries);

  // The column shows the passage a citation opens the item for, once the item's reader is on
  // show.
  function openItem(itemId: string) {
    location.hash = itemLink(itemId);
  }

  function openImported(entry: ItemEntry) {
    setImports(imports + 1);
    entries.readNow();
    location.hash = itemLink(entry.id);
  }

  return (
    <SeatingProvider value={seating}>
      <div className="app">
        <BrowsingColumns
          entries={entries}
          seen={seen}
          selected={selected}
          onImported={openImported}
        />
        {selected === undefined ? (
          <Notice failed={false}>粘贴一个网址或上传一个文件，或从左侧打开一篇。</Notice>
        ) : (
          // An import may have started the open item's reading again, so the pane is rebuilt
          // after every import and reads the item again.
          <PaneBoundary key={`${selected}:${imports}`}>
            <ItemPane itemId={selected} revision={linked?.revision} onReading={entries.readNow} />
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
