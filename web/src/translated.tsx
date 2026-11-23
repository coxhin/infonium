import { Fragment, memo } from "react";
import { BlockForm, MarkedText, SourceBlock } from "./blocks";
import type { Anchor, Block, Segment, TranslatedBlock } from "./client";
import type { Setting } from "./setting";
import type { Again, TranslationState } from "./translation";
import "./translated.css";

// The element that holds a block's translation carries the block's number in this attribute,
// never in the one a source block carries, so that nothing takes one for the other.
export const TRANSLATION_ATTRIBUTE = "data-translation";
export const POINTED_CLASS = "pointed";
// Segments of one block follow each other as sentences of one paragraph do.
const SEGMENT_GAP = " ";
// The control to translate a block again stands in the narrow margin beside the block, where
// a word would not fit.
const AGAIN_MARK = "↻";

// A range of a block's text, in code points.
export type TextRange = { start: number; end: number };

export function overlaps(first: TextRange, second: TextRange): boolean {
  return first.start < second.end && second.start < first.end;
}

function SegmentText({ segment, pointed }: { segment: Segment; pointed: boolean }) {
  return (
    <span className={pointed ? `segment ${POINTED_CLASS}` : "segment"}>
      <MarkedText text={segment.text} marks={segment.marks} />
    </span>
  );
}

// A control to translate one block again, and what has come of doing so. It stands in the
// margin beside the block, clear of its words; where there is a pointer that hovers it shows
// on hover and on focus, and elsewhere it is always on show.
export function AgainControl({
  number,
  again,
  onAgain,
}: {
  number: number;
  again: Again | undefined;
  onAgain: (blockNumber: number) => void;
}) {
  return (
    <>
      <button
        className="again"
        disabled={again?.state === "underway"}
        title="用写摘要的模型重新翻译这一段"
        aria-label="重新翻译这一段"
        onClick={() => onAgain(number)}
      >
        {AGAIN_MARK}
      </button>
      {again?.state === "underway" && <p className="again-note">正在重新翻译这一段…</p>}
      {again?.state === "failed" && (
        <p className="again-note failed">重新翻译没有成功：{again.reason}</p>
      )}
    </>
  );
}

function TranslationOf({
  block,
  number,
  translated,
  beside,
  again,
  pointed,
  onAgain,
}: {
  block: Block;
  number: number;
  translated: TranslatedBlock;
  beside: boolean;
  again: Again | undefined;
  pointed: Anchor | undefined;
  onAgain: (blockNumber: number) => void;
}) {
  const isPointed = (segment: Segment) => pointed !== undefined && overlaps(segment, pointed);
  const content = translated.segments.map((segment, index) => (
    <Fragment key={index}>
      {index > 0 && SEGMENT_GAP}
      <SegmentText segment={segment} pointed={isPointed(segment)} />
    </Fragment>
  ));
  // A table's cells hold no element of their own around each segment, so a table is marked
  // whole.
  const tablePointed = block.kind === "table" && translated.segments.some(isPointed);
  return (
    <div
      className={beside ? "translated beside" : "translated"}
      {...{ [TRANSLATION_ATTRIBUTE]: number }}
    >
      <BlockForm
        block={block}
        text={translated.segments.map((segment) => segment.text).join(SEGMENT_GAP)}
        content={content}
        marking={tablePointed ? { className: POINTED_CLASS } : {}}
      />
      <AgainControl number={number} again={again} onAgain={onAgain} />
    </div>
  );
}

// One block of the text as the setting asks for it: its source, its translation, or the one
// followed by the other. A block without a translation is shown as its source. The source
// stays in the page when only the translation is on show, so that a citation of it can be
// resolved there. Rows are drawn again only when what they are given changes, so a
// translation that arrives in pieces redraws only the blocks of each piece.
export const Row = memo(function Row({
  block,
  number,
  setting,
  translated,
  again,
  pointed,
  onAgain,
}: {
  block: Block;
  number: number;
  setting: Setting;
  translated: TranslatedBlock | undefined;
  again: Again | undefined;
  pointed: Anchor | undefined;
  onAgain: (blockNumber: number) => void;
}) {
  if (setting === "source" || translated === undefined) {
    return <SourceBlock block={block} number={number} hidden={false} />;
  }
  return (
    <>
      <SourceBlock block={block} number={number} hidden={setting === "translation"} />
      <TranslationOf
        block={block}
        number={number}
        translated={translated}
        beside={setting === "both"}
        again={again}
        // Beside its source, a passage pointed at is highlighted in the source alone.
        pointed={setting === "translation" ? pointed : undefined}
        onAgain={onAgain}
      />
    </>
  );
});

// How far the translation has come, and what can be done about it from there.
export function TranslationNote({ translation }: { translation: TranslationState }) {
  const loaded = translation.loaded;
  if (loaded === undefined) {
    return null;
  }
  if (loaded.state === "loading") {
    return <p className="translation-note">正在读取译文…</p>;
  }
  if (loaded.state === "failed") {
    return (
      <p className="translation-note failed">
        译文取不到：{loaded.reason}
        <button onClick={translation.readAgain}>再试一次</button>
      </p>
    );
  }
  const { progress, input_tokens: input, output_tokens: output, language } = loaded.value;
  const interrupted = translation.interrupted && (
    <span className="interrupted">与服务的连接断了，正在重连…</span>
  );
  switch (progress.kind) {
    case "unasked":
      return (
        <p className="translation-note">
          还没有译文。
          <button onClick={translation.goOn}>翻译</button>
          {interrupted}
        </p>
      );
    case "translating":
      return (
        <p className="translation-note">
          <span>
            正在翻译：已译 {progress.translated} / {progress.total} 段
          </span>
          <progress value={progress.translated} max={Math.max(progress.total, 1)} />
          {interrupted}
        </p>
      );
    case "stopped":
      return (
        <p className="translation-note failed">
          翻译停在了半路：{progress.reason}
          <button onClick={translation.goOn}>继续翻译</button>
        </p>
      );
    case "unneeded":
      return <p className="translation-note">原文已是{language}，不用翻译，下面是原文。</p>;
    case "translated":
      return (
        <p className="translation-note">
          <span>
            已译完 · 输入 {input.toLocaleString()} · 输出 {output.toLocaleString()} tokens
          </span>
          <button className="quiet" onClick={translation.anew}>
            全部重新翻译
          </button>
        </p>
      );
  }
}
