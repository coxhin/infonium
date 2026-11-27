import "./newmarks.css";

// How many new items there are, as a small count beside a name; a screen reader hears what it
// counts.
export function NewCount({ count }: { count: number }) {
  return (
    <span className="new-count" title={`${count} 条新条目`}>
      <span aria-hidden="true">{count}</span>
      <span className="heard">，{count} 条新条目</span>
    </span>
  );
}

// The mark of a new item, before its title; a screen reader hears it after the title.
export function NewDot() {
  return <span className="new-dot" aria-hidden="true" />;
}

export function NewHeard() {
  return <span className="heard">（新）</span>;
}
