import { useEffect, type ReactNode } from "react";
import type { Anchors, ItemDocument, ItemEntry } from "./client";
import { itemTitle } from "./entry";
import { reasonOf } from "./loading";
import type { Marking } from "./marking";
import { PagesView } from "./pagesview";
import { Reader, showPassage, usePassageKept, type Playing } from "./reader";
import type { Setting } from "./setting";
import type { TranslationState } from "./translation";
import { WebpageView } from "./webpage";

export const PDF_MEDIA_TYPE = "application/pdf";
const HTML_MEDIA_TYPE = "text/html";

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again. The passage is given by its anchors, one for each block it
// runs through. Each view works out where the passage lies in what it shows.
export type Pointed = { anchors: Anchors };

// The views there are. A view to come is named here, given an entry, and offered in viewsOf.
export type ViewName = "pages" | "text" | "webpage";

// What the reader tells every view: the item and its document, what the switch is set to, the
// translation as far as it has come, the passage pointed at, the reader's highlights and how a
// selection is handed over, the recording's playing where there is one, and whether this view
// is the one on show.
export type ViewContext = {
  entry: ItemEntry;
  itemDocument: ItemDocument;
  setting: Setting;
  translation: TranslationState;
  pointed: Pointed | undefined;
  marking: Marking;
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

// The document read from the source, in the words the switch asks for, with the reader's
// highlights drawn and a selection in it handed over. A passage pointed at is shown whole and
// brought into view at its start where it is on show once this view is on show, and is kept
// there while the translation arrives around it.
function TextView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  marking,
  playing,
  onShow,
  onLost,
}: ViewContext) {
  const remember = usePassageKept(pointed?.anchors[0], onShow, setting);
  useEffect(() => {
    if (onShow && pointed !== undefined) {
      try {
        showPassage(pointed.anchors);
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
      pointed={pointed?.anchors}
      marking={marking}
      playing={playing}
      onShow={onShow}
    />
  );
}

const PAGES: View = { name: "pages", label: "原件", showsTranslation: true, Draw: PagesView };
const TEXT: View = { name: "text", label: "文本", showsTranslation: true, Draw: TextView };
const WEBPAGE: View = { name: "webpage", label: "原件", showsTranslation: true, Draw: WebpageView };

// The views of a source, as its document says what the source is, the first being the one
// the reader opens on. Every source has its text; a PDF also has its pages, and a web article
// the page it was read from.
export function viewsOf(itemDocument: ItemDocument): View[] {
  switch (itemDocument.media_type) {
    case PDF_MEDIA_TYPE:
      return [PAGES, TEXT];
    case HTML_MEDIA_TYPE:
      return [TEXT, WEBPAGE];
    default:
      return [TEXT];
  }
}
