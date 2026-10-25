import { atom } from "nanostores";

import type { SessionActiveItem } from "../gatewayTypes.js";

/** `idle` (a session with no turn yet) is only assigned by the agent view; the strip shows such sessions as `done`. */
export type StripState = "done" | "failed" | "idle" | "input" | "working";

export interface StripRow {
  activity: string;
  elapsedSeconds: null | number;
  /** `session` rows attach via session.activate; `agent` rows are in-turn sub-agents. */
  kind: "agent" | "session";
  id: string;
  key: string;
  state: StripState;
  title: string;
}

export const STRIP_MAX_ROWS = 6;

/** Background sessions from the `session.active_list` poll (current session excluded). */
export const $stripSessions = atom<SessionActiveItem[]>([]);

// `current` is left out: the poll marks the TUI's own session, the pushed `session.active_list` event marks none.
const snapshotKey = (rows: readonly SessionActiveItem[]) =>
  JSON.stringify(rows.map(({ current: _current, ...row }) => row));

/** Store a poll reply or pushed snapshot; an unchanged one sets nothing, so the idle TUI does not re-render. */
export function applyStripSessions(rows: SessionActiveItem[]) {
  if (snapshotKey($stripSessions.get()) !== snapshotKey(rows)) {
    $stripSessions.set(rows);
  }
}

/** Row actions the agent view calls (attach a session or open an agent, stop a row); registered by useMainApp. */
export interface StripHandlers {
  activate: (row: StripRow) => void;
  stop: (row: StripRow) => void;
}

let handlers: StripHandlers | null = null;

export const setStripHandlers = (h: StripHandlers | null) => {
  handlers = h;
};

export const getStripHandlers = () => handlers;
