import { useEffect, useMemo, type ReactNode } from "react";
import { sourceLink, type Anchor, type ItemDocument, type ItemEntry, type PagePlace } from "./client";
import { itemTitle } from "./entry";
import { reasonOf } from "./loading";
import { PageViewer } from "./pages";
import { pagePlaces, placesOf } from "./places";
import { Reader, showAnchor, usePassageKept, type Playing } from "./reader";
import type { Setting } from "./setting";
import type { TranslationState } from "./translation";
import { WebpageView } from "./webpage";

const PDF_MEDIA_TYPE = "application/pdf";
const HTML_MEDIA_TYPE = "text/html";

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again. Each view works out where the passage lies in what it shows.
export type Pointed = { anchor: Anchor };

// The views there are. A view to come is named here, given an entry, and offered in viewsOf.
export type ViewName = "pages" | "text" | "webpage";

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

const NO_PAGES: PagePlace[] = [];

// A PDF as its pages, with the boxes of the passage pointed at outlined. The pages are shown as
// they are, in the language of the source, whatever the switch says.
function PagesView({ entry, itemDocument, setting, pointed, onShow }: ViewContext) {
  // Worked out anew at every act of pointing, so that the pages turn to the same boxes again.
  const boxes = useMemo(
    () =>
      pointed === undefined ? NO_PAGES : pagePlaces(placesOf(itemDocument.blocks, pointed.anchor)),
    [pointed, itemDocument],
  );
  return (
    <>
      {setting !== "source" && (
        <p className="view-note">原件的页面按原文显示，译文在「文本」里。</p>
      )}
      <PageViewer source={sourceLink(entry.id)} pointed={boxes} inView={onShow} />
    </>
  );
}

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

const PAGES: View = { name: "pages", label: "原件", showsTranslation: false, Draw: PagesView };
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
