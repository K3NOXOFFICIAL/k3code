import type { SessionListRow } from "@k3code/shared/gateway-events";
import { atom } from "nanostores";

import { resumableHistory } from "../components/activeSessionSwitcher.js";
import type { SessionActiveItem } from "../gatewayTypes.js";
import type { SubagentProgress } from "../types.js";

import { buildStripRows, sessionRow } from "./agentStrip.js";
import type { StripRow, StripState } from "./agentStripStore.js";

export type ViewGroup = "completed" | "input" | "new" | "working";

/** `new` holds only the synthetic "+ New agent" row; it has no header and no count. */
export const VIEW_GROUPS: readonly ViewGroup[] = [
  "new",
  "input",
  "working",
  "completed",
];

export const VIEW_GROUP_LABEL: Record<ViewGroup, string> = {
  completed: "Completed",
  input: "Needs input",
  new: "",
  working: "Working",
};

export interface ViewRow extends Omit<StripRow, "kind"> {
  current: boolean;
  group: ViewGroup;
  kind: "agent" | "new" | "past" | "session";
  lastActive?: null | number;
  model?: string;
}

/** `session.list` row. The k3code gateway adds `cwd` (null when unknown); the generated Hermes contract lacks it. */
export type PastSessionRow = SessionListRow & { cwd?: null | string };

/** The row above every session: text typed on it starts a new background agent. */
export const NEW_AGENT_ROW: ViewRow = {
  activity: "",
  current: false,
  elapsedSeconds: null,
  group: "new",
  id: "",
  key: "new",
  kind: "new",
  state: "idle",
  title: "+ New agent",
};

/** The view's rows: "+ New agent" first, then every session (`buildViewRows`). */
export const withNewAgentRow = (rows: readonly ViewRow[]): ViewRow[] => [
  NEW_AGENT_ROW,
  ...rows,
];

/**
 * Where the selection rests until the user moves: this session's row, else the first live row, else "+ New agent". An
 * earlier session is never the default, so typed text does not resume one by surprise.
 */
export const defaultViewKey = (rows: readonly ViewRow[]): null | string =>
  (
    rows.find((r) => r.current) ??
    rows.find((r) => r.kind === "session" || r.kind === "agent") ??
    rows[0]
  )?.key ?? null;

/** The view's composer draft. An atom, so it survives the view closing (an approval closes it) and reopening. */
export const $agentViewDraft = atom<string>("");
export const getAgentViewDraft = () => $agentViewDraft.get();
export const setAgentViewDraft = (v: string) => $agentViewDraft.set(v);

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

export type ConfirmAction = "delete" | "stop";

export interface ViewNav {
  /** What the pending y/n confirmation does; `stop` when unset. */
  confirmAction?: ConfirmAction;
  /** Row key awaiting a y/n confirmation. */
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
  | { row: ViewRow; type: "activate" | ConfirmAction }
  | { text: string; type: "dispatch" }
  | { type: "clearDraft" | "close" | "new" };

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

/** Earlier sessions and live sessions other than this one can be deleted (after a y/n). */
export const isDeletable = (row: ViewRow) =>
  row.kind === "past" || (row.kind === "session" && !row.current);

/**
 * Pure key reducer for the full-screen view. The view's composer (a TextInput) sees every key first, so text keys and
 * Enter are left to it (`consumed: false`); ←/→/Home/End only act on the list while `draft` is empty. A pending y/n
 * confirmation eats the next key (the composer is unfocused meanwhile).
 */
export function reduceViewKey(
  nav: ViewNav,
  rows: readonly ViewRow[],
  key: ViewKey,
  pageSize: number,
  draft = "",
): { consumed: boolean; effect: null | ViewEffect; nav: ViewNav } {
  const last = rows.length - 1;
  const index = Math.max(0, Math.min(nav.index, last));
  // Only Ctrl chords are view commands (Ctrl+N, Ctrl+X); Alt+letter belongs to the composer.
  const chord = key.ctrl && !key.meta ? (key.ch ?? "").toLowerCase() : "";
  const ch = key.ctrl || key.meta ? "" : (key.ch ?? "").toLowerCase();
  const done = (
    effect: null | ViewEffect,
    next: Partial<ViewNav> = {},
    consumed = true,
  ) => ({ consumed, effect, nav: { confirmKey: null, index, ...next } });
  const pass = () => done(null, {}, false);
  // A pending confirmation eats the next key and is cleared. Only y/⏎ on a target still listed acts (→ cancels like any
  // other key); when the row has gone (stopped elsewhere, list refreshed) the key does nothing, so ⏎ cannot act on
  // whatever row moved under it.
  if (nav.confirmKey) {
    const target = rows.find((r) => r.key === nav.confirmKey);

    return done(
      target && !key.escape && !key.left && (ch === "y" || key.return)
        ? { row: target, type: nav.confirmAction ?? "stop" }
        : null,
    );
  }

  const typed = draft.trim();

  if (chord === "n") {
    // Ctrl+N: an empty composer starts a new foreground session; with text it starts a background agent on it.
    return done(typed ? { text: typed, type: "dispatch" } : { type: "new" });
  }

  if (key.escape) {
    return done(draft ? { type: "clearDraft" } : { type: "close" });
  }

  // With text in the composer ←/→/Home/End edit it.
  if (draft && (key.left || key.right || key.home || key.end)) {
    return pass();
  }

  if (key.left) {
    return done({ type: "close" });
  }

  if (!rows.length) {
    return key.up || key.down || key.pageUp || key.pageDown
      ? done(null)
      : pass();
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

  if (key.right) {
    return done(row.current ? { type: "close" } : { row, type: "activate" });
  }

  if (chord === "x") {
    return isStoppable(row)
      ? done(null, { confirmAction: "stop", confirmKey: row.key })
      : done(null);
  }

  // Enter is the composer's (its onSubmit), and every other key is text.
  return pass();
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

/** A result line under the list: the transcript is hidden while the view is open, so view actions report here. */
export interface ViewNotice {
  /** A background agent's new session, so the selection can follow it once the poll lists it. */
  sessionId?: string;
  text: string;
  tone: "error" | "info";
}

/** What a submitted composer line does, decided from the text and the selected row. */
export type ViewAction =
  | { action: ConfirmAction; row: ViewRow; type: "confirm" }
  | { notice: ViewNotice; type: "notice" }
  | { row: ViewRow; text: string; type: "resumeAndSend" | "sendTo" | "steer" }
  | { row: ViewRow; title: string; type: "rename" }
  | { row: ViewRow; type: "activate" | "closeSession" }
  | { text: string; type: "dispatch" | "main" | "sendCurrent" }
  | { type: "new" | "none" };

const refuse = (text: string): ViewAction => ({
  notice: { text, tone: "error" },
  type: "notice",
});

const IN_THIS_SESSION = "you are in this session";

/** The commands the view runs itself, on the selected row; `null` for any other name. */
function viewCommand(
  name: string,
  arg: string,
  row: ViewRow,
): null | ViewAction {
  const session = row.kind === "session" || row.kind === "past";

  switch (name) {
    case "agents":
      return { type: "none" };
    case "attach":
    case "resume":
      return { row, type: "activate" };
    case "bg":
      return arg
        ? { text: arg, type: "dispatch" }
        : refuse("usage: /bg <prompt>");
    case "close":
      return row.kind !== "session"
        ? refuse("only a running session can be closed")
        : row.current
          ? refuse(IN_THIS_SESSION)
          : { row, type: "closeSession" };
    case "delete":
      return row.current
        ? refuse(IN_THIS_SESSION)
        : isDeletable(row)
          ? { action: "delete", row, type: "confirm" }
          : refuse("only a session can be deleted");
    case "new":
      return { type: "new" };
    case "rename":
    case "title":
      return !session
        ? refuse("only a session can be renamed")
        : arg
          ? { row, title: arg, type: "rename" }
          : refuse(`usage: /${name} <title>`);
    case "stop":
      return isStoppable(row)
        ? { action: "stop", row, type: "confirm" }
        : refuse("nothing to stop on this row");
  }

  return null;
}

/**
 * Route a submitted, non-empty composer line. View commands act on `row`; other `/cmd` and `!cmd` leave the view and
 * run in the attached session; plain text goes to the selected row (new agent, this session, another session, a
 * sub-agent as steering, or an earlier session that is resumed first).
 */
export function routeViewInput(text: string, row: ViewRow): ViewAction {
  const line = text.trim();

  if (line.startsWith("/")) {
    const m = /^\/(\S+)\s*([\s\S]*)$/.exec(line);
    const own = m ? viewCommand(m[1]!.toLowerCase(), m[2]!.trim(), row) : null;

    return own ?? { text: line, type: "main" };
  }

  if (line.startsWith("!")) {
    return { text: line, type: "main" };
  }

  switch (row.kind) {
    case "new":
      return { text: line, type: "dispatch" };
    case "agent":
      return { row, text: line, type: "steer" };
    case "past":
      return { row, text: line, type: "resumeAndSend" };
    case "session":
      return row.current
        ? { text: line, type: "sendCurrent" }
        : { row, text: line, type: "sendTo" };
  }
}
