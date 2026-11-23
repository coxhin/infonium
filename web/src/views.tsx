import type { ItemDocument, PagePlace } from "./client";
import { PageViewer } from "./pages";
import { surfaceOf } from "./places";
import type { Setting } from "./setting";
import type { TranslationState } from "./translation";

// A way the reader shows a source. The text is the document read from it, which every source
// has; a source with pages may also be shown as those pages. A view is named by what it shows,
// and called by the reader as its label says.
export type ViewName = "pages" | "text";

export const VIEW_LABELS: Record<ViewName, string> = { pages: "原件", text: "文本" };

// The views the reader offers of a source, the first being the one it opens on.
export function viewsOf(itemDocument: ItemDocument): ViewName[] {
  return surfaceOf(itemDocument.blocks) === "page" ? ["pages", "text"] : ["text"];
}

// What every view is told of the reader: what the switch is set to, and the translation as
// far as it has come.
export type ViewContext = {
  setting: Setting;
  translation: TranslationState;
  inView: boolean;
};

// A PDF as its pages, with the boxes pointed at outlined. The pages are shown as they are,
// in the language of the source, whatever the switch says.
export function PagesView({
  source,
  pointed,
  context,
}: {
  source: string;
  pointed: PagePlace[];
  context: ViewContext;
}) {
  return (
    <>
      {context.setting !== "source" && (
        <p className="view-note">原件的页面按原文显示，译文在「文本」里。</p>
      )}
      <PageViewer source={source} pointed={pointed} inView={context.inView} />
    </>
  );
}
