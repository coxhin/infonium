import { useState, useSyncExternalStore } from "react";
import { listItems, readItem, type ItemEntry } from "./client";
import { DigestPanel } from "./digest";
import { Library } from "./library";
import { itemLink, linkedItem } from "./links";
import { useLoaded } from "./loading";
import { Reader } from "./reader";

function onLinkChange(notify: () => void): () => void {
  addEventListener("hashchange", notify);
  return () => removeEventListener("hashchange", notify);
}

function selectedItem(): string | undefined {
  return linkedItem(location.hash);
}

function ItemPane({ itemId }: { itemId: string }) {
  const reading = useLoaded(() => readItem(itemId), itemId);
  return (
    <>
      <main>
        {reading.state === "loading" && <p className="status">正在打开…</p>}
        {reading.state === "failed" && <p className="status failed">{reading.reason}</p>}
        {reading.state === "ready" && <Reader reading={reading.value} />}
      </main>
      {reading.state === "ready" && <DigestPanel itemId={itemId} />}
    </>
  );
}

export function App() {
  const selected = useSyncExternalStore(onLinkChange, selectedItem);
  const [imports, setImports] = useState(0);
  const entries = useLoaded(listItems, String(imports));

  function openImported(entry: ItemEntry) {
    setImports(imports + 1);
    location.hash = itemLink(entry.id);
  }

  return (
    <div className="app">
      <Library entries={entries} selected={selected} onImported={openImported} />
      {selected === undefined ? (
        <main>
          <p className="status">粘贴一个网址导入文章，或从左侧打开一篇。</p>
        </main>
      ) : (
        // An import may have replaced the open item's document, so the pane is rebuilt after
        // every import and reads the item again.
        <ItemPane key={`${selected}:${imports}`} itemId={selected} />
      )}
    </div>
  );
}
