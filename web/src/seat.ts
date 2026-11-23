// The reader on show: its item, the document it shows, and which of the readers made since
// the app began it is. An item opened again gets a reader of a new life, even when it shows
// the same document.
export type Seat = { itemId: string; documentId: string; life: number };
