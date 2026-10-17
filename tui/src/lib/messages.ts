import { MAX_HISTORY } from "../config/limits.js";
import type { Msg, Role } from "../types.js";

import { appendToolShelfMessage } from "./liveProgress.js";

// Stamp live rows AT APPEND (wall clock, Unix seconds) rather than later:
// a message's authoring time is when it entered the transcript, not when it
// happened to be persisted or re-rendered (#82840-class rule). Rehydrated
// rows arrive with their persisted `createdAt` and keep it.
export const appendTranscriptMessage = (prev: Msg[], msg: Msg): Msg[] =>
  appendToolShelfMessage(
    prev,
    msg.createdAt === undefined
      ? { ...msg, createdAt: Date.now() / 1000 }
      : msg,
  );

// Keeps the transcript at MAX_HISTORY rows. Trimmed rows leave a dim "N earlier
// rows not shown" event row (right after the intro) so the cut is never silent;
// it counts as one of the MAX_HISTORY rows and its `hiddenRows` accumulates
// across later trims instead of stacking markers (#150).
export const capTranscriptHistory = (items: Msg[]): Msg[] => {
  if (items.length <= MAX_HISTORY) {
    return items;
  }

  const intro = items[0]?.kind === "intro" ? items[0] : undefined;
  const rest = items.slice(intro ? 1 : 0);
  const prior = rest[0]?.hiddenRows;
  const rows = prior === undefined ? rest : rest.slice(1);
  const kept = rows.slice(-(MAX_HISTORY - (intro ? 2 : 1)));
  const hiddenRows = (prior ?? 0) + rows.length - kept.length;

  const marker: Msg = {
    hiddenRows,
    kind: "event",
    role: "system",
    text: `${hiddenRows} earlier rows not shown`,
  };

  return intro ? [intro, marker, ...kept] : [marker, ...kept];
};

export const upsert = (prev: Msg[], role: Role, text: string): Msg[] =>
  prev.at(-1)?.role === role
    ? [...prev.slice(0, -1), { role, text }]
    : [...prev, { role, text }];
