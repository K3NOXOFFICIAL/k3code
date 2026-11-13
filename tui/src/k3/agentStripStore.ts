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

const withoutCurrent = ({ current: _current, ...row }: SessionActiveItem) =>
  JSON.stringify(row);

/**
 * Whether two snapshots say the same. `current` counts only where both sides state it: a core that does not mark it per
 * client pushes rows with none while the poll marks the TUI's own session, and that alone changes nothing; the mark
 * moving from one session to another (the TUI switched) does.
 */
const sameSnapshot = (
  a: readonly SessionActiveItem[],
  b: readonly SessionActiveItem[],
) =>
  a.length === b.length &&
  a.every((row, i) => {
    const other = b[i] as SessionActiveItem;

    return (
      withoutCurrent(row) === withoutCurrent(other) &&
      (row.current === undefined ||
        other.current === undefined ||
        row.current === other.current)
    );
  });

/**
 * Store a poll reply or pushed snapshot; an unchanged one sets nothing, so the idle TUI does not re-render. A pushed
 * snapshot marks no row current: its rows take the mark from `currentSid` (the TUI's own session) when known, else from
 * the stored copy, since the poll that would bring the mark back is deduped against them.
 */
export function applyStripSessions(
  rows: SessionActiveItem[],
  currentSid: null | string = null,
) {
  const stored = $stripSessions.get();

  if (sameSnapshot(stored, rows)) {
    return;
  }

  const wasCurrent = new Set(stored.filter((s) => s.current).map((s) => s.id));

  $stripSessions.set(
    rows.map((s) =>
      s.current === undefined &&
      (currentSid != null ? s.id === currentSid : wasCurrent.has(s.id))
        ? { ...s, current: true }
        : s,
    ),
  );
}

/** Bumped when the gateway is ready again after a drop: what the agent view loaded before it is no longer known. */
export const $gatewayEpoch = atom(0);

/**
 * The gateway dropped: the rows cannot be told from stale ones (a session that died with it would show "working" for
 * good), so the strip empties until the next snapshot after the reconnect brings the real ones.
 */
export function stripOffline() {
  if ($stripSessions.get().length) {
    $stripSessions.set([]);
  }
}

export function stripBackOnline() {
  $gatewayEpoch.set($gatewayEpoch.get() + 1);
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
