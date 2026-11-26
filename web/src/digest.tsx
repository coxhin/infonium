import { useEffect, useMemo, useState } from "react";
import {
  digestsUnderWay,
  readDigestStyles,
  readDigests,
  writeDigest,
  type ChosenStyle,
  type Digest,
  type DigestStyle,
  type DigestWriting,
  type Statement,
  type StyleOffer,
} from "./client";
import {
  Citations,
  citationNumbers,
  LostCitations,
  type CitationNumbers,
  type Citing,
} from "./citing";
import { reasonOf, useLoaded } from "./loading";
import { NOTE_GUTTER, NoteDown, statementTaking } from "./notedown";
import { boldPieces, StatementWords } from "./statementwords";
import "./digest.css";

const CUSTOM = "custom" satisfies DigestStyle;
// The most characters a custom style's instructions may have, as the service holds them to.
const CUSTOM_INSTRUCTIONS_CHARACTERS = 2000;
const SAID_SOMETHING = /\S/;
// What the reader chose last, kept with the browser as the models chosen are: the style on
// show, and the instructions of the custom style last written.
const STYLE_KEY = "infonium.digestStyle";
const INSTRUCTIONS_KEY = "infonium.customInstructions";

// How a style's digest stands in the panel beyond what is kept: being written, or not written
// the last time it was asked for, and why, with what was asked.
type Writing = { state: "writing" } | { state: "failed"; reason: string; asked: ChosenStyle };

// The service offers the same styles until it starts again, so they are asked for once; a
// failure is asked again the next time.
let stylesOffered: Promise<StyleOffer[]> | undefined;

function offeredStyles(): Promise<StyleOffer[]> {
  stylesOffered ??= readDigestStyles().catch((error: unknown) => {
    stylesOffered = undefined;
    throw error;
  });
  return stylesOffered;
}

function characterCount(text: string): number {
  return [...text].length;
}

function Sentence({
  itemId,
  statement,
  numberOf,
  citing,
}: {
  itemId: string;
  statement: Statement;
  numberOf: CitationNumbers;
  citing: Citing;
}) {
  // The citations and the mark that puts the statement into a note stay on the line of the
  // statement's last words.
  return (
    <>
      <StatementWords words={boldPieces(statement.text)} />
      <span className="sentence-end">
        <Citations
          anchors={statement.anchors}
          numberOf={numberOf}
          citing={citing}
          titleOf={(anchor) => anchor.quote}
          onPoint={(anchor) => citing.point([anchor])}
        />
        <LostCitations statement={statement} citesAll />
        <NoteDown
          taking={statementTaking([statement], () => itemId, boldPieces)}
          label="记到笔记：这句摘要"
          shape="mark"
        />
      </span>
    </>
  );
}

// A digest's sections: a list shows its statements one under another, a paragraph runs them
// on, each followed by its citations.
function DigestBody({
  itemId,
  digest,
  citing,
}: {
  itemId: string;
  digest: Digest;
  citing: Citing;
}) {
  const numberOf = useMemo(
    () => citationNumbers(digest.sections.flatMap((section) => section.statements)),
    [digest],
  );
  return (
    <>
      {digest.sections.map((section, sectionIndex) => (
        <section key={sectionIndex}>
          {section.title !== "" && <h2>{section.title}</h2>}
          {section.form === "paragraph" ? (
            <p className="running">
              {section.statements.map((statement, statementIndex) => (
                <span key={statementIndex} className="sentence">
                  <Sentence
                    itemId={itemId}
                    statement={statement}
                    numberOf={numberOf}
                    citing={citing}
                  />
                </span>
              ))}
            </p>
          ) : (
            <ul>
              {section.statements.map((statement, statementIndex) => (
                <li key={statementIndex} className={NOTE_GUTTER}>
                  <Sentence
                    itemId={itemId}
                    statement={statement}
                    numberOf={numberOf}
                    citing={citing}
                  />
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
      <footer className="spent">
        {digest.model} · 输入 {digest.input_tokens.toLocaleString()} · 输出{" "}
        {digest.output_tokens.toLocaleString()} tokens
      </footer>
    </>
  );
}

// The reader's own instructions for the digest they want, with how much of the limit they
// take, and the button that writes them.
function InstructionsBox({
  prefilled,
  disabled,
  onWrite,
  onCancel,
}: {
  prefilled: string;
  disabled: boolean;
  onWrite: (instructions: string) => void;
  onCancel: (() => void) | undefined;
}) {
  const [draft, setDraft] = useState(prefilled);
  const count = characterCount(draft);
  const over = count > CUSTOM_INSTRUCTIONS_CHARACTERS;
  return (
    <div className="instructions-box">
      <textarea
        aria-label="自定义摘要的要求"
        placeholder="例如：按时间顺序列出文中的事件，每条以日期开头"
        value={draft}
        rows={4}
        onChange={(event) => setDraft(event.target.value)}
      />
      <div className="instructions-foot">
        <span className={over ? "count over" : "count"} aria-live="polite">
          {count.toLocaleString()} / {CUSTOM_INSTRUCTIONS_CHARACTERS.toLocaleString()}
        </span>
        {onCancel !== undefined && <button onClick={onCancel}>取消</button>}
        <button
          className="write"
          disabled={disabled || over || !SAID_SOMETHING.test(draft)}
          onClick={() => onWrite(draft)}
        >
          生成
        </button>
      </div>
    </div>
  );
}

// What the custom digest on show was written by, and the way to change it and write it again.
// After a writing that failed, the box opens with the instructions it sent.
function KeptInstructions({
  instructions,
  failedInstructions,
  disabled,
  onWrite,
}: {
  instructions: string;
  failedInstructions: string | undefined;
  disabled: boolean;
  onWrite: (instructions: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  if (editing) {
    return (
      <InstructionsBox
        prefilled={failedInstructions ?? instructions}
        disabled={disabled}
        onWrite={(changed) => {
          setEditing(false);
          onWrite(changed);
        }}
        onCancel={() => setEditing(false)}
      />
    );
  }
  return (
    <div className="kept-instructions">
      <span className="label">按这些要求生成：</span>
      <p>{instructions}</p>
      <button disabled={disabled} onClick={() => setEditing(true)}>
        修改要求
      </button>
    </div>
  );
}

// One style in the panel: its digest when one is kept, or what it holds and the way to write
// it; while it is written, that it is.
function StylePane({
  itemId,
  offer,
  digest,
  writing,
  citing,
  onWrite,
}: {
  itemId: string;
  offer: StyleOffer;
  digest: Digest | undefined;
  writing: Writing | undefined;
  citing: Citing;
  onWrite: (style: ChosenStyle) => void;
}) {
  const style = offer.id;
  const busy = writing?.state === "writing";
  const failedInstructions =
    writing?.state === "failed" && writing.asked.style === CUSTOM
      ? writing.asked.instructions
      : undefined;
  const writeCustom = (instructions: string) => {
    localStorage.setItem(INSTRUCTIONS_KEY, instructions);
    onWrite({ style: CUSTOM, instructions });
  };
  const rewrite = () =>
    style === CUSTOM ? writeCustom(digest?.instructions ?? "") : onWrite({ style });
  return (
    <>
      {digest !== undefined && (
        <div className="digest-tools">
          <button disabled={busy} onClick={rewrite}>
            重新生成
          </button>
        </div>
      )}
      {writing?.state === "failed" && <p className="status failed">生成失败：{writing.reason}</p>}
      {busy ? (
        <p className="status">正在生成「{offer.name}」摘要…</p>
      ) : digest === undefined ? (
        <div className="unwritten">
          <p className="description">{offer.description}</p>
          {style === CUSTOM ? (
            <InstructionsBox
              prefilled={failedInstructions ?? localStorage.getItem(INSTRUCTIONS_KEY) ?? ""}
              disabled={busy}
              onWrite={writeCustom}
              onCancel={undefined}
            />
          ) : (
            <button className="write" onClick={() => onWrite({ style })}>
              生成
            </button>
          )}
        </div>
      ) : (
        <>
          {digest.instructions !== null && (
            <KeptInstructions
              instructions={digest.instructions}
              failedInstructions={failedInstructions}
              disabled={busy}
              onWrite={writeCustom}
            />
          )}
          <DigestBody itemId={itemId} digest={digest} citing={citing} />
        </>
      )}
    </>
  );
}

// The digests of the open item. Nothing is written until the reader chooses a style: with none
// kept or being written, the styles are offered, and a press of one writes it. Otherwise the
// style chosen last is on show if it is kept or being written, or else the one written last,
// or else one being written, under a row of every style.
function Digests({
  itemId,
  model,
  citing,
  styles,
  keptAtFirst,
  underWayAtFirst,
}: {
  itemId: string;
  model: string | undefined;
  citing: Citing;
  styles: StyleOffer[];
  keptAtFirst: Digest[];
  underWayAtFirst: ReadonlyMap<DigestStyle, DigestWriting>;
}) {
  const [kept, setKept] = useState(
    () => new Map(keptAtFirst.map((digest) => [digest.style, digest])),
  );
  const [writings, setWritings] = useState(
    () =>
      new Map<DigestStyle, Writing>(
        [...underWayAtFirst.keys()].map((style) => [style, { state: "writing" }]),
      ),
  );
  const [picked, setPicked] = useState<DigestStyle>();

  // The digest written goes among those kept whenever it arrives.
  function follow(writing: DigestWriting) {
    const style = writing.asked.style;
    writing.digest.then(
      (digest) => {
        setKept((before) => new Map(before).set(style, digest));
        setWritings((before) => {
          const after = new Map(before);
          after.delete(style);
          return after;
        });
      },
      (error: unknown) =>
        setWritings((before) =>
          new Map(before).set(style, {
            state: "failed",
            reason: reasonOf(error),
            asked: writing.asked,
          }),
        ),
    );
  }

  // The writings under way when the digests kept were read, which another view of the item
  // began, are followed from the start, so that one that ended meanwhile lands as well.
  useEffect(() => {
    for (const writing of underWayAtFirst.values()) {
      follow(writing);
    }
  }, []);

  function choose(style: DigestStyle) {
    localStorage.setItem(STYLE_KEY, style);
    setPicked(style);
  }

  // The chips of the digest that is written anew are gone, so the passage one of them showed
  // is taken off show with them.
  function write(asked: ChosenStyle) {
    citing.hide();
    choose(asked.style);
    setWritings((before) => new Map(before).set(asked.style, { state: "writing" }));
    follow(writeDigest(itemId, asked, model));
  }

  const lastChosen = localStorage.getItem(STYLE_KEY);
  const latest = [...kept.values()].sort(
    (one, other) => Date.parse(other.written) - Date.parse(one.written),
  )[0];
  // The styles that have something to show: a digest kept, or a writing under way or failed.
  const present = styles
    .map((offered) => offered.id)
    .filter((style) => kept.has(style) || writings.has(style));
  const shown =
    picked ?? present.find((style) => style === lastChosen) ?? latest?.style ?? present[0];
  const offer = styles.find((style) => style.id === shown);

  if (offer === undefined) {
    return (
      <div className="digest-offers">
        <p className="lead">还没有摘要。选一种风格生成：</p>
        <ul>
          {styles.map((style) => (
            <li key={style.id}>
              <button
                onClick={() =>
                  style.id === CUSTOM ? choose(style.id) : write({ style: style.id })
                }
              >
                <span className="name">{style.name}</span>
                <span className="description">{style.description}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <>
      <div className="digest-styles" role="group" aria-label="摘要风格">
        {styles.map((style) => (
          <button
            key={style.id}
            aria-pressed={style.id === offer.id}
            data-kept={kept.has(style.id)}
            data-writing={writings.get(style.id)?.state === "writing"}
            title={kept.has(style.id) ? `${style.description}（已生成）` : style.description}
            onClick={() => choose(style.id)}
          >
            {style.name}
          </button>
        ))}
      </div>
      <StylePane
        key={offer.id}
        itemId={itemId}
        offer={offer}
        digest={kept.get(offer.id)}
        writing={writings.get(offer.id)}
        citing={citing}
        onWrite={write}
      />
    </>
  );
}

// What infonium wrote about the open item, in the styles the reader chose. Its citations point
// into the reader, at passages of the document that is on show there. A digest is written by
// the model chosen when the reader asks for it; one that is kept stays when another model is
// chosen.
export function DigestPanel({
  itemId,
  model,
  citing,
}: {
  itemId: string;
  model: string | undefined;
  citing: Citing;
}) {
  // The writings under way are taken before the digests kept are read: one that ends while
  // they are read is then still followed, though the digests read may not hold its digest.
  const loaded = useLoaded(() => {
    const underWay = digestsUnderWay(itemId);
    return Promise.all([offeredStyles(), readDigests(itemId)]).then(([styles, kept]) => ({
      styles,
      kept,
      underWay,
    }));
  }, itemId);
  return (
    <div className="digest">
      {loaded.state === "loading" && <p className="status">正在读取摘要…</p>}
      {loaded.state === "failed" && <p className="status failed">读不到摘要：{loaded.reason}</p>}
      {loaded.state === "ready" && (
        <Digests
          itemId={itemId}
          model={model}
          citing={citing}
          styles={loaded.value.styles}
          keptAtFirst={loaded.value.kept}
          underWayAtFirst={loaded.value.underWay}
        />
      )}
    </div>
  );
}
