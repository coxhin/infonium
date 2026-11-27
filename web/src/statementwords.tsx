import { Fragment } from "react";

// The words of a statement infonium wrote, as they are shown and put into a note. A digest
// marks the words a reader skimming it most needs between pairs of double asterisks, which are
// shown in bold; an answer's words carry no marks.

// A run of a statement's words, in bold or not.
export type WordsPiece = { text: string; bold: boolean };

const BOLD_MARK = "**";

// A digest's statement cut at its marks: the words between the marks of a pair are bold, and a
// mark without a partner, the last of an odd number, is words as written. A pair that holds
// nothing leaves nothing, and runs of the same kind next to each other are one run.
export function boldPieces(text: string): WordsPiece[] {
  const parts = text.split(BOLD_MARK);
  if (parts.length % 2 === 0) {
    const unpaired = parts.splice(-2, 2).join(BOLD_MARK);
    parts.push(unpaired);
  }
  const pieces: WordsPiece[] = [];
  parts.forEach((part, index) => {
    const bold = index % 2 === 1;
    const last = pieces.at(-1);
    if (part === "") {
      return;
    }
    if (last !== undefined && last.bold === bold) {
      last.text += part;
    } else {
      pieces.push({ text: part, bold });
    }
  });
  return pieces;
}

// An answer's statement, whose words are all as written.
export function plainWords(text: string): WordsPiece[] {
  return text === "" ? [] : [{ text, bold: false }];
}

export function StatementWords({ words }: { words: WordsPiece[] }) {
  return words.map((piece, index) =>
    piece.bold ? (
      <strong key={index}>{piece.text}</strong>
    ) : (
      <Fragment key={index}>{piece.text}</Fragment>
    ),
  );
}
