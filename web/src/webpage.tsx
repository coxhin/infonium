import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { pageLink, readPageBlocks, type Anchor, type Block, type PageBlocks } from "./client";
import {
  bringIntoFrame,
  bringReaderToTop,
  keepingPlace,
  placeToKeep,
  useFrameHeight,
  usePassageFollowed,
} from "./frameroom";
import { FrameHighlights } from "./framehighlights";
import { useFrameSelection } from "./frameselection";
import { useLoaded } from "./loading";
import type { Marked } from "./marking";
import {
  FramedPage,
  guardLinks,
  holderOf,
  holdersOf,
  selectorOf,
  writeBlocks,
  type PointedPlace,
} from "./pagewriter";
import type { Setting } from "./setting";
import type { Pointed, ViewContext } from "./views";
import "./webpage.css";

const UNPLACED = "这段引文所在的段落在原件里没有单独的位置，标不出来；请到「文本」里看。";
const UNREACHED = "没有取到原件的页面，连接失败或被拒绝了";
const EMPTY = "原件的页面是空的";
const OTHER_PAGE = "取回的不是这一篇的原件页面";
const REPLACED = "原件的页面被别的内容换掉了";
const ABOUT_BLANK = "about:blank";
// The state of a document whose HTML is still being parsed.
const PARSING = "loading";
// The state of a document whose stylesheets, pictures and frames have all arrived.
const LOADED = "complete";
// How much of what the frame holds in place of the page is quoted in the reason.
const QUOTED_LENGTH = 80;

type Lookup = { found: true; place: PointedPlace } | { found: false; reason: string };

// Where a passage stands in the page: the element that stands for its block, whole, or the
// anchor's words where the element holds the block's text and they are found in it.
function pointedPlaceOf(
  framed: FramedPage,
  pageBlocks: PageBlocks,
  blocks: Block[],
  anchor: Anchor,
): Lookup {
  const page = framed.document;
  const block = blocks[anchor.block_number];
  const holder = holderOf(page, pageBlocks, blocks, anchor.block_number);
  if (block === undefined || holder === null) {
    return { found: false, reason: UNPLACED };
  }
  const words =
    block.kind !== "table" && framed.holdsSource(holder)
      ? framed.wordsOf(holder, block.text).wordsOf({ start: anchor.start, end: anchor.end })
      : null;
  const whole = page.createRange();
  whole.selectNodeContents(holder);
  return {
    found: true,
    place: { holder, selector: selectorOf(pageBlocks, holder), passage: words ?? whole },
  };
}

// What the frame holds: nothing yet, this item's page, or something else, said as the reason
// the page cannot be shown. Once its loading has ended, a frame that holds nothing yet holds
// nothing it will show.
type Holding =
  | { kind: "nothing" }
  | { kind: "page"; page: Document; view: Window & typeof globalThis }
  | { kind: "other"; reason: string };

function holdingOf(
  frame: HTMLIFrameElement,
  attribute: string,
  itemId: string,
  ended: boolean,
): Holding {
  const page = frame.contentDocument;
  const view = frame.contentWindow;
  // A document the app cannot reach is the browser's own page of a failed load.
  if (page === null || view === null) {
    return ended ? { kind: "other", reason: UNREACHED } : { kind: "nothing" };
  }
  const root = page.documentElement;
  if (page.URL === ABOUT_BLANK || root === null) {
    return ended ? { kind: "other", reason: EMPTY } : { kind: "nothing" };
  }
  if (root.getAttribute(attribute) === itemId) {
    return { kind: "page", page, view: view as Window & typeof globalThis };
  }
  const said = (page.title || root.textContent || "").trim().slice(0, QUOTED_LENGTH);
  return { kind: "other", reason: said === "" ? OTHER_PAGE : `${OTHER_PAGE}：「${said}」` };
}

// The highlights that run through a block that has no element of its own in the page, and so
// cannot be drawn there whole.
function unplacedCount(marked: Marked[], placed: number[]): number {
  const known = new Set(placed);
  return marked.filter((highlight) =>
    highlight.anchors.some((anchor) => !known.has(anchor.block_number)),
  ).length;
}

function UnplacedNote({
  total,
  placed,
  marked,
  setting,
}: {
  total: number;
  placed: number;
  marked: number;
  setting: Setting;
}) {
  const unplaced = total - placed;
  if (unplaced === 0) {
    return <p className="webpage-note">本文 {total} 段在原件里都有自己的位置。</p>;
  }
  return (
    <p className="webpage-note">
      本文 {total} 段中有 {unplaced} 段在原件里没有单独的位置，按网页原样显示
      {setting !== "source" && "，不换成译文"}；「文本」里有全部 {total} 段。
      {marked > 0 && `你的高亮中有 ${marked} 处落在这些段里，这里标不出，「文本」里有。`}
    </p>
  );
}

// A web article as the page it was read from, kept and inert, in the frame: in its source, its
// translation in the place of each block's text, or each block followed by its translation. A
// passage pointed at is marked and brought into view in the frame, and one whose blocks have no
// element of their own in the page is said to be lost. The reader's highlights are drawn in
// the page, and what the reader selects or presses in it is handed over. The frame shows
// nothing but this item's page: anything else it comes to hold is a failure, said in its place.
export function WebpageView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  marking,
  onShow,
  onLost,
}: ViewContext) {
  const pageBlocks = useLoaded(() => readPageBlocks(entry.id), entry.id);
  const frame = useRef<HTMLIFrameElement>(null);
  const [framed, setFramed] = useState<FramedPage>();
  const [highlights, setHighlights] = useState<FrameHighlights>();
  // Counts the times something changed what the reader may see of the page's words after it was
  // parsed: its stylesheets and pictures all arriving, a details element opened or closed, the
  // page's window made narrower or wider, as its own media rules may tell; so that its words
  // are found and drawn again.
  const [laidOut, setLaidOut] = useState(0);
  const [failure, setFailure] = useState<string>();
  const height = useFrameHeight(frame, onShow && pageBlocks.state === "ready");
  const following = usePassageFollowed(frame, framed?.view, onShow);
  const blocks = itemDocument.blocks;
  const ready = pageBlocks.state === "ready" ? pageBlocks.value : undefined;
  const failed = pageBlocks.state === "failed" ? pageBlocks.reason : failure;
  // The act of pointing last brought into view, so that it is brought into view once, and
  // again when the view comes on show again.
  const shownPointing = useRef<Pointed>(undefined);
  const settled = useRef<Document>(undefined);
  // Counted once for each change of the highlights, not at each drawing of the view, which
  // happens at every step of a scroll while a selection is handed over.
  const unplaced = useMemo(
    () => (ready === undefined ? 0 : unplacedCount(marking.marked, ready.placed)),
    [marking.marked, ready],
  );
  const holders = useMemo(
    () =>
      framed === undefined || ready === undefined ? undefined : holdersOf(framed.document, ready),
    [framed, ready],
  );
  const selection = useFrameSelection(
    frame,
    framed,
    highlights,
    holders,
    itemDocument,
    marking,
    onShow,
  );

  // Takes what the frame holds once it holds something: the page is guarded from the moment
  // its document can be reached, before anything in it can be clicked; it is shown, written
  // into and pointed into once it is parsed whole, since a page that arrives in parts holds
  // only some of its blocks before then; and from the moment it is left, it is a failure.
  function settle(ended: boolean): boolean {
    const element = frame.current;
    if (element === null || ready === undefined) {
      return false;
    }
    const holding = holdingOf(element, ready.page_attribute, entry.id, ended);
    switch (holding.kind) {
      case "nothing":
        return false;
      case "other":
        setFailure(holding.reason);
        return true;
      case "page":
        if (settled.current === undefined) {
          settled.current = holding.page;
          guardLinks(holding.page);
          holding.view.addEventListener("pagehide", () => setFailure(REPLACED));
          const { page, view } = holding;
          const parsed = () => {
            const shown = new FramedPage(page, view);
            setFramed(shown);
            setHighlights(new FrameHighlights(shown));
            let relaying = 0;
            function relaid() {
              if (relaying === 0) {
                relaying = requestAnimationFrame(() => {
                  relaying = 0;
                  shown.forgetWords();
                  setLaidOut((times) => times + 1);
                });
              }
            }
            if (page.readyState !== LOADED) {
              view.addEventListener("load", relaid, { once: true });
            }
            page.addEventListener("toggle", relaid, true);
            view.addEventListener("resize", relaid);
          };
          if (page.readyState === PARSING) {
            page.addEventListener("DOMContentLoaded", parsed, { once: true });
          } else {
            parsed();
          }
        } else if (settled.current !== holding.page) {
          setFailure(REPLACED);
        }
        return true;
    }
  }

  // The frame's document is looked for at every frame the browser draws until it is there, so
  // that it is guarded before it is first drawn.
  useEffect(() => {
    if (ready === undefined) {
      return;
    }
    let looking = 0;
    function look() {
      if (!settle(false)) {
        looking = requestAnimationFrame(look);
      }
    }
    look();
    return () => cancelAnimationFrame(looking);
  }, [ready]);

  // Once the frame has its height, so that the window has the room to scroll to it.
  const measured = height !== undefined;
  useLayoutEffect(() => {
    if (onShow && measured && frame.current !== null) {
      bringReaderToTop(frame.current);
    }
  }, [onShow, measured]);

  useLayoutEffect(() => {
    if (framed !== undefined && ready !== undefined && onShow) {
      const holder =
        pointed === undefined
          ? null
          : holderOf(framed.document, ready, blocks, pointed.anchors[0].block_number);
      const holders = `[${ready.block_attribute}], [${ready.cell_attribute}]`;
      // A selection in an element that is written anew no longer holds the words it was made
      // of, so it is let go.
      const selected = new Set(selection.selectedHolders());
      keepingPlace(placeToKeep(framed.document, holders, holder), framed.view, () => {
        const changed = writeBlocks(framed, ready, blocks, setting, translation.blocks);
        if (changed.some((element) => selected.has(element))) {
          selection.letGo();
        }
      });
    }
  }, [framed, ready, blocks, setting, translation.blocks, onShow, pointed]);

  useLayoutEffect(() => {
    const first = shownPointing.current !== pointed;
    if (!onShow || pointed === undefined) {
      framed?.unmark();
      following.follow(undefined);
      shownPointing.current = undefined;
      return;
    }
    if (failed !== undefined) {
      if (first) {
        onLost(`原件打不开：${failed}`);
        shownPointing.current = pointed;
      }
      return;
    }
    if (framed === undefined || ready === undefined || frame.current === null) {
      return;
    }
    // Each anchor is marked where its block has an element; the passage is lost only where
    // none has.
    const lookups = pointed.anchors.map((anchor) => pointedPlaceOf(framed, ready, blocks, anchor));
    const places = lookups.flatMap((lookup) => (lookup.found ? [lookup.place] : []));
    const start = places[0];
    if (start === undefined) {
      framed.unmark();
      following.follow(undefined);
      if (first) {
        onLost(lookups.find((lookup) => !lookup.found)?.reason ?? UNPLACED);
      }
    } else {
      framed.mark(places);
      if (first) {
        bringIntoFrame(frame.current, framed.view, start.passage);
        following.follow(start.passage);
      } else {
        following.renew(start.passage);
      }
    }
    shownPointing.current = pointed;
  }, [framed, ready, failed, blocks, pointed, onShow, setting, translation.blocks, laidOut]);

  // Drawn after the blocks are written, since writing a block replaces the words drawn in it.
  useLayoutEffect(() => {
    if (highlights !== undefined && holders !== undefined && onShow) {
      highlights.draw(holders, blocks, marking.marked, marking.opened);
    }
  }, [
    highlights,
    holders,
    blocks,
    setting,
    translation.blocks,
    onShow,
    marking.marked,
    marking.opened,
    laidOut,
  ]);

  if (failed !== undefined) {
    return (
      <p className="status failed webpage-failed">
        原件打不开：{failed}。「文本」里有这一篇的全部内容。
      </p>
    );
  }
  if (ready === undefined) {
    return <p className="status webpage-loading">正在打开原件…</p>;
  }
  return (
    <div className="webpage">
      <UnplacedNote
        total={blocks.length}
        placed={ready.placed.length}
        marked={unplaced}
        setting={setting}
      />
      <iframe
        ref={frame}
        className="webpage-frame"
        src={pageLink(entry.id)}
        sandbox="allow-same-origin"
        title="原件"
        // Nothing of the frame is drawn until it is known to hold this item's page.
        style={{ height, visibility: framed === undefined ? "hidden" : undefined }}
        onLoad={() => settle(true)}
      />
    </div>
  );
}
