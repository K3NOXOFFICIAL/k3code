import { atom } from "nanostores";

import type { SessionActiveItem } from "../gatewayTypes.js";

export type StripState = "done" | "failed" | "input" | "working";

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

/** Rows currently painted, published by AgentStrip so the key handler can navigate them. */
export const $stripRows = atom<StripRow[]>([]);

export interface StripNav {
  /** Row key awaiting a y/n stop confirmation. */
  confirmKey: null | string;
  focused: boolean;
  index: number;
}

export const IDLE_NAV: StripNav = {
  confirmKey: null,
  focused: false,
  index: 0,
};

export const $stripNav = atom<StripNav>(IDLE_NAV);

export interface StripHandlers {
  activate: (row: StripRow) => void;
  stop: (row: StripRow) => void;
}

let handlers: StripHandlers | null = null;

export const setStripHandlers = (h: StripHandlers | null) => {
  handlers = h;
};

export const getStripHandlers = () => handlers;

export type StripKey = {
  ch?: string;
  down?: boolean;
  escape?: boolean;
  return?: boolean;
  up?: boolean;
};

export type StripEffect = { row: StripRow; type: "activate" | "stop" } | null;

/**
 * Pure key reducer. Returns the next nav state plus an optional side effect.
 * `consumed` is false when the key is not for the strip (caller keeps handling it).
 */
export function reduceStripKey(
  nav: StripNav,
  rows: readonly StripRow[],
  key: StripKey,
): { consumed: boolean; effect: StripEffect; nav: StripNav } {
  const none = { consumed: false, effect: null, nav };

  if (!nav.focused) {
    return none;
  }

  if (!rows.length) {
    return { consumed: true, effect: null, nav: IDLE_NAV };
  }

  const index = Math.min(nav.index, rows.length - 1);
  const row = rows[index]!;

  if (nav.confirmKey) {
    const ch = (key.ch ?? "").toLowerCase();

    if (ch === "y" || key.return) {
      const target = rows.find((r) => r.key === nav.confirmKey);

      return {
        consumed: true,
        effect: target ? { row: target, type: "stop" } : null,
        nav: { ...nav, confirmKey: null, index },
      };
    }

    return {
      consumed: true,
      effect: null,
      nav: { ...nav, confirmKey: null, index },
    };
  }

  if (key.escape) {
    return { consumed: true, effect: null, nav: IDLE_NAV };
  }

  if (key.up) {
    return {
      consumed: true,
      effect: null,
      nav: index === 0 ? IDLE_NAV : { ...nav, index: index - 1 },
    };
  }

  if (key.down) {
    return {
      consumed: true,
      effect: null,
      nav: { ...nav, index: Math.min(rows.length - 1, index + 1) },
    };
  }

  if (key.return) {
    return { consumed: true, effect: { row, type: "activate" }, nav: IDLE_NAV };
  }

  if ((key.ch ?? "").toLowerCase() === "x") {
    return {
      consumed: true,
      effect: null,
      nav: { ...nav, confirmKey: row.key, index },
    };
  }

  // Any other key is swallowed so stray typing cannot leak into the (unfocused) input.
  return { consumed: true, effect: null, nav: { ...nav, index } };
}

/** `↓` on an empty, history-idle input enters the strip when it has rows. */
export function shouldEnterStrip(opts: {
  historyIdx: null | number;
  input: string;
  rows: number;
}): boolean {
  return opts.rows > 0 && opts.input === "" && opts.historyIdx === null;
}
