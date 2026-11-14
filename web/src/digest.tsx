import { useEffect, useState } from "react";
import { ensureDigest, rewriteDigest, type Anchor, type Digest } from "./client";
import { reasonOf, useLoaded } from "./loading";
import { hideAnchor, showAnchor } from "./reader";

type Pointing =
  | { state: "nowhere" }
  | { state: "shown"; anchor: Anchor }
  | { state: "lost"; reason: string };

function DigestBody({ digest }: { digest: Digest }) {
  const [pointing, setPointing] = useState<Pointing>({ state: "nowhere" });
  useEffect(() => hideAnchor, []);

  function point(anchor: Anchor) {
    try {
      showAnchor(anchor);
      setPointing({ state: "shown", anchor });
    } catch (error) {
      hideAnchor();
      setPointing({ state: "lost", reason: reasonOf(error) });
    }
  }

  return (
    <>
      {pointing.state === "lost" && <p className="status failed">{pointing.reason}</p>}
      {digest.sections.map((section, sectionIndex) => (
        <section key={sectionIndex}>
          <h2>{section.title}</h2>
          <ul>
            {section.statements.map((statement, statementIndex) => (
              <li key={statementIndex}>
                {statement.text}
                {statement.anchors.map((anchor, anchorIndex) => (
                  <button
                    key={anchorIndex}
                    className="citation"
                    title={anchor.quote}
                    aria-pressed={pointing.state === "shown" && pointing.anchor === anchor}
                    onClick={() => point(anchor)}
                  >
                    {anchorIndex + 1}
                  </button>
                ))}
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
      <footer>
        {digest.model} · 输入 {digest.input_tokens.toLocaleString()} · 输出{" "}
        {digest.output_tokens.toLocaleString()} tokens
      </footer>
    </>
  );
}

export function DigestPanel({ itemId }: { itemId: string }) {
  const [round, setRound] = useState(0);
  const digest = useLoaded(
    () => (round === 0 ? ensureDigest(itemId) : rewriteDigest(itemId)),
    `${itemId}:${round}`,
  );
  return (
    <aside className="digest">
      <header>
        <h1>摘要</h1>
        <button disabled={digest.state === "loading"} onClick={() => setRound(round + 1)}>
          重新生成
        </button>
      </header>
      {digest.state === "loading" && <p className="status">正在生成摘要…</p>}
      {digest.state === "failed" && <p className="status failed">生成失败：{digest.reason}</p>}
      {digest.state === "ready" && <DigestBody digest={digest.value} />}
    </aside>
  );
}
