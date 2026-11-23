import { useSyncExternalStore } from "react";

// Which words the reader shows a source in: those it was written in, their translation, or
// both, each block followed by its translation.
export type Setting = "source" | "translation" | "both";

const SETTING_NAMES: Record<Setting, string> = { source: "原文", translation: "译文", both: "对照" };
const SETTINGS = Object.keys(SETTING_NAMES) as Setting[];
const DEFAULT_SETTING: Setting = "source";
// The setting is the reader's, not an item's: it stays as it was set when another item is
// opened, and over a reload of the page.
const SETTING_KEY = "infonium.setting";

const listeners = new Set<() => void>();

function isSetting(value: string | null): value is Setting {
  return SETTINGS.some((setting) => setting === value);
}

function storedSetting(): Setting {
  const value = localStorage.getItem(SETTING_KEY);
  return isSetting(value) ? value : DEFAULT_SETTING;
}

// Another window of the app that changes the setting changes it here too.
function onSettingChange(notify: () => void): () => void {
  listeners.add(notify);
  addEventListener("storage", notify);
  return () => {
    listeners.delete(notify);
    removeEventListener("storage", notify);
  };
}

function chooseSetting(setting: Setting): void {
  localStorage.setItem(SETTING_KEY, setting);
  listeners.forEach((notify) => notify());
}

export function useSetting(): Setting {
  return useSyncExternalStore(onSettingChange, storedSetting);
}

export function SettingSwitch({ setting }: { setting: Setting }) {
  return (
    <div className="setting-switch" role="group" aria-label="显示的语言">
      {SETTINGS.map((choice) => (
        <button
          key={choice}
          aria-pressed={setting === choice}
          onClick={() => chooseSetting(choice)}
        >
          {SETTING_NAMES[choice]}
        </button>
      ))}
    </div>
  );
}
