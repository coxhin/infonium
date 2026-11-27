import { useCallback, useState } from "react";
import { requestReason } from "./client";

// What came of the last press on a control that asks the service: nothing to tell, an answer
// awaited, or a refusal and why. A refused control stays usable.
export type Press = { state: "idle" } | { state: "asking" } | { state: "refused"; reason: string };

const IDLE: Press = { state: "idle" };

export type Pressing = {
  press: Press;
  // Asks the service, and hands its answer on; a refusal is kept to be said beside the control.
  ask: <Answer>(asking: () => Promise<Answer>, answered: (answer: Answer) => void) => void;
};

export function usePress(): Pressing {
  const [press, setPress] = useState<Press>(IDLE);
  const ask = useCallback(
    <Answer>(asking: () => Promise<Answer>, answered: (answer: Answer) => void) => {
      setPress({ state: "asking" });
      asking().then(
        (answer) => {
          setPress(IDLE);
          answered(answer);
        },
        (error: unknown) => setPress({ state: "refused", reason: requestReason(error) }),
      );
    },
    [],
  );
  return { press, ask };
}
