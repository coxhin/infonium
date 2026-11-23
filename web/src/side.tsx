import { DigestPanel, type Pointer } from "./digest";

// The column beside the reader: what infonium wrote about the open item. Its citations point
// into the reader, at passages of the document that is on show there.
export function SidePanel({
  itemId,
  pointer,
}: {
  itemId: string;
  documentId: string;
  pointer: Pointer;
}) {
  return <DigestPanel itemId={itemId} pointer={pointer} />;
}
