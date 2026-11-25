import { useEffect, useState, useSyncExternalStore } from "react";
import { readTranslationModels, type TranslationModels } from "./client";

// The model the reader has documents translated with, one of those the service offers. Like
// the setting, it is the reader's, not an item's: it stays as it was chosen when another item
// is opened, and over a reload of the page.
const MODEL_KEY = "infonium.translationModel";

export type ModelChoice = {
  // The models the service offers, once it has said them.
  models: TranslationModels | undefined;
  // The model a translation that starts or goes on is asked of; none: the service's default.
  chosen: string | undefined;
};

const listeners = new Set<() => void>();
// The service offers the same models until it starts again, so they are asked for once.
let offered: TranslationModels | undefined;

function storedModel(): string | null {
  return localStorage.getItem(MODEL_KEY);
}

// Another window of the app that changes the choice changes it here too.
function onModelChange(notify: () => void): () => void {
  listeners.add(notify);
  addEventListener("storage", notify);
  return () => {
    listeners.delete(notify);
    removeEventListener("storage", notify);
  };
}

function chooseModel(model: string): void {
  localStorage.setItem(MODEL_KEY, model);
  listeners.forEach((notify) => notify());
}

// The model chosen earlier is asked of as long as the service offers it ready; one it no longer
// offers, or cannot reach, gives way to the default. Before the service has said what it
// offers, the model chosen earlier is taken as it is, so that a translation that starts at
// once is made with it.
function chosenModel(stored: string | null, models: TranslationModels | undefined) {
  if (models === undefined) {
    return stored ?? undefined;
  }
  const usable = models.models.some((model) => model.name === stored && model.ready);
  return usable && stored !== null ? stored : models.default;
}

export function useModelChoice(): ModelChoice {
  const stored = useSyncExternalStore(onModelChange, storedModel);
  const [models, setModels] = useState(offered);
  useEffect(() => {
    if (offered !== undefined) {
      return;
    }
    let current = true;
    // Without the list there is nothing to choose from: the choice stays off the page, and a
    // translation is asked of the model chosen earlier, or of the default.
    readTranslationModels().then(
      (read) => {
        offered = read;
        if (current) {
          setModels(read);
        }
      },
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, []);
  return { models, chosen: chosenModel(stored, models) };
}

// The choice of the model, beside the setting, so that it can be made before a translation
// starts. A model the service cannot reach is offered but cannot be chosen.
export function ModelChoiceSelect({ choice }: { choice: ModelChoice }) {
  const { models, chosen } = choice;
  if (models === undefined || models.models.length < 2) {
    return null;
  }
  return (
    <label className="model-choice" title="开始、继续或全部重新翻译时用这个模型">
      翻译模型
      <select value={chosen} onChange={(event) => chooseModel(event.target.value)}>
        {models.models.map((model) => (
          <option key={model.name} value={model.name} disabled={!model.ready}>
            {model.ready ? model.name : `${model.name}（没有填 key）`}
          </option>
        ))}
      </select>
    </label>
  );
}
