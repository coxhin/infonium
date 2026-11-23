import { useState } from "react";
import { ensureDigest, rewriteDigest, type Digest } from "./client";
import { Citations, type Citing } from "./citing";
import { useLoaded } from "./loading";
import "./digest.css";

export type { Pointer } from "./citing";

function DigestBody({ digest, citing }: { digest: Digest; citing: Citing }) {
  return (
    <>
      {digest.sections.map((section, sectionIndex) => (
        <section key={sectionIndex}>
          <h2>{section.title}</h2>
          <ul>
            {section.statements.map((statement, statementIndex) => (
              <li key={statementIndex}>
                {statement.text}
                <Citations
                  anchors={statement.anchors}
                  citing={citing}
                  titleOf={(anchor) => anchor.quote}
                  onPoint={citing.point}
                />
                {statement.anchors.length === 0 && (
                  <span className="unsourced" title="模型给出的引句在原文里找不到">
                    无出处
                  </span>
                )}
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
export function DigestPanel({ itemId, citing }: { itemId: string; citing: Citing }) {
  const [round, setRound] = useState(0);
  const digest = useLoaded(
    () => (round === 0 ? ensureDigest(itemId) : rewriteDigest(itemId)),
    `${itemId}:${round}`,
  );
  return (
    <div className="digest">
      <header>
        <button disabled={digest.state === "loading"} onClick={() => setRound(round + 1)}>
          重新生成
        </button>
      </header>
      {digest.state === "loading" && <p className="status">正在生成摘要…</p>}
      {digest.state === "failed" && <p className="status failed">生成失败：{digest.reason}</p>}
      {digest.state === "ready" && <DigestBody digest={digest.value} citing={citing} />}
    </div>
  );
}
