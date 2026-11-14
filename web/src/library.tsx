import { useState, type FormEvent } from "react";
import { importPage, type ItemEntry } from "./client";
import { itemLink } from "./links";
import { reasonOf, type Loaded } from "./loading";

type Importing = { state: "idle" } | { state: "running" } | { state: "failed"; reason: string };

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

  async function importUrl(event: FormEvent) {
    event.preventDefault();
    setImporting({ state: "running" });
    try {
      const entry = await importPage(url);
      setUrl("");
      setImporting({ state: "idle" });
      onImported(entry);
    } catch (error) {
      setImporting({ state: "failed", reason: reasonOf(error) });
    }
  }

  return (
    <nav className="library">
      <h1>infonium</h1>
      <form onSubmit={importUrl}>
        <input
          type="url"
          required
          placeholder="粘贴文章网址"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
        />
        <button disabled={importing.state === "running"}>
          {importing.state === "running" ? "导入中…" : "导入"}
        </button>
      </form>
      {importing.state === "failed" && <p className="status failed">{importing.reason}</p>}
      {entries.state === "failed" && <p className="status failed">{entries.reason}</p>}
      {entries.state === "ready" && (
        <ul>
          {entries.value.map((entry) => (
            <li key={entry.id}>
              <a href={itemLink(entry.id)} aria-current={entry.id === selected}>
                {entry.title}
                <small>{new URL(entry.url).hostname}</small>
              </a>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}
