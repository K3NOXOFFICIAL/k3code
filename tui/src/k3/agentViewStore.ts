import type { SessionListRow } from "@k3code/shared/gateway-events";

import { resumableHistory } from "../components/activeSessionSwitcher.js";
import type { SessionActiveItem } from "../gatewayTypes.js";
import type { SubagentProgress } from "../types.js";

import { buildStripRows, sessionRow } from "./agentStrip.js";
import type { StripRow, StripState } from "./agentStripStore.js";

export type ViewGroup = "completed" | "input" | "working";

export const VIEW_GROUPS: readonly ViewGroup[] = [
  "input",
  "working",
  "completed",
];

export const VIEW_GROUP_LABEL: Record<ViewGroup, string> = {
  completed: "Completed",
  input: "Needs input",
  working: "Working",
};

export interface ViewRow extends Omit<StripRow, "kind"> {
  current: boolean;
  group: ViewGroup;
  kind: "agent" | "past" | "session";
  lastActive?: null | number;
  model?: string;
}

/** `session.list` row. The k3code gateway adds `cwd` (null when unknown); the generated Hermes contract lacks it. */
export type PastSessionRow = SessionListRow & { cwd?: null | string };

/** Idle, done and failed all land in Completed; each row keeps its own glyph. */
const groupOf = (state: StripState): ViewGroup =>
  state === "input" ? "input" : state === "working" ? "working" : "completed";

/**
 * A session that has not had a turn yet: nothing ran, so "completed" would be a lie. Only a known zero counts; an
 * unknown message count keeps the strip's `done`.
 */
const noTurnYet = (status: string, messageCount: number | undefined) =>
  (status === "idle" || status === "new") && messageCount === 0;

export function buildViewRows({
  currentCwd,
  currentSid,
  nowMs,
  past,
  sessions,
  subagents,
}: {
  currentCwd?: string;
  currentSid: null | string;
  nowMs: number;
  past: readonly PastSessionRow[];
  sessions: readonly SessionActiveItem[];
  subagents: readonly SubagentProgress[];
}): ViewRow[] {
  const byId = new Map(sessions.map((s) => [s.id, s]));
  const live = (r: StripRow, current: boolean): ViewRow => {
    const s = r.kind === "session" ? byId.get(r.id) : undefined;
    // The view's own reading; the inline strip keeps showing such sessions as completed.
    const state: StripState =
      s && noTurnYet(s.status, s.message_count) ? "idle" : r.state;

    return {
      ...r,
      current,
      group: groupOf(state),
      state,
      lastActive: s?.last_active ?? null,
      ...(s?.model ? { model: s.model } : {}),
    };
  };

  // By id first: a stale `current` flag on another row must not take the lead while the TUI knows its session.
  const cur =
    sessions.find((s) => s.id === currentSid) ??
    (currentSid == null ? sessions.find((s) => s.current) : undefined);
  const strip = buildStripRows(subagents, sessions, nowMs, currentSid).map(
    (r) => live(r, false),
  );
  // The current session leads its group: it is where the user came from.
  const liveRows = cur ? [live(sessionRow(cur, nowMs), true), ...strip] : strip;

  // A past session only belongs here when it ran in this project; rows without a cwd cannot be placed, so they stay out.
  const pastRows: ViewRow[] = (
    resumableHistory(past, sessions) as PastSessionRow[]
  )
    // An empty stored session (opened, never used) is noise, not history.
    .filter((h) => h.message_count !== 0)
    .filter((h) => !!h.cwd && h.cwd === currentCwd)
    .map((h, i) => ({ h, i }))
    .sort((a, b) => (b.h.started_at ?? 0) - (a.h.started_at ?? 0) || a.i - b.i)
    .map(({ h }) => ({
      activity: h.preview?.trim() ?? "",
      current: false,
      elapsedSeconds: null,
      group: "completed",
      id: h.id,
      key: `past:${h.id}`,
      kind: "past",
      lastActive: h.started_at ?? null,
      state: "done",
      title: h.title?.trim() || h.preview?.trim() || h.id.slice(0, 8),
    }));

  // Earlier sessions close the Completed group, after the live rows.
  return [
    ...VIEW_GROUPS.flatMap((g) => liveRows.filter((r) => r.group === g)),
    ...pastRows,
  ];
}

export interface ViewNav {
  /** Row key awaiting a y/n stop confirmation. */
  confirmKey: null | string;
  index: number;
}

export const IDLE_VIEW_NAV: ViewNav = { confirmKey: null, index: 0 };

export type ViewKey = {
  ch?: string;
  ctrl?: boolean;
  down?: boolean;
  end?: boolean;
  escape?: boolean;
  home?: boolean;
  left?: boolean;
  meta?: boolean;
  pageDown?: boolean;
  pageUp?: boolean;
  return?: boolean;
  right?: boolean;
  up?: boolean;
};

export type ViewEffect =
  | { row: ViewRow; type: "activate" | "stop" }
  | { type: "close" }
  | { type: "new" };

/**
 * Selected row by key, so a refresh that reorders or inserts rows keeps the cursor on the same row. Only when that row
 * has gone does the old position (clamped) stand in.
 */
export function selectedIndex(
  rows: readonly ViewRow[],
  key: null | string,
  fallback: number,
): number {
  const i = key == null ? -1 : rows.findIndex((r) => r.key === key);

  return i >= 0 ? i : Math.max(0, Math.min(fallback, rows.length - 1));
}

/** Live sessions other than this one and in-turn agents can be stopped; past rows and the current session cannot. */
export const isStoppable = (row: ViewRow) =>
  row.kind === "agent" || (row.kind === "session" && !row.current);

/** Pure key reducer for the full-screen view; it owns the screen, so every key is consumed. */
export function reduceViewKey(
  nav: ViewNav,
  rows: readonly ViewRow[],
  key: ViewKey,
  pageSize: number,
): { consumed: true; effect: null | ViewEffect; nav: ViewNav } {
  const last = rows.length - 1;
  const index = Math.max(0, Math.min(nav.index, last));
  // With Ctrl/Meta held the letter is a chord (Ctrl+N, Ctrl+X), not a view command.
  const ch = key.ctrl || key.meta ? "" : (key.ch ?? "").toLowerCase();
  const done = (effect: null | ViewEffect, next: Partial<ViewNav> = {}) => ({
    consumed: true as const,
    effect,
    nav: { confirmKey: null, index, ...next },
  });
  // A pending confirmation eats the next key and is cleared. Only y/⏎ on a target still listed stops (→ cancels like any
  // other key); when the row has gone (stopped elsewhere, list refreshed) the key does nothing, so ⏎ cannot attach to
  // whatever row moved under it.
  if (nav.confirmKey) {
    const target = rows.find((r) => r.key === nav.confirmKey);

    return done(
      target && !key.escape && !key.left && (ch === "y" || key.return)
        ? { row: target, type: "stop" }
        : null,
    );
  }

  if (key.escape || key.left) {
    return done({ type: "close" });
  }

  if (ch === "n") {
    return done({ type: "new" });
  }

  if (!rows.length) {
    return done(null);
  }

  const step = Math.max(1, pageSize);
  const move = (to: number) =>
    done(null, { index: Math.max(0, Math.min(last, to)) });

  if (key.up) {
    return move(index - 1);
  }

  if (key.down) {
    return move(index + 1);
  }

  if (key.home) {
    return move(0);
  }

  if (key.end) {
    return move(last);
  }

  if (key.pageUp) {
    return move(index - step);
  }

  if (key.pageDown) {
    return move(index + step);
  }

  const row = rows[index]!;

  if (key.return || key.right) {
    return done(row.current ? { type: "close" } : { row, type: "activate" });
  }

  if (ch === "x" && isStoppable(row)) {
    return done(null, { confirmKey: row.key });
  }

  return done(null);
}

/**
 * Window of `height` lines out of `total` that keeps `index` visible, moving the previous window (`prevStart`) as
 * little as possible. `end` is exclusive.
 */
export function visibleWindow(
  total: number,
  index: number,
  height: number,
  prevStart = 0,
): { end: number; start: number } {
  const h = Math.max(1, height);

  if (total <= h) {
    return { end: Math.max(0, total), start: 0 };
  }

  const i = Math.max(0, Math.min(index, total - 1));
  const start = Math.max(
    0,
    Math.min(Math.max(i - h + 1, Math.min(prevStart, i)), total - h),
  );

  return { end: start + h, start };
}
