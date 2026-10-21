import type { SessionListRow } from "@k3code/shared/gateway-events";

import { resumableHistory } from "../components/activeSessionSwitcher.js";
import { stripControl } from "../lib/text.js";
import type { SessionActiveItem } from "../gatewayTypes.js";
import type { SubagentProgress } from "../types.js";

import { buildStripRows, sessionRow } from "./agentStrip.js";
import type { StripRow, StripState } from "./agentStripStore.js";

export type ViewGroup = "archived" | "completed" | "input" | "working";

/** Archived comes last: it is put-away history, below everything the user may still act on. */
export const VIEW_GROUPS: readonly ViewGroup[] = [
  "input",
  "working",
  "completed",
  "archived",
];

export const VIEW_GROUP_LABEL: Record<ViewGroup, string> = {
  archived: "Archived",
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

/**
 * `session.list` row. The k3code gateway adds `cwd` (null when unknown), `updated_at` (last activity, epoch s) and
 * `archived_at` (epoch s, null when not archived); the generated Hermes contract lacks them, and an older core omits the
 * last two.
 */
export type PastSessionRow = SessionListRow & {
  archived_at?: null | number;
  cwd?: null | string;
  updated_at?: number;
};

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
  archived = [],
  currentCwd,
  currentSid,
  nowMs,
  past,
  sessions,
  subagents,
}: {
  /** Archived stored sessions (`session.list` with `archived: true`); they fill the Archived group. */
  archived?: readonly PastSessionRow[];
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
  // Archived ones go through the same filters; only their group differs (archived is not a state, so not `groupOf`).
  const pastRows = (
    list: readonly PastSessionRow[],
    group: ViewGroup,
  ): ViewRow[] =>
    (resumableHistory(list, sessions) as PastSessionRow[])
      // An empty stored session (opened, never used) is noise, not history.
      .filter((h) => h.message_count !== 0)
      .filter((h) => !!h.cwd && h.cwd === currentCwd)
      .map((h, i) => ({ h, i }))
      // Newest by last use, the order the rows' ages read in (a session resumed today is not filed under its creation).
      .sort(
        (a, b) =>
          (b.h.updated_at ?? b.h.started_at ?? 0) -
            (a.h.updated_at ?? a.h.started_at ?? 0) || a.i - b.i,
      )
      .map(({ h }) => ({
        activity: h.preview?.trim() ?? "",
        current: false,
        elapsedSeconds: null,
        group,
        id: h.id,
        key: `past:${h.id}`,
        kind: "past",
        // When it was last used, not when it was created: a session resumed today is not "3d ago".
        lastActive: h.updated_at ?? h.started_at ?? null,
        state: "done",
        title: h.title?.trim() || h.preview?.trim() || h.id.slice(0, 8),
      }));

  // Earlier sessions close the Completed group, after the live rows; the archived ones follow in their own group.
  return [
    ...VIEW_GROUPS.flatMap((g) => liveRows.filter((r) => r.group === g)),
    ...pastRows(past, "completed"),
    ...pastRows(archived, "archived"),
  ];
}

export type ViewFocus = "input" | "list";

export interface ViewNav {
  /** What the composer body holds; edited by the TextInput, never by the reducer. */
  draft: string;
  confirmKey: null | string;
  index: number;
  /** Collapsed group names (groups whose rows are hidden). */
  collapsedGroups: ViewGroup[];
  /** Which half owns the keyboard: the row list, or the bottom composer. */
  focus: ViewFocus;
  /** A collapsed group's header holds the selection (its rows are hidden); null on a row. */
  header: null | ViewGroup;
}

// Archived starts collapsed on every open: its header says how many there are, its rows stay out of the way.
export const IDLE_VIEW_NAV: ViewNav = {
  collapsedGroups: ["archived"],
  confirmKey: null,
  draft: "",
  focus: "list",
  header: null,
  index: 0,
};

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
  tab?: boolean;
  up?: boolean;
};

export type ViewEffect =
  | { row: ViewRow; type: "activate" | "archive" | "stop" | "unarchive" }
  // The archive proposal banner: archive every listed session, or keep them all and stop asking.
  | { type: "archive_suggested" | "close" | "decline_suggested" };

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

/** Build the visible line list (headers + rows) respecting collapsed groups. */
export function buildLines(
  rows: readonly ViewRow[],
  collapsedGroups: readonly ViewGroup[] = [],
): (
  | { group: ViewGroup; type: "header" }
  | { index: number; row: ViewRow; type: "row" }
)[] {
  const lines: (
    | { group: ViewGroup; type: "header" }
    | { index: number; row: ViewRow; type: "row" }
  )[] = [];

  for (const group of VIEW_GROUPS) {
    const inGroup = rows.filter((r) => r.group === group);
    const isCollapsed = collapsedGroups.includes(group);

    if (inGroup.length) {
      lines.push({ group, type: "header" });

      if (!isCollapsed) {
        for (const row of inGroup) {
          lines.push({ index: rows.indexOf(row), row, type: "row" });
        }
      }
    }
  }

  return lines;
}

/** What ↑↓ step through: the rows of open groups and the header of each collapsed group. */
export type NavItem =
  { group: ViewGroup; type: "header" } | { index: number; type: "row" };

export function navItems(
  rows: readonly ViewRow[],
  collapsedGroups: readonly ViewGroup[],
): NavItem[] {
  // In row order (buildViewRows already sorts rows by group): a collapsed group's header takes its first row's place.
  return rows.flatMap((r, index): NavItem[] =>
    !collapsedGroups.includes(r.group)
      ? [{ index, type: "row" }]
      : rows.findIndex((o) => o.group === r.group) === index
        ? [{ group: r.group, type: "header" }]
        : [],
  );
}

/** The header holding the selection: only a collapsed group that still has rows. */
export const selectedHeader = (
  nav: ViewNav,
  rows: readonly ViewRow[],
): null | ViewGroup =>
  nav.header &&
  nav.collapsedGroups.includes(nav.header) &&
  rows.some((r) => r.group === nav.header)
    ? nav.header
    : null;

/**
 * Collapse or expand `group` (c, ⏎ on a header, a click on it). Collapsing the group the selection is in moves the
 * selection to its header, so it never sits on a hidden row; expanding from the header lands on the group's first row.
 */
export function toggleGroup(
  nav: ViewNav,
  rows: readonly ViewRow[],
  group: ViewGroup,
): ViewNav {
  const first = rows.findIndex((r) => r.group === group);

  if (nav.collapsedGroups.includes(group)) {
    const onIt = selectedHeader(nav, rows) === group;

    return {
      ...nav,
      collapsedGroups: nav.collapsedGroups.filter((g) => g !== group),
      ...(onIt ? { header: null, index: Math.max(0, first) } : {}),
    };
  }

  const holdsSelection =
    !selectedHeader(nav, rows) && rows[nav.index]?.group === group;

  return {
    ...nav,
    collapsedGroups: [...nav.collapsedGroups, group],
    ...(holdsSelection ? { header: group } : {}),
  };
}

/** Live sessions other than this one and in-turn agents can be stopped; past rows and the current session cannot. */
export const isStoppable = (row: ViewRow) =>
  row.kind === "agent" || (row.kind === "session" && !row.current);

/** An earlier session can be archived; one in the Archived group can be restored. Live rows can be neither. */
export const isArchivable = (row: ViewRow) =>
  row.kind === "past" && row.group !== "archived";

/** Letters the list acts on while it has the keyboard: a archive, c collapse, n new (compose), u restore, x stop. */
const LIST_HOTKEYS = ["a", "c", "n", "u", "x"];

/**
 * Pure key reducer for the full-screen view; it owns the screen, so every key is consumed. `suggesting`: the archive
 * proposal banner is showing, so Alt+Y archives its sessions and Alt+N keeps them.
 */
export function reduceViewKey(
  nav: ViewNav,
  rows: readonly ViewRow[],
  key: ViewKey,
  pageSize: number,
  suggesting = false,
): { consumed: true; effect: null | ViewEffect; nav: ViewNav } {
  const last = rows.length - 1;
  const index = Math.max(0, Math.min(nav.index, last));
  // With Ctrl/Meta held the letter is a chord (Ctrl+N, Ctrl+X), not a view command.
  const ch = key.ctrl || key.meta ? "" : (key.ch ?? "").toLowerCase();
  const done = (effect: null | ViewEffect, next: Partial<ViewNav> = {}) => ({
    consumed: true as const,
    effect,
    nav: { ...nav, confirmKey: null, index, ...next },
  });
  // The banner's chords are the main view's proposal-card ones. Being chords, they work whichever half has the
  // keyboard: the view opens with the composer focused, where a bare letter would be typed.
  const chord = suggesting && key.meta && !key.ctrl ? key.ch : undefined;
  const suggestion: null | ViewEffect =
    chord === "y"
      ? { type: "archive_suggested" }
      : chord === "n"
        ? { type: "decline_suggested" }
        : null;

  // The composer owns editing keys through its own useInput; only the chords it
  // hands up reach the reducer here. Esc clears the draft first, so leaving the
  // view takes a second Esc — text is never lost to a stray Esc. ↑/↓ on an empty
  // composer hand the keyboard to the list, the selection where it was. (⏎ on an
  // empty composer opens the selected row: the pane does that from the submit,
  // which sees the real text; the draft here can lag the TextInput by a frame.)
  if (nav.focus === "input") {
    if (key.tab) {
      return done(null, { focus: "list" });
    }
    if (key.escape) {
      return nav.draft ? done(null, { draft: "" }) : done({ type: "close" });
    }
    if ((key.up || key.down) && !nav.draft && rows.length) {
      return done(null, { focus: "list" });
    }
    return done(suggestion);
  }

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

  if (suggestion) {
    return done(suggestion);
  }

  if (key.escape || key.left) {
    return done({ type: "close" });
  }

  // n and tab both hand the keyboard to the composer; the draft survives the switch.
  if (ch === "n" || key.tab) {
    return done(null, { focus: "input" });
  }

  // Any other typed text is meant for the composer: it takes the keyboard and the text, so no letter is lost.
  if (
    ch &&
    !LIST_HOTKEYS.includes(ch) &&
    !key.return &&
    stripControl(key.ch ?? "") === key.ch
  ) {
    return done(null, { draft: nav.draft + (key.ch ?? ""), focus: "input" });
  }

  if (!rows.length) {
    return done(null);
  }

  const step = Math.max(1, pageSize);
  // Navs built before groups could collapse (older callers, tests) may carry no list.
  const collapsed: readonly ViewGroup[] = nav.collapsedGroups ?? [];
  const items = navItems(rows, collapsed);
  // A selected row its group's collapse has hidden stands on that group's header.
  const header =
    selectedHeader(nav, rows) ??
    (collapsed.includes(rows[index]!.group) ? rows[index]!.group : null);
  const at = Math.max(
    0,
    items.findIndex((it) =>
      it.type === "header"
        ? it.group === header
        : !header && it.index === index,
    ),
  );
  const move = (to: number, effect: null | ViewEffect = null) => {
    const it = items[Math.max(0, Math.min(items.length - 1, to))]!;

    return it.type === "row"
      ? done(effect, { header: null, index: it.index })
      : done(effect, {
          header: it.group,
          index: rows.findIndex((r) => r.group === it.group),
        });
  };

  if (key.up) {
    return move(at - 1);
  }

  if (key.down) {
    return move(at + 1);
  }

  if (key.home) {
    return move(0);
  }

  if (key.end) {
    return move(items.length - 1);
  }

  if (key.pageUp) {
    return move(at - step);
  }

  if (key.pageDown) {
    return move(at + step);
  }

  // On a collapsed header ⏎/→ and c expand it; nothing else applies to a header.
  if (header) {
    return key.return || key.right || ch === "c"
      ? done(null, toggleGroup({ ...nav, index }, rows, header))
      : done(null);
  }

  const row = rows[index]!;

  if (key.return || key.right) {
    return done(row.current ? { type: "close" } : { row, type: "activate" });
  }

  if (ch === "x" && isStoppable(row)) {
    return done(null, { confirmKey: row.key });
  }

  // a and u move the row to the other group once the list is refetched. The cursor steps off it now, so it does not
  // follow the row into a collapsed group where nothing shows it: onto the next row of the same group, else the item
  // above. Reversible, so no confirmation.
  if (
    (ch === "a" && isArchivable(row)) ||
    (ch === "u" && row.group === "archived")
  ) {
    const effect: ViewEffect = {
      row,
      type: ch === "a" ? "archive" : "unarchive",
    };
    const next = items[at + 1];
    const to =
      next?.type === "row" && rows[next.index]!.group === row.group
        ? at + 1
        : at > 0
          ? at - 1
          : at + 1;

    return items[to] ? move(to, effect) : done(effect);
  }

  // c collapses the selected row's group; the selection moves to its header.
  if (ch === "c") {
    return done(null, toggleGroup({ ...nav, index }, rows, row.group));
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
