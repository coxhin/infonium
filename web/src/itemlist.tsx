import { useId, useState, type ChangeEvent, type FormEvent } from "react";
import { importLink, importUpload, type ItemEntry } from "./client";
import { isNew, itemTitle, originLabel } from "./entry";
import { itemLink } from "./links";
import { reasonOf, type Loaded } from "./loading";
import { NewDot, NewHeard } from "./newmarks";

type Importing = { state: "idle" } | { state: "running" } | { state: "failed"; reason: string };

const ATTEMPT_NOTES: Record<ItemEntry["reading"]["attempt"]["kind"], string> = {
  underway: " · 读取中…",
  settled: "",
  failed: " · 读取失败",
};
// Kinds of file the upload dialog offers; what a file really is, the server tells from its bytes.
const UPLOADABLE = "application/pdf,audio/*,video/*,text/html,.html,.htm";

// Which items the list shows: all, those a reader added by a link or an upload, or those that
// subscriptions brought.
type Narrowing = "all" | "added" | "subscribed";
const NARROWING_NAMES: Record<Narrowing, string> = {
  all: "全部",
  added: "手动添加",
  subscribed: "订阅",
};
const NARROWED_AWAY: Record<Narrowing, string> = {
  all: "",
  added: "粘贴网址或上传文件添加的文章会列在这里。",
  subscribed: "订阅读进来的文章会列在这里。",
};

function narrowed(entries: ItemEntry[], narrowing: Narrowing): ItemEntry[] {
  switch (narrowing) {
    case "all":
      return entries;
    case "added":
      return entries.filter((entry) => entry.origin.kind !== "subscribed");
    case "subscribed":
      return entries.filter((entry) => entry.origin.kind === "subscribed");
  }
}

// The list of items as it has always been: adding by an address or a file, and the items, each
// with where it came from, a subscription by its title; narrowed by where they came from.
export function ItemList({
  entries,
  interrupted,
  selected,
  seen,
  subscriptionTitles,
  onImported,
}: {
  entries: Loaded<ItemEntry[]>;
  interrupted: boolean;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  subscriptionTitles: ReadonlyMap<string, string>;
  onImported: (entry: ItemEntry) => void;
}) {
  const [url, setUrl] = useState("");
  const [importing, setImporting] = useState<Importing>({ state: "idle" });
  const [narrowing, setNarrowing] = useState<Narrowing>("all");
  const narrowingName = useId();

  async function imported(importingItem: Promise<ItemEntry>) {
    setImporting({ state: "running" });
    try {
      const entry = await importingItem;
      setImporting({ state: "idle" });
      onImported(entry);
    } catch (error) {
      setImporting({ state: "failed", reason: reasonOf(error) });
    }
  }

  function importUrl(event: FormEvent) {
    event.preventDefault();
    void imported(importLink(url).then((entry) => (setUrl(""), entry)));
  }

  function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // The same file can be chosen again only once the field has let go of it.
    event.target.value = "";
    if (file !== undefined) {
      void imported(importUpload(file));
    }
  }

  // An item that a subscription brought is told by the subscription's title, and by its site
  // once the subscription is gone.
  function sourceName(entry: ItemEntry): string {
    return entry.origin.kind === "subscribed"
      ? (subscriptionTitles.get(entry.origin.subscription) ?? originLabel(entry.origin))
      : originLabel(entry.origin);
  }

  const shown = entries.state === "ready" ? narrowed(entries.value, narrowing) : [];
  return (
    <>
      <form onSubmit={importUrl}>
        <input
          type="url"
          required
          placeholder="粘贴网址：文章、PDF、视频、播客"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
        />
        <button disabled={importing.state === "running"}>
          {importing.state === "running" ? "导入中…" : "导入"}
        </button>
      </form>
      <label className="upload" aria-disabled={importing.state === "running"}>
        <input
          type="file"
          accept={UPLOADABLE}
          disabled={importing.state === "running"}
          onChange={importFile}
        />
        上传 PDF、音频或视频
      </label>
      {importing.state === "failed" && <p className="status failed">{importing.reason}</p>}
      {entries.state === "failed" && <p className="status failed">{entries.reason}</p>}
      {interrupted && <p className="status failed">连接不上服务，正在重试…</p>}
      {entries.state === "ready" && entries.value.length > 0 && (
        <fieldset className="narrowing">
          <legend className="heard">显示哪些文章</legend>
          {(Object.keys(NARROWING_NAMES) as Narrowing[]).map((name) => (
            <label key={name}>
              <input
                type="radio"
                name={narrowingName}
                value={name}
                checked={narrowing === name}
                onChange={() => setNarrowing(name)}
              />
              {NARROWING_NAMES[name]}
            </label>
          ))}
        </fieldset>
      )}
      {entries.state === "ready" && entries.value.length > 0 && shown.length === 0 && (
        <p className="status">{NARROWED_AWAY[narrowing]}</p>
      )}
      {shown.length > 0 && (
        <ul>
          {shown.map((entry) => {
            const fresh = isNew(entry, seen);
            const source = sourceName(entry);
            return (
              <li key={entry.id}>
                <a
                  href={itemLink(entry.id)}
                  className={fresh ? "new" : undefined}
                  aria-current={entry.id === selected}
                >
                  {fresh && <NewDot />}
                  {itemTitle(entry)}
                  {fresh && <NewHeard />}
                  <small>
                    <span className="source" title={source}>
                      {source}
                    </span>
                    <span className="attempt">{ATTEMPT_NOTES[entry.reading.attempt.kind]}</span>
                  </small>
                </a>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
