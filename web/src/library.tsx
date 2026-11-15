import { useState, type ChangeEvent, type FormEvent } from "react";
import { importLink, importUpload, type ItemEntry } from "./client";
import { itemLink } from "./links";
import { reasonOf, type Loaded } from "./loading";
import { itemTitle, originLabel } from "./origin";

type Importing = { state: "idle" } | { state: "running" } | { state: "failed"; reason: string };

const ATTEMPT_NOTES: Record<ItemEntry["reading"]["attempt"]["kind"], string> = {
  underway: " · 读取中…",
  settled: "",
  failed: " · 读取失败",
};
// Kinds of file the upload dialog offers; what a file really is, the server tells from its bytes.
const UPLOADABLE = "application/pdf,audio/*,video/*";

export function Library({
  entries,
  selected,
  onImported,
}: {
  entries: Loaded<ItemEntry[]>;
  selected: string | undefined;
  onImported: (entry: ItemEntry) => void;
}) {
  const [url, setUrl] = useState("");
  const [importing, setImporting] = useState<Importing>({ state: "idle" });

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

  return (
    <nav className="library">
      <h1>infonium</h1>
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
      {entries.state === "ready" && (
        <ul>
          {entries.value.map((entry) => (
            <li key={entry.id}>
              <a href={itemLink(entry.id)} aria-current={entry.id === selected}>
                {itemTitle(entry)}
                <small>
                  {originLabel(entry.origin)}
                  {ATTEMPT_NOTES[entry.reading.attempt.kind]}
                </small>
              </a>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}
