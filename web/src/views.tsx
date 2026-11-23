import { useEffect, type ReactNode } from "react";
import type { Anchor, ItemDocument, ItemEntry } from "./client";
import { itemTitle } from "./entry";
import { reasonOf } from "./loading";
import { PagesView } from "./pagesview";
import { Reader, showAnchor, usePassageKept, type Playing } from "./reader";
import type { Setting } from "./setting";
import type { TranslationState } from "./translation";

const PDF_MEDIA_TYPE = "application/pdf";

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again. Each view works out where the passage lies in what it shows.
export type Pointed = { anchor: Anchor };

// The views there are. A view to come is named here, given an entry, and offered in viewsOf.
export type ViewName = "pages" | "text";

// What the reader tells every view: the item and its document, what the switch is set to, the
// translation as far as it has come, the passage pointed at, the recording's playing where
// there is one, and whether this view is the one on show.
export type ViewContext = {
  entry: ItemEntry;
  itemDocument: ItemDocument;
  setting: Setting;
  translation: TranslationState;
  pointed: Pointed | undefined;
  playing: Playing | undefined;
  onShow: boolean;
  onLost: (reason: string) => void;
};

// A way the reader shows a source: named, called by its label, and drawn by its component.
// One that shows the translation is the only kind for which a translation is asked for.
export type View = {
  name: ViewName;
  label: string;
  showsTranslation: boolean;
  Draw: (context: ViewContext) => ReactNode;
};

// The document read from the source, in the words the switch asks for. A passage pointed at
// is brought into view where it is on show once this view is on show, and is kept there while
// the translation arrives around it.
function TextView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  playing,
  onShow,
  onLost,
}: ViewContext) {
  const anchor = pointed?.anchor;
  const remember = usePassageKept(anchor, onShow, setting);
  useEffect(() => {
    if (onShow && pointed !== undefined) {
      try {
        showAnchor(pointed.anchor);
        remember();
      } catch (error) {
        onLost(reasonOf(error));
      }
    }
  }, [pointed, onShow, setting]);
  return (
    <Reader
      entry={entry}
      title={itemTitle(entry)}
      itemDocument={itemDocument}
      setting={setting}
      translation={translation}
      pointed={anchor}
      playing={playing}
    />
  );
}

const PAGES: View = { name: "pages", label: "原件", showsTranslation: true, Draw: PagesView };
const TEXT: View = { name: "text", label: "文本", showsTranslation: true, Draw: TextView };

// The views of a source, as its document says what the source is, the first being the one
// the reader opens on. Every source has its text; a PDF also has its pages.
export function viewsOf(itemDocument: ItemDocument): View[] {
  return itemDocument.media_type === PDF_MEDIA_TYPE ? [PAGES, TEXT] : [TEXT];
}
