import type { Key, MouseTrackingMode } from "@k3code/ink";

import type {
  BusyInputMode,
  IndicatorStyle,
  StatusBarMode,
  UiState,
} from "../app/interfaces.js";
import { clampOverlayWidth } from "../components/overlayPrimitives.js";
import type { DetailsMode, SectionName } from "../types.js";

import { SECTION_NAMES, sectionMode } from "./details.js";

/**
 * The /dashboard popup (also `/display`): one tabbed screen for the settings a
 * user otherwise reaches through a dozen slash commands. Pure functions, like
 * `tune.ts`: ui slice -> state, key / click -> next state + action, state ->
 * lines. `components/dashboard.tsx` only paints the lines and feeds keys and
 * clicks in, so what a key does, what is sent and what fits 80x24 are
 * unit-tested without a terminal.
 *
 * A field is a row of choices ("chips"); ←/→ or a click on a chip picks one,
 * ↑/↓ or a click on a row moves the cursor. A field that is `preview` is
 * painted live while the popup is open and put back by Esc; the others are
 * applied by Enter only. Enter sends one `config.set` per changed key.
 */

export type DashboardTabId = "context" | "details" | "input" | "look";

export const DASHBOARD_TABS: readonly { id: DashboardTabId; label: string }[] =
  [
    { id: "look", label: "Look" },
    { id: "details", label: "Details" },
    { id: "input", label: "Input" },
    { id: "context", label: "Context" },
  ];

/** A section row that follows the global details mode (or the built-in default) rather than pinning one. */
export const SECTION_DEFAULT = "default";

export interface DashboardField {
  /**
   * A slash command that applies the value in place of `config.set`: the
   * command checks and saves it itself (`/autocompact 70%`).
   */
  command?: (value: string) => string;
  /** One line under the list while the cursor is on the row. */
  hint: string;
  id: string;
  /** The `config.set` key a change is sent under (the name in a summary when `command` applies it). */
  key: string;
  label: string;
  /** Choices in the order ←/→ walk them; a value the ui already has that is not one of them is added at the end. */
  options: readonly string[];
  /** Painted while the popup is open; false = applied when Enter saves. */
  preview: boolean;
  tab: DashboardTabId;
}

const SECTION_LABELS: Record<SectionName, string> = {
  activity: "Activity",
  subagents: "Sub-agents",
  thinking: "Thinking",
  tools: "Tool calls",
};

const MODE_OPTIONS = ["hidden", "collapsed", "expanded"] as const;

export const DASHBOARD_FIELDS: readonly DashboardField[] = [
  {
    hint: "Where the status rule (model, context, cost) sits.",
    id: "statusbar",
    key: "statusbar",
    label: "Status bar",
    options: ["top", "bottom", "off"],
    preview: true,
    tab: "look",
  },
  {
    hint: "The spinner style while the model works. ascii is safe everywhere.",
    id: "indicator",
    key: "indicator",
    label: "Indicator",
    options: ["ascii", "emoji", "kaomoji", "unicode"],
    preview: true,
    tab: "look",
  },
  {
    hint: "Tighter spacing between transcript blocks.",
    id: "density",
    key: "density",
    label: "Compact layout",
    options: ["off", "on"],
    preview: true,
    tab: "look",
  },
  {
    hint: "A battery gauge in the status bar (laptops).",
    id: "battery",
    key: "battery",
    label: "Battery",
    options: ["off", "on"],
    preview: true,
    tab: "look",
  },
  {
    hint: "How much agent detail to show. Picking one sets all four sections below.",
    id: "details",
    key: "details_mode",
    label: "All sections",
    options: MODE_OPTIONS,
    preview: true,
    tab: "details",
  },
  ...SECTION_NAMES.map((name): DashboardField => ({
    hint: `${SECTION_LABELS[name]}: pin a mode, or default to follow the global one.`,
    id: `section.${name}`,
    key: `details_mode.${name}`,
    label: SECTION_LABELS[name],
    options: [SECTION_DEFAULT, ...MODE_OPTIONS],
    preview: true,
    tab: "details",
  })),
  {
    hint: "all: wheel scrolls, a click moves the cursor. Select text with Shift-drag (Option-drag on macOS). wheel: scroll only, the terminal keeps selection.",
    id: "mouse",
    key: "mouse",
    label: "Mouse",
    options: ["all", "buttons", "wheel", "off"],
    preview: true,
    tab: "input",
  },
  {
    hint: "A message typed while the model works: queue it, steer the running turn, or interrupt.",
    id: "busy",
    key: "busy",
    label: "While busy",
    options: ["queue", "steer", "interrupt"],
    preview: true,
    tab: "input",
  },
  {
    hint: "Show only your prompts, questions, errors and final answers (Ctrl+F). Applied on Enter.",
    id: "focus",
    key: "focus",
    label: "Focus view",
    options: ["off", "on"],
    preview: false,
    tab: "input",
  },
  {
    command: (value) => `/autocompact ${value}`,
    hint: "When the conversation is folded into a summary; auto is 70% of the window. Enter saves it for all sessions (/autocompact 120k sets a limit).",
    id: "autocompact",
    key: "autocompact",
    label: "Autocompact",
    options: ["auto", "50%", "60%", "70%", "80%", "90%", "off"],
    preview: false,
    tab: "context",
  },
];

const byId = new Map(DASHBOARD_FIELDS.map((f) => [f.id, f] as const));

export const fieldsOf = (tab: DashboardTabId): DashboardField[] =>
  DASHBOARD_FIELDS.filter((f) => f.tab === tab);

// ── Ui slice <-> values ──────────────────────────────────────────────

/** The part of the ui state the popup reads, previews and puts back. */
export type DashboardUi = Pick<
  UiState,
  | "autocompact"
  | "battery"
  | "busyInputMode"
  | "compact"
  | "detailsMode"
  | "detailsModeCommandOverride"
  | "focusView"
  | "indicatorStyle"
  | "mouseTracking"
  | "sections"
  | "statusBar"
>;

/** field id -> chosen option, always one of the field's options. */
export type DashboardValues = Record<string, string>;

export const dashboardUi = (ui: DashboardUi): DashboardUi => ({
  autocompact: ui.autocompact,
  battery: ui.battery,
  busyInputMode: ui.busyInputMode,
  compact: ui.compact,
  detailsMode: ui.detailsMode,
  detailsModeCommandOverride: ui.detailsModeCommandOverride,
  focusView: ui.focusView,
  indicatorStyle: ui.indicatorStyle,
  mouseTracking: ui.mouseTracking,
  sections: { ...ui.sections },
  statusBar: ui.statusBar,
});

const onOff = (v: boolean) => (v ? "on" : "off");

/** What each row shows for this ui. A section without an override is `default`. */
export function dashboardValues(ui: DashboardUi): DashboardValues {
  const values: DashboardValues = {
    autocompact: ui.autocompact,
    battery: onOff(ui.battery),
    busy: ui.busyInputMode,
    density: onOff(ui.compact),
    details: ui.detailsMode,
    focus: onOff(ui.focusView),
    indicator: ui.indicatorStyle,
    mouse: ui.mouseTracking,
    statusbar: ui.statusBar,
  };

  for (const name of SECTION_NAMES) {
    values[`section.${name}`] = ui.sections[name] ?? SECTION_DEFAULT;
  }

  return values;
}

/**
 * The ui the draft `values` describe, built on `origin` so a field the popup
 * does not own (and the `/details` command-override flag, unless the global
 * mode moved) is kept. `onlyPreview` leaves out the fields that wait for Enter.
 */
export function dashboardPatch(
  values: DashboardValues,
  origin: DashboardUi,
  onlyPreview = true,
): Partial<DashboardUi> {
  const include = (id: string) =>
    !onlyPreview || byId.get(id)?.preview !== false;
  const patch: Partial<DashboardUi> = {};
  const details = values.details as DetailsMode;
  const sections: Partial<Record<SectionName, DetailsMode>> = {};

  for (const name of SECTION_NAMES) {
    const v = values[`section.${name}`];

    if (v && v !== SECTION_DEFAULT) {
      sections[name] = v as DetailsMode;
    }
  }

  if (include("statusbar")) {
    patch.statusBar = values.statusbar as StatusBarMode;
  }

  if (include("indicator")) {
    patch.indicatorStyle = values.indicator as IndicatorStyle;
  }

  if (include("density")) {
    patch.compact = values.density === "on";
  }

  if (include("battery")) {
    patch.battery = values.battery === "on";
  }

  if (include("details")) {
    patch.detailsMode = details;
    patch.sections = sections;
    patch.detailsModeCommandOverride =
      origin.detailsModeCommandOverride || details !== origin.detailsMode;
  }

  if (include("mouse")) {
    patch.mouseTracking = values.mouse as MouseTrackingMode;
  }

  if (include("busy")) {
    patch.busyInputMode = values.busy as BusyInputMode;
  }

  if (include("focus")) {
    patch.focusView = values.focus === "on";
  }

  // `autocompact` is not painted: its command saves it, and the config sync reads the saved value back

  return patch;
}

// ── State ────────────────────────────────────────────────────────────

export interface DashboardState {
  /** Row within the tab's fields. */
  cursor: number;
  /** The ui as the popup found it: what Esc puts back. */
  origin: DashboardUi;
  /** Index into DASHBOARD_TABS. */
  tab: number;
  /** The draft. */
  values: DashboardValues;
  /** The values as the popup found them: what "changed" is measured against. */
  base: DashboardValues;
  /** The chips of each row: the field's options, plus the value the ui had if it is none of them (a `120k` limit). */
  options: Record<string, readonly string[]>;
}

/** The chips of a row: ``f.options``, with ``value`` added at the end when it is not among them. */
const chipsFor = (f: DashboardField, value: string | undefined) =>
  value && !f.options.includes(value) ? [...f.options, value] : f.options;

export function initDashboardState(
  ui: DashboardUi,
  tab: DashboardTabId = "look",
): DashboardState {
  const origin = dashboardUi(ui);
  const values = dashboardValues(origin);

  return {
    base: { ...values },
    cursor: 0,
    options: Object.fromEntries(
      DASHBOARD_FIELDS.map((f) => [f.id, chipsFor(f, values[f.id])]),
    ),
    origin,
    tab: Math.max(
      0,
      DASHBOARD_TABS.findIndex((t) => t.id === tab),
    ),
    values,
  };
}

export const currentFields = (state: DashboardState): DashboardField[] =>
  fieldsOf(DASHBOARD_TABS[state.tab]!.id);

/** `/dashboard [look|details|input|context]`, `/display ...`: a tab by name or number, else null. */
export function parseDashboardTab(arg: string): DashboardTabId | null {
  const word = arg.trim().toLowerCase();
  const byNumber = DASHBOARD_TABS[Number(word) - 1];

  return (
    DASHBOARD_TABS.find((t) => t.id === word || t.label.toLowerCase() === word)
      ?.id ??
    byNumber?.id ??
    null
  );
}

// ── Changes ──────────────────────────────────────────────────────────

export interface DashboardChange {
  /** The slash command that applies it, for a field that has one (it saves the value itself); else `config.set`. */
  command?: string;
  key: string;
  /** The `config.set` value: "" resets a section override. */
  value: string;
}

/**
 * ``context`` of ``config.get full`` as a chip: ``off``, a token limit (``120k``), a share of the window (``65%``) or
 * ``auto``, the same words ``/autocompact`` takes. A hand-edited value that is none of them counts as ``auto``.
 */
export function autocompactLabel(context: unknown): string {
  const c = (context && typeof context === "object" ? context : {}) as {
    autocompact?: unknown;
    compact_at_ratio?: unknown;
    compact_at_tokens?: unknown;
  };

  if (c.autocompact === false) {
    return "off";
  }

  const tokens = Number(c.compact_at_tokens);

  if (Number.isFinite(tokens) && tokens > 0) {
    return tokens % 1_000_000 === 0
      ? `${tokens / 1_000_000}m`
      : tokens % 1000 === 0
        ? `${tokens / 1000}k`
        : String(Math.round(tokens));
  }

  const ratio = Number(c.compact_at_ratio);

  return Number.isFinite(ratio) && ratio > 0 && ratio <= 1
    ? `${Math.round(ratio * 100)}%`
    : "auto";
}

/**
 * The `config.set` calls Enter sends: only fields that moved off what the
 * popup found, in a stable order. Moving the global details mode moves the
 * four section rows with it, so all four are sent and a restart shows the same
 * panels as now instead of the built-in section defaults.
 */
export function dashboardChanges(state: DashboardState): DashboardChange[] {
  const moved = (id: string) => state.values[id] !== state.base[id];
  const out: DashboardChange[] = [];

  for (const f of DASHBOARD_FIELDS) {
    if (f.id.startsWith("section.")) {
      continue;
    }

    if (moved(f.id)) {
      const value = state.values[f.id]!;

      out.push({
        ...(f.command ? { command: f.command(value) } : {}),
        key: f.key,
        value,
      });
    }
  }

  for (const f of DASHBOARD_FIELDS) {
    if (!f.id.startsWith("section.")) {
      continue;
    }

    if (moved(f.id)) {
      const v = state.values[f.id]!;

      out.push({ key: f.key, value: v === SECTION_DEFAULT ? "" : v });
    }
  }

  return out;
}

// ── Keys ─────────────────────────────────────────────────────────────

export type DashboardKey = Partial<
  Pick<
    Key,
    | "ctrl"
    | "downArrow"
    | "escape"
    | "leftArrow"
    | "meta"
    | "return"
    | "rightArrow"
    | "shift"
    | "tab"
    | "upArrow"
  >
>;

export type DashboardAction =
  | { type: "none" }
  /** Paint this slice of the ui now (a field moved). */
  | { patch: Partial<DashboardUi>; type: "preview" }
  /** Close; put the ui back to `revert`. */
  | { revert: DashboardUi; type: "cancel" }
  /** Close and send `changes`; `patch` is every field of the draft, including the ones that waited for Enter. */
  | {
      changes: DashboardChange[];
      patch: Partial<DashboardUi>;
      type: "save";
    };

export interface DashboardStep {
  action: DashboardAction;
  state: DashboardState;
}

const cancelStep = (state: DashboardState): DashboardStep => ({
  action: { revert: state.origin, type: "cancel" },
  state,
});

/** The value after `delta` steps through the row's chips (wraps: a row is a ring of chips). */
function stepped(
  state: DashboardState,
  f: DashboardField,
  value: string,
  delta: number,
): string {
  const chips = state.options[f.id] ?? f.options;
  const i = Math.max(0, chips.indexOf(value));

  return chips[(i + delta + chips.length) % chips.length]!;
}

/** Pick `value` on a field: the row's own value, and with the global details mode the four sections too. */
export function setField(
  state: DashboardState,
  id: string,
  value: string,
): DashboardStep {
  const f = byId.get(id);

  if (
    !f ||
    !(state.options[id] ?? f.options).includes(value) ||
    state.values[id] === value
  ) {
    return { action: { type: "none" }, state };
  }

  const values = { ...state.values, [id]: value };

  if (id === "details") {
    for (const name of SECTION_NAMES) {
      values[`section.${name}`] = value;
    }
  }

  const next = { ...state, values };
  const patch = dashboardPatch(values, state.origin);

  return {
    action: f.preview ? { patch, type: "preview" } : { type: "none" },
    state: next,
  };
}

const moveTab = (state: DashboardState, tab: number): DashboardStep => {
  const n = DASHBOARD_TABS.length;
  const to = ((tab % n) + n) % n;

  return to === state.tab
    ? { action: { type: "none" }, state }
    : { action: { type: "none" }, state: { ...state, cursor: 0, tab: to } };
};

const moveCursor = (state: DashboardState, cursor: number): DashboardStep => {
  const to = Math.max(0, Math.min(currentFields(state).length - 1, cursor));

  return {
    action: { type: "none" },
    state: to === state.cursor ? state : { ...state, cursor: to },
  };
};

/**
 * One keypress. ←/→ (h/l, space) change the row, ↑/↓ (k/j) move between rows,
 * Tab / Shift+Tab and 1-3 change tab, Enter saves, Esc puts everything back.
 * Locked (an approval / question / password / confirm prompt is open) every
 * key is ignored, so the prompt owns the keyboard.
 */
export function dashboardKey(
  state: DashboardState,
  ch: string,
  key: DashboardKey,
  opts: { locked?: boolean } = {},
): DashboardStep {
  const same: DashboardStep = { action: { type: "none" }, state };

  if (opts.locked) {
    return same;
  }

  if (key.escape) {
    return cancelStep(state);
  }

  if (key.return) {
    const changes = dashboardChanges(state);

    return changes.length
      ? {
          action: {
            changes,
            patch: dashboardPatch(state.values, state.origin, false),
            type: "save",
          },
          state,
        }
      : cancelStep(state);
  }

  if (key.tab) {
    return moveTab(state, state.tab + (key.shift ? -1 : 1));
  }

  if (key.upArrow) {
    return moveCursor(state, state.cursor - 1);
  }

  if (key.downArrow) {
    return moveCursor(state, state.cursor + 1);
  }

  const field = currentFields(state)[state.cursor];
  const change = (delta: number) =>
    field
      ? setField(
          state,
          field.id,
          stepped(state, field, state.values[field.id]!, delta),
        )
      : same;

  if (key.leftArrow) {
    return change(-1);
  }

  if (key.rightArrow) {
    return change(1);
  }

  if (key.ctrl || key.meta) {
    return same;
  }

  switch (ch) {
    case "h":
      return change(-1);
    case "l":
    case " ":
      return change(1);
    case "k":
      return moveCursor(state, state.cursor - 1);
    case "j":
      return moveCursor(state, state.cursor + 1);
    default:
  }

  if (/^[1-9]$/.test(ch) && Number(ch) <= DASHBOARD_TABS.length) {
    return moveTab(state, Number(ch) - 1);
  }

  return same;
}

// ── Layout ───────────────────────────────────────────────────────────

const MIN_WIDTH = 44;
const MAX_WIDTH = 78;
/** Columns around the popup's content: its border and padding. */
const SIDE_CHROME = 4;
/** Rows around the popup: its double border and top margin (3) plus the status rule and spacer under it (3). */
export const DASHBOARD_OVERLAY_CHROME = 6;

export interface DashboardLayout {
  /** A short terminal: no title and no blank lines. */
  compact: boolean;
  width: number;
}

/** Popup width: wide enough for the longest row, capped like ModelPicker's grid cell. */
export const dashboardWidth = (cols: number, maxWidth?: number) =>
  clampOverlayWidth(
    Math.min(
      Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, cols - 6)),
      Math.max(1, cols - SIDE_CHROME),
    ),
    maxWidth,
  );

/** Rows the hint under the list may take. */
const HINT_LINES = 2;

/** The tallest tab (title, tabs, 2 blanks, rows, hint, keys) must fit under the chrome, else drop the roomy lines. */
export function dashboardLayout(o: {
  cols: number;
  maxWidth?: number;
  rows: number;
}): DashboardLayout {
  const tallest = Math.max(...DASHBOARD_TABS.map((t) => fieldsOf(t.id).length));
  const roomy = 1 + 1 + 1 + tallest + 1 + HINT_LINES + 1;

  return {
    compact: o.rows - DASHBOARD_OVERLAY_CHROME < roomy,
    width: dashboardWidth(o.cols, o.maxWidth),
  };
}

// ── Screen ───────────────────────────────────────────────────────────

export type DashboardTone = "accent" | "label" | "muted" | "ok" | "plain";

export interface DashboardSeg {
  bold?: boolean;
  /** The chip painted as picked. */
  picked?: boolean;
  text: string;
  tone: DashboardTone;
}

/** A clickable span of a line, columns [start, end). */
export type DashboardTarget =
  | { end: number; index: number; start: number; type: "tab" }
  | { end: number; field: number; option: string; start: number; type: "chip" };

export interface DashboardLine {
  /** The cursor row: painted as the shared selection chip. */
  active?: boolean;
  /** The field this line is (index in the tab), for a click on the row outside its chips. */
  field?: number;
  kind: "blank" | "hint" | "keys" | "row" | "tabs" | "title";
  segs: DashboardSeg[];
  targets: DashboardTarget[];
}

export const lineText = (line: DashboardLine) =>
  line.segs.map((s) => s.text).join("");

const seg = (
  text: string,
  tone: DashboardTone,
  extra: Partial<DashboardSeg> = {},
): DashboardSeg => ({ ...extra, text, tone });

const plain = (
  kind: DashboardLine["kind"],
  ...segs: DashboardSeg[]
): DashboardLine => ({ kind, segs, targets: [] });

const blank = (): DashboardLine => plain("blank", seg(" ", "muted"));

const cut = (text: string, max: number) =>
  text.length > max ? `${text.slice(0, Math.max(0, max - 1))}…` : text;

/** `text` broken at spaces into at most `max` lines of `width` columns; the last one is cut with an ellipsis. */
export function wrap(text: string, width: number, max: number): string[] {
  const lines: string[] = [];
  let line = "";

  for (const word of text.split(/\s+/).filter(Boolean)) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }

  if (line) {
    lines.push(line);
  }

  if (lines.length > max) {
    return [
      ...lines.slice(0, max - 1),
      cut(lines.slice(max - 1).join(" "), width),
    ];
  }

  return lines.map((l) => cut(l, width));
}

function tabsLine(state: DashboardState): DashboardLine {
  const segs: DashboardSeg[] = [];
  const targets: DashboardTarget[] = [];
  let col = 1;

  segs.push(seg(" ", "muted"));

  DASHBOARD_TABS.forEach((t, i) => {
    const on = i === state.tab;
    const text = on ? `[${t.label}]` : ` ${t.label} `;

    targets.push({ end: col + text.length, index: i, start: col, type: "tab" });
    segs.push(seg(text, on ? "accent" : "muted", on ? { bold: true } : {}));
    col += text.length;
  });

  return { kind: "tabs", segs, targets };
}

/** What a section row's `default` currently resolves to, so the chip is not a mystery. */
function defaultNote(state: DashboardState, f: DashboardField): string {
  if (!f.id.startsWith("section.")) {
    return "";
  }

  const name = f.id.slice("section.".length) as SectionName;

  if (state.values[f.id] !== SECTION_DEFAULT) {
    return "";
  }

  const override =
    state.values.details !== state.origin.detailsMode ||
    state.origin.detailsModeCommandOverride;

  return ` → ${sectionMode(name, state.values.details as DetailsMode, {}, override)}`;
}

function rowLine(
  state: DashboardState,
  layout: DashboardLayout,
  f: DashboardField,
  index: number,
  labelW: number,
): DashboardLine {
  const active = index === state.cursor;
  const value = state.values[f.id]!;
  const segs: DashboardSeg[] = [];
  const targets: DashboardTarget[] = [];
  const head = `${active ? "❯ " : "  "}${f.label.padEnd(labelW)}  `;

  segs.push(seg(head, "plain"));

  let col = head.length;

  for (const option of state.options[f.id] ?? f.options) {
    const picked = option === value;
    const text = picked ? `[${option}]` : ` ${option} `;

    if (col + text.length > layout.width) {
      break;
    }

    targets.push({
      end: col + text.length,
      field: index,
      option,
      start: col,
      type: "chip",
    });
    segs.push(seg(text, picked ? "ok" : "muted", picked ? { picked } : {}));
    col += text.length;
  }

  const note = defaultNote(state, f);

  if (note && col + note.length <= layout.width) {
    segs.push(seg(note, "muted"));
  }

  return { active, field: index, kind: "row", segs, targets };
}

const KEYS = "←/→ change · ↑/↓ row · Tab tab · Enter save · Esc revert";
const KEYS_SHORT = "←/→ ↑/↓ Tab · Enter save · Esc revert";
const LOCKED = "Answer the question above first.";

/** The popup as lines, top to bottom. */
export function dashboardScreen(
  state: DashboardState,
  layout: DashboardLayout,
  opts: { locked?: boolean } = {},
): DashboardLine[] {
  const fields = currentFields(state);
  const labelW = Math.max(...fields.map((f) => f.label.length));
  const lines: DashboardLine[] = [];
  const spacer = () => !layout.compact && lines.push(blank());

  if (!layout.compact) {
    lines.push(plain("title", seg("Display & input", "label", { bold: true })));
  }

  lines.push(tabsLine(state));
  spacer();

  fields.forEach((f, i) => {
    lines.push(rowLine(state, layout, f, i, labelW));
  });

  spacer();

  const hint = wrap(
    fields[state.cursor]?.hint ?? "",
    layout.width - 1,
    HINT_LINES,
  );

  for (let i = 0; i < HINT_LINES; i++) {
    lines.push(plain("hint", seg(hint[i] ?? " ", "muted")));
  }

  lines.push(
    plain(
      "keys",
      seg(
        opts.locked ? LOCKED : layout.width >= KEYS.length ? KEYS : KEYS_SHORT,
        "muted",
      ),
    ),
  );

  return lines;
}

// ── Clicks ───────────────────────────────────────────────────────────

export type DashboardHit =
  | { field: number; option: string; type: "chip" }
  | { field: number; type: "row" }
  | { index: number; type: "tab" }
  | { type: "none" };

/** What is under the cell (row, col) of the painted popup, counted from its top-left corner. */
export function dashboardHit(
  lines: DashboardLine[],
  row: number,
  col: number,
): DashboardHit {
  const line = lines[row];

  if (!line) {
    return { type: "none" };
  }

  for (const t of line.targets) {
    if (col >= t.start && col < t.end) {
      return t.type === "tab"
        ? { index: t.index, type: "tab" }
        : { field: t.field, option: t.option, type: "chip" };
    }
  }

  return line.field === undefined
    ? { type: "none" }
    : { field: line.field, type: "row" };
}

/**
 * A click. A tab moves to it and a row moves the cursor to it; both only look
 * around. A chip is the control: it moves the cursor to its row AND picks the
 * option, the same as ←/→ would. Locked, a click does nothing.
 */
export function dashboardClick(
  state: DashboardState,
  hit: DashboardHit,
  opts: { locked?: boolean } = {},
): DashboardStep {
  const same: DashboardStep = { action: { type: "none" }, state };

  if (opts.locked) {
    return same;
  }

  if (hit.type === "tab") {
    return moveTab(state, hit.index);
  }

  if (hit.type === "row") {
    return moveCursor(state, hit.field);
  }

  if (hit.type === "chip") {
    const field = currentFields(state)[hit.field];

    if (!field) {
      return same;
    }

    const moved = moveCursor(state, hit.field);
    const step = setField(moved.state, field.id, hit.option);

    return step.state === moved.state && step.action.type === "none"
      ? moved
      : step;
  }

  return same;
}

/**
 * A click event as the popup's root Box hands it over: `localRow` / `localCol`
 * count from the Box's own top-left corner, which is the first painted line.
 */
export const dashboardClickAt = (
  state: DashboardState,
  layout: DashboardLayout,
  at: { localCol: number; localRow: number },
  opts: { locked?: boolean } = {},
): DashboardStep =>
  dashboardClick(
    state,
    dashboardHit(
      dashboardScreen(state, layout, opts),
      at.localRow,
      at.localCol,
    ),
    opts,
  );

/** Wheel over the popup walks the rows, as ↑/↓ do. */
export const dashboardWheel = (
  state: DashboardState,
  dir: -1 | 1,
  opts: { locked?: boolean } = {},
): DashboardStep =>
  opts.locked
    ? { action: { type: "none" }, state }
    : moveCursor(state, state.cursor + dir);

/** The summary the transcript gets after Enter. */
export function dashboardSummary(changes: DashboardChange[]): string {
  if (!changes.length) {
    return "display: no changes";
  }

  const shown = changes
    .filter((c) => !c.key.startsWith("details_mode."))
    .map((c) => `${c.key}=${c.value}`);
  const sections = changes.filter((c) => c.key.startsWith("details_mode."));

  if (sections.length) {
    shown.push(
      sections
        .map(
          (c) =>
            `${c.key.slice("details_mode.".length)}=${c.value || "default"}`,
        )
        .join(" "),
    );
  }

  return `display: ${shown.join("  ")}`;
}
