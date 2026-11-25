import { useState } from "react";
import { ensureDigest, rewriteDigest, type Digest } from "./client";
import { Citations, LostCitations, type Citing } from "./citing";
import { useLoaded } from "./loading";
import { NOTE_GUTTER, NoteDown, statementTaking } from "./notedown";
import "./digest.css";

function DigestBody({
  itemId,
  digest,
  citing,
}: {
  itemId: string;
  digest: Digest;
  citing: Citing;
}) {
  return (
    <>
      {digest.sections.map((section, sectionIndex) => (
        <section key={sectionIndex}>
          <h2>{section.title}</h2>
          <ul>
            {section.statements.map((statement, statementIndex) => (
              <li key={statementIndex} className={NOTE_GUTTER}>
                {statement.text}
                <Citations
                  anchors={statement.anchors}
                  citing={citing}
                  titleOf={(anchor) => anchor.quote}
                  onPoint={(anchor) => citing.point([anchor])}
                />
                <LostCitations statement={statement} citesAll />
                <NoteDown
                  taking={statementTaking([statement], () => itemId)}
                  label="记到笔记：这句摘要"
                  shape="mark"
                />
              </li>
            ))}
          </ul>
        </section>
      ))}
      <footer className="spent">
        {digest.model} · 输入 {digest.input_tokens.toLocaleString()} · 输出{" "}
        {digest.output_tokens.toLocaleString()} tokens
      </footer>
    </>
  );
}

// What infonium wrote about the open item. Its citations point into the reader, at passages
// of the document that is on show there.
// A digest is written by the model chosen when it is first asked for or written anew; one that
// is kept stays when another model is chosen.
export function DigestPanel({
  itemId,
  model,
  citing,
}: {
  itemId: string;
  model: string | undefined;
  citing: Citing;
}) {
  const [round, setRound] = useState(0);
  const digest = useLoaded(
    () => (round === 0 ? ensureDigest(itemId, model) : rewriteDigest(itemId, model)),
    `${itemId}:${round}`,
  );

  // The chips of the digest that is written anew are gone, so the passage one of them showed
  // is taken off show with them.
  function rewrite() {
    citing.hide();
    setRound(round + 1);
  }

  return (
    <div className="digest">
      <header>
        <button disabled={digest.state === "loading"} onClick={rewrite}>
          重新生成
        </button>
      </header>
      {digest.state === "loading" && <p className="status">正在生成摘要…</p>}
      {digest.state === "failed" && <p className="status failed">生成失败：{digest.reason}</p>}
      {digest.state === "ready" && <DigestBody itemId={itemId} digest={digest.value} citing={citing} />}
    </div>
  );
}
