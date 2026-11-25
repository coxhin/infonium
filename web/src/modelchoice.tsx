import { useEffect, useState, useSyncExternalStore } from "react";
import { readModels, type ModelsOffered } from "./client";
import "./modelchoice.css";

// The models the reader has work done with, each one of those the service offers: one that
// translates documents, and one that writes digests, answers questions and translates one
// block again. Like the setting, each choice is the reader's, not an item's: it stays as it
// was chosen when another item is opened, and over a reload of the page.
export type Purpose = "translation" | "answering";

const CHOICE_KEYS: Record<Purpose, string> = {
  translation: "infonium.translationModel",
  answering: "infonium.answeringModel",
};

export type ModelChoice = {
  // The models the service offers, once it has said them.
  offered: ModelsOffered | undefined;
  // The model the work is asked of; none: the service's default.
  chosen: string | undefined;
  choose: (model: string) => void;
};

const listeners = new Set<() => void>();
// The service offers the same models until it starts again, so they are asked for once.
let offeredOnce: ModelsOffered | undefined;

// Another window of the app that changes a choice changes it here too.
function onChoiceChange(notify: () => void): () => void {
  listeners.add(notify);
  addEventListener("storage", notify);
  return () => {
    listeners.delete(notify);
    removeEventListener("storage", notify);
  };
}

// The model chosen earlier is asked of as long as the service offers it ready; one it no longer
// offers, or cannot reach, gives way to the default. Before the service has said what it
// offers, the model chosen earlier is taken as it is, so that work that starts at once is
// done with it.
function chosenModel(
  stored: string | null,
  offered: ModelsOffered | undefined,
  purpose: Purpose,
): string | undefined {
  if (offered === undefined) {
    return stored ?? undefined;
  }
  const usable = offered.models.some((model) => model.name === stored && model.ready);
  return usable && stored !== null ? stored : offered[purpose];
}

export function useModelChoice(purpose: Purpose): ModelChoice {
  const key = CHOICE_KEYS[purpose];
  const stored = useSyncExternalStore(onChoiceChange, () => localStorage.getItem(key));
  const [offered, setOffered] = useState(offeredOnce);
  useEffect(() => {
    if (offeredOnce !== undefined) {
      return;
    }
    let current = true;
    // Without the list there is nothing to choose from: the choice stays off the page, and
    // the work is asked of the model chosen earlier, or of the default.
    readModels().then(
      (read) => {
        offeredOnce = read;
        if (current) {
          setOffered(read);
        }
      },
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, []);
  return {
    offered,
    chosen: chosenModel(stored, offered, purpose),
    choose: (model) => {
      localStorage.setItem(key, model);
      listeners.forEach((notify) => notify());
    },
  };
}

// A choice of the model, where it can be made before the work starts. A model the service
// cannot reach is offered but cannot be chosen.
export function ModelChoiceSelect({
  choice,
  label,
  title,
}: {
  choice: ModelChoice;
  label: string;
  title: string;
}) {
  const { offered, chosen } = choice;
  if (offered === undefined || offered.models.length < 2) {
    return null;
  }
  return (
    <label className="model-choice" title={title}>
      {label}
      <select value={chosen} onChange={(event) => choice.choose(event.target.value)}>
        {offered.models.map((model) => (
          <option key={model.name} value={model.name} disabled={!model.ready}>
            {model.ready ? model.name : `${model.name}（没有填 key）`}
          </option>
        ))}
      </select>
    </label>
  );
}
