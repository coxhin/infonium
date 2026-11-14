import { useState, useSyncExternalStore } from "react";
import { itemReading, listedItems, type ItemEntry } from "./client";
import { DigestPanel } from "./digest";
import { itemLink, Library } from "./library";
import { useLoaded } from "./loading";
import { Reader } from "./reader";

const ITEM_LINK = /^#\/items\/([0-9a-f]+)$/;

function onLinkChange(notify: () => void): () => void {
  addEventListener("hashchange", notify);
  return () => removeEventListener("hashchange", notify);
}

function selectedItem(): string | undefined {
  return ITEM_LINK.exec(location.hash)?.[1];
}

function ItemPane({ itemId }: { itemId: string }) {
  const reading = useLoaded(() => itemReading(itemId), itemId);
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
  const entries = useLoaded(listedItems, String(imports));

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
        <ItemPane key={selected} itemId={selected} />
      )}
    </div>
  );
}
