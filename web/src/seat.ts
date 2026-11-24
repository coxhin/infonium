// The reader on show: its item, the document it shows, the document the item holds now (which
// differs where an earlier revision is on show), the link it was opened by, and which of the
// readers made since the app began it is. An item opened again gets a reader of a new life,
// even when it shows the same document.
export type Seat = {
  itemId: string;
  documentId: string;
  holding: string;
  link: string;
  life: number;
};

// An item the reader cannot show, as it is selected: its reading failed or is under way, or it
// could not be opened. A citation that opens it is told why instead of waiting.
export type Unshown = { itemId: string; reason: string };
