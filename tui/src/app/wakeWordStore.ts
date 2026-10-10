import { atom } from "nanostores";

import { type WakeConfig, wakeConfig } from "@k3code/shared/wake-words";

// The `wake_words` config as last read from `config.get full`: the composer
// accent only promises the modes the gateway will run.
export const $wakeWordConfig = atom<WakeConfig>(wakeConfig(undefined));

export const setWakeWordConfig = (raw: unknown) =>
  $wakeWordConfig.set(wakeConfig(raw));
