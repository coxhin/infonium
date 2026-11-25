import { useEffect, useRef, type CSSProperties, type MouseEvent } from "react";
import { percent, type AreaText, type Box } from "./areas";
import { MarkedText } from "./blocks";
import { leadingRatioOf, type Setting } from "./fitting";
import { acrossWidth, cqw } from "./flowlengths";
import { edgesOf, type Flow } from "./flows";
import type { Colours } from "./pagedrawing";
import { useSelectedBlock, type MarkState, type SelectionDrawing } from "./pagemarking";

// A flow of a page's translation as it is drawn: its paper, its areas and what a press on it
// does.

// How long a press waits, in milliseconds, to be told from the first of a double press.
const DOUBLE_PRESS = 250;

// One area of a flow: it begins at its own left side and runs to the flow's right, after the
// gap its room leaves above it and the shares of spare room the gap takes. Its text is set at
// its print's size times its flow's step, led as its flow's leading says of its print.
function TranslatedArea({
  text,
  flow,
  index,
  shape,
  spacing,
  colours,
  ragged,
  pointed,
  cited,
  marked,
  drawing,
}: {
  text: AreaText;
  flow: Flow;
  index: number;
  shape: number;
  spacing: number;
  colours: Colours | undefined;
  ragged: boolean;
  // Whether the area holds the translation of a block a passage pointed at runs through: it
  // is then marked whole, as the text view marks the translated block.
  pointed: boolean;
  // Whether it holds the translation of the first line pointed at on its page.
  cited: boolean;
  // How the strongest of the highlights that run through the area's block is drawn, if any
  // does; and the selection, which the area is drawn in where it takes text of its block.
  marked: MarkState | undefined;
  drawing: SelectionDrawing;
}) {
  const selected = useSelectedBlock(drawing, text.blockNumber);
  const { area, piece } = text;
  const size = area.vertical ? area.typeset.size : area.typeset.size * shape;
  const ratio = leadingRatioOf(text, spacing);
  const gap = acrossWidth(flow, flow.gaps[index] ?? 0, shape);
  const style = {
    "--size": cqw(size),
    // A piece cut out of a line takes the flow's sides.
    marginInlineStart:
      area.vertical || flow.pieces.has(text.key) ? undefined : cqw(area.room.left - flow.room.left),
    marginBlockStart: `calc(${cqw(gap)} + var(--share) * ${flow.weights[index] ?? 0})`,
    lineHeight: `calc(var(--leading) * ${ratio})`,
    color: colours?.ink,
  } as CSSProperties;
  return (
    <div
      className={[
        "area",
        pointed ? "pointed" : "",
        marked !== undefined ? "marked" : "",
        selected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-mark={marked}
      style={style}
      data-block-translated={text.blockNumber}
      data-kind={text.kind}
      data-ragged={ragged ? "" : undefined}
      data-cited={cited ? "" : undefined}
    >
      {text.marker !== undefined && <span className="marker">{text.marker}</span>}
      <MarkedText text={piece.text} marks={piece.marks} />
    </div>
  );
}

// A flow of areas set as one run of text over the paper of its room. Pressing a cut flow or
// focusing it shows its text whole; where the source is to be seen under the translation,
// pressing it shows the source under all its areas until it is pressed again. Whether it is
// open is its page's to keep, by its areas.
export function TranslatedFlow({
  flow,
  setting,
  open,
  onTurn,
  onFocused,
  ragged,
  shape,
  spacing,
  colours,
  revealing,
  pointedBlocks,
  cited,
  markedBlocks,
  drawing,
}: {
  flow: Flow;
  setting: Setting;
  open: boolean;
  onTurn: (flow: Flow) => void;
  // Told when the flow takes the keyboard's focus, and when it gives it up while it stays on
  // show.
  onFocused: (flow: Flow | undefined) => void;
  ragged: ReadonlySet<string>;
  shape: number;
  spacing: number;
  colours: ReadonlyMap<string, Colours>;
  revealing: boolean;
  pointedBlocks: ReadonlySet<number>;
  cited: string | undefined;
  markedBlocks: ReadonlyMap<number, MarkState>;
  drawing: SelectionDrawing;
}) {
  const element = useRef<HTMLDivElement>(null);
  // A press turns the flow once it is plain that it selected nothing: a drag that selects its
  // text ends in a click too, and a double click begins with one.
  const turning = useRef<number>(undefined);
  useEffect(() => () => window.clearTimeout(turning.current), []);
  const pressed = (event: MouseEvent) => {
    window.clearTimeout(turning.current);
    if (event.detail > 1) {
      return;
    }
    turning.current = window.setTimeout(() => {
      const selection = getSelection();
      const selects =
        selection !== null &&
        !selection.isCollapsed &&
        element.current !== null &&
        selection.containsNode(element.current, true) &&
        selection.toString().trim() !== "";
      if (!selects) {
        onTurn(flow);
      }
    }, DOUBLE_PRESS);
  };
  const { room } = flow;
  const paper = colours.get(flow.key)?.paper;
  const place: Box = flow.vertical
    ? { ...room, left: room.left - setting.growth, width: room.width + setting.growth }
    : { ...room, height: room.height + setting.growth };
  // The paper is laid in the colour of the page's, and its edge in a part of the body's size.
  const paperStyle = (box: Box) => ({
    left: percent(box.left),
    top: percent(box.top),
    width: percent(box.width),
    height: percent(box.height),
    "--body": cqw(acrossWidth(flow, flow.print.size, shape)),
    backgroundColor: paper,
    "--paper": paper,
  });
  const edges = edgesOf(flow, setting.growth);
  const style = {
    ...paperStyle(place),
    "--edge-top": edges.top ? undefined : 0,
    "--edge-foot": edges.foot ? undefined : 0,
    "--leading": setting.leading,
    "--step": setting.step,
    "--share": cqw(acrossWidth(flow, setting.share, shape)),
  } as CSSProperties;
  const pressable = setting.cut || revealing;
  return (
    <>
      <div
        ref={element}
        className={flow.vertical ? "flow vertical" : "flow"}
        style={style}
        data-leading={setting.leading.toFixed(2)}
        data-step={setting.step.toFixed(2)}
        data-cut={setting.cut ? "" : undefined}
        data-open={open ? "" : undefined}
        data-bare={edges.top && edges.foot ? undefined : ""}
        tabIndex={pressable ? 0 : undefined}
        role={pressable ? "button" : undefined}
        aria-pressed={pressable ? open : undefined}
        aria-label={revealing ? "按下看这几段的原文" : undefined}
        onClick={pressable ? pressed : undefined}
        onKeyDown={
          pressable
            ? (event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onTurn(flow);
                }
              }
            : undefined
        }
        onFocus={() => onFocused(flow)}
        onBlur={(event) => {
          // A flow taken off the page loses the focus too; only one still on show gave it up.
          const left = event.currentTarget;
          queueMicrotask(() => {
            if (left.isConnected) {
              onFocused(undefined);
            }
          });
        }}
      >
        {flow.texts.map((text, index) => (
          <TranslatedArea
            key={text.key}
            text={text}
            flow={flow}
            index={index}
            shape={shape}
            spacing={spacing}
            colours={colours.get(text.key)}
            ragged={ragged.has(text.key)}
            pointed={pointedBlocks.has(text.blockNumber)}
            cited={cited === text.key}
            marked={markedBlocks.get(text.blockNumber)}
            drawing={drawing}
          />
        ))}
      </div>
      {flow.tails.map((tail, index) => (
        <div
          key={index}
          className="paper"
          aria-hidden
          data-open={open ? "" : undefined}
          style={paperStyle(tail) as CSSProperties}
        />
      ))}
    </>
  );
}
