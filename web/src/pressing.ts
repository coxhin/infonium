import { useCallback, useState } from "react";
import { requestReason } from "./client";

// What came of the last press on a control that asks the service: nothing to tell, an answer
// awaited, or a refusal and why. A refused control stays usable. While an answer is awaited the
// control is told as unusable (aria-disabled) rather than disabled, so that the keyboard stays
// on it.
export type Press = { state: "idle" } | { state: "asking" } | { state: "refused"; reason: string };

const IDLE: Press = { state: "idle" };

export type Pressing = {
  press: Press;
  // Asks the service, and hands its answer on; a refusal is kept to be said beside the control.
  // A press while an answer is awaited asks nothing.
  ask: <Answer>(asking: () => Promise<Answer>, answered: (answer: Answer) => void) => void;
};

export function usePress(): Pressing {
  const [press, setPress] = useState<Press>(IDLE);
  const awaited = press.state === "asking";
  const ask = useCallback(
    <Answer>(asking: () => Promise<Answer>, answered: (answer: Answer) => void) => {
      if (awaited) {
        return;
      }
      setPress({ state: "asking" });
      asking().then(
        (answer) => {
          setPress(IDLE);
          answered(answer);
        },
        (error: unknown) => setPress({ state: "refused", reason: requestReason(error) }),
      );
    },
    [awaited],
  );
  return { press, ask };
}
