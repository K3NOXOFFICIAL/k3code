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
 * applied by Enter only. Enter sends one `config.set` per changed key, or the
 * field's own slash command.
 *
 * Two tabs read `settings.get`, which answers later: the Session rows (model,
 * effort, permissions, style, theme, thinking) and the Status text appear once
 * `applySnapshot` has run. `/` searches the rows of every tab at once.
 */

export type DashboardTabId =
  "context" | "details" | "input" | "look" | "session" | "status";

export const DASHBOARD_TABS: readonly { id: DashboardTabId; label: string }[] =
  [
    { id: "look", label: "Look" },
    { id: "details", label: "Details" },
    { id: "input", label: "Input" },
    { id: "context", label: "Context" },
    { id: "session", label: "Session" },
    { id: "status", label: "Status" },
  ];

/** A section row that follows the global details mode (or the built-in default) rather than pinning one. */
export const SECTION_DEFAULT = "default";

export interface DashboardField {
  /**
   * A slash command that applies the value in place of `config.set`: the
   * command checks and saves it itself (`/autocompact 70%`).
   */
  command?: (value: string) => string;
  /** The heading the old /settings panel filed the row under, or the nearest one for a row it lacked (none for Autocompact): a search for it lists the row. */
  group?: "Display" | "Input" | "Model";
  /** One line under the list while the cursor is on the row. */
  hint: string;
  id: string;
  /** The `config.set` key a change is sent under (the name in a summary when `command` applies it). */
  key: string;
  label: string;
  /**
   * Choices in the order ←/→ walk them; a value the ui already has that is not one of them is added at the end.
   * Empty for the lists only the backend knows (models, permission modes, output styles): `applySnapshot` fills them.
   */
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

const EFFORT_OPTIONS = ["default", "low", "medium", "high", "xhigh", "max"];

/** A command argument: a name with a space (`my style`) is quoted, which the gateway reads back as one word. */
const quoted = (value: string) =>
  /\s/.test(value) ? JSON.stringify(value) : value;

export const DASHBOARD_FIELDS: readonly DashboardField[] = [
  {
    group: "Display",
    hint: "Where the status rule (model, context, cost) sits.",
    id: "statusbar",
    key: "statusbar",
    label: "Status bar",
    options: ["top", "bottom", "off"],
    preview: true,
    tab: "look",
  },
  {
    group: "Display",
    hint: "The spinner style while the model works. ascii is safe everywhere.",
    id: "indicator",
    key: "indicator",
    label: "Indicator",
    options: ["ascii", "emoji", "kaomoji", "unicode"],
    preview: true,
    tab: "look",
  },
  {
    group: "Display",
    hint: "Tighter spacing between transcript blocks.",
    id: "density",
    key: "density",
    label: "Compact layout",
    options: ["off", "on"],
    preview: true,
    tab: "look",
  },
  {
    group: "Display",
    hint: "A battery gauge in the status bar (laptops).",
    id: "battery",
    key: "battery",
    label: "Battery",
    options: ["off", "on"],
    preview: true,
    tab: "look",
  },
  {
    group: "Display",
    hint: "How much agent detail to show. Picking one sets all four sections below.",
    id: "details",
    key: "details_mode",
    label: "All sections",
    options: MODE_OPTIONS,
    preview: true,
    tab: "details",
  },
  ...SECTION_NAMES.map((name): DashboardField => ({
    group: "Display",
    hint: `${SECTION_LABELS[name]}: pin a mode, or default to follow the global one.`,
    id: `section.${name}`,
    key: `details_mode.${name}`,
    label: SECTION_LABELS[name],
    options: [SECTION_DEFAULT, ...MODE_OPTIONS],
    preview: true,
    tab: "details",
  })),
  {
    group: "Input",
    hint: "all: wheel scrolls, a click moves the cursor. Select text with Shift-drag (Option-drag on macOS). wheel: scroll only, the terminal keeps selection.",
    id: "mouse",
    key: "mouse",
    label: "Mouse",
    options: ["all", "buttons", "wheel", "off"],
    preview: true,
    tab: "input",
  },
  {
    group: "Input",
    hint: "A message typed while the model works: queue it, steer the running turn, or interrupt.",
    id: "busy",
    key: "busy",
    label: "While busy",
    options: ["queue", "steer", "interrupt"],
    preview: true,
    tab: "input",
  },
  {
    group: "Input",
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
  {
    command: (value) => `/model ${value}`,
    group: "Model",
    hint: "The model this session uses. Enter switches to it (/model).",
    id: "model",
    key: "model",
    label: "Model",
    options: [],
    preview: false,
    tab: "session",
  },
  {
    command: (value) => `/reasoning ${value}`,
    group: "Model",
    hint: "How hard the model thinks; default leaves it to the model. Enter applies it (/reasoning).",
    id: "effort",
    key: "effort",
    label: "Reasoning effort",
    options: EFFORT_OPTIONS,
    preview: false,
    tab: "session",
  },
  {
    command: (value) => `/permissions mode ${value}`,
    group: "Model",
    hint: "What runs without asking first. Enter applies it (/permissions mode).",
    id: "permissions",
    key: "permissions",
    label: "Permission mode",
    options: [],
    preview: false,
    tab: "session",
  },
  {
    command: (value) => `/output-style ${quoted(value)}`,
    group: "Model",
    hint: "How answers are written. Enter applies it (/output-style).",
    id: "style",
    key: "style",
    label: "Output style",
    options: [],
    preview: false,
    tab: "session",
  },
  {
    command: (value) => `/theme ${value}`,
    group: "Display",
    hint: "Colour theme: auto follows the terminal, or pin light or dark. Enter applies it (/theme).",
    id: "theme",
    key: "theme",
    label: "Theme",
    options: ["auto", "light", "dark"],
    preview: false,
    tab: "session",
  },
  {
    command: (value) => `/reasoning ${value === "on" ? "show" : "hide"}`,
    group: "Display",
    hint: "Show the model's reasoning in the transcript. Enter applies it (/reasoning show|hide).",
    id: "reasoning",
    key: "reasoning",
    label: "Show thinking",
    options: ["off", "on"],
    preview: false,
    tab: "session",
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
  | "showReasoning"
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
  showReasoning: ui.showReasoning,
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
    reasoning: onOff(ui.showReasoning),
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

  // `autocompact` is not painted: its command saves it, and the config sync reads the saved value back.
  // Nor is the Session tab (`showReasoning` included): each of its commands applies its own value.

  return patch;
}

// ── State ────────────────────────────────────────────────────────────

export interface DashboardState {
  /** Row within the list the screen shows: the tab's fields, or the rows the search matches. */
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
  /** The search text. A non-empty one lists the matching rows of every tab instead of the tab's own. */
  query: string;
  /** The row the cursor was on when the box opened on an empty text: Esc in the box puts it back. Null on a kept text. */
  searchFrom: null | number;
  /** Typing into the search box: printable keys go to `query`. */
  searching: boolean;
  /** Whether `settings.get` has answered; the Session rows and the Status text wait for it. */
  session: "failed" | "loading" | "ready";
  /** Why it failed. */
  sessionError: string;
  /** The Status tab's text, one entry per line. */
  statusLines: string[];
  /** The first line of the Status text that is painted. */
  statusScroll: number;
}

/** The chips of a row: ``options``, with ``value`` added at the end when it is not among them. */
const chipsFor = (options: readonly string[], value: string | undefined) =>
  value && !options.includes(value) ? [...options, value] : options;

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
      DASHBOARD_FIELDS.map((f) => [f.id, chipsFor(f.options, values[f.id])]),
    ),
    origin,
    query: "",
    searchFrom: null,
    searching: false,
    session: "loading",
    sessionError: "",
    statusLines: [],
    statusScroll: 0,
    tab: Math.max(
      0,
      DASHBOARD_TABS.findIndex((t) => t.id === tab),
    ),
    values,
  };
}

// ── Settings snapshot ────────────────────────────────────────────────

/** What `settings.get` says, as the Session and Status tabs need it. */
export interface DashboardSnapshot {
  effort: string;
  model: string;
  modelKeys: string[];
  outputStyle: string;
  outputStyles: string[];
  permissionMode: string;
  permissionModes: string[];
  /** The plain `/settings` text, one entry per line. */
  statusLines: string[];
  /** `auto`, `light` or `dark`: the backend's `default` (or nothing) means auto. */
  theme: string;
}

const isRecord = (x: unknown): x is Record<string, unknown> =>
  !!x && typeof x === "object";

/** The `settings.get` result (its `settings` view and `output` text) as a snapshot; a missing or odd field gets a default. */
export function normalizeDashboardSnapshot(raw: unknown): DashboardSnapshot {
  const r = isRecord(raw) ? raw : {};
  const v = isRecord(r.settings) ? r.settings : {};
  const str = (x: unknown, fallback = "") =>
    typeof x === "string" && x ? x : fallback;
  const strs = (x: unknown) =>
    Array.isArray(x)
      ? [...new Set(x.filter((s): s is string => typeof s === "string" && !!s))]
      : [];
  const theme = str(v.theme);
  const output = str(r.output).trimEnd();

  return {
    effort: str(v.effort, "default"),
    model: str(v.model_key),
    modelKeys: strs(v.model_keys),
    outputStyle: str(v.output_style, "default"),
    outputStyles: strs(v.output_styles),
    permissionMode: str(v.permission_mode, "default"),
    permissionModes: strs(v.permission_modes),
    statusLines: output ? output.split(/\r?\n/) : [],
    theme: theme === "default" ? "auto" : theme || "auto",
  };
}

/**
 * The snapshot arrived: show the Session rows with its values and option lists
 * and the Status text. Only those move: a Look value the user already changed
 * while it was loading stays changed.
 */
export function applySnapshot(
  state: DashboardState,
  snap: DashboardSnapshot,
): DashboardState {
  const picked: DashboardValues = {
    effort: snap.effort,
    model: snap.model,
    permissions: snap.permissionMode,
    style: snap.outputStyle,
    theme: snap.theme,
  };
  const lists: Record<string, readonly string[]> = {
    model: snap.modelKeys,
    permissions: snap.permissionModes,
    style: snap.outputStyles,
  };
  const options = { ...state.options };

  for (const [id, value] of Object.entries(picked)) {
    options[id] = chipsFor(lists[id] ?? byId.get(id)!.options, value);
  }

  return inList({
    ...state,
    base: { ...state.base, ...picked },
    options,
    session: "ready",
    sessionError: "",
    statusLines: snap.statusLines,
    values: { ...state.values, ...picked },
  });
}

/** `settings.get` failed: the Session and Status tabs say why instead of showing rows. */
export const failSnapshot = (
  state: DashboardState,
  message: string,
): DashboardState =>
  inList({ ...state, session: "failed", sessionError: message });

// ── Rows ─────────────────────────────────────────────────────────────

const tabLabel = (tab: DashboardTabId) =>
  DASHBOARD_TABS.find((t) => t.id === tab)!.label;

const activeTab = (state: DashboardState) => DASHBOARD_TABS[state.tab]!.id;

/** The search text without the spaces around it, lower-cased: what a row has to contain. */
const needle = (state: DashboardState) => state.query.trim().toLowerCase();

/** The rows a tab shows: the Session rows wait for the snapshot, Status has none. */
const tabFields = (state: DashboardState, tab: DashboardTabId) =>
  tab === "session" && state.session !== "ready" ? [] : fieldsOf(tab);

/**
 * The rows the cursor walks: the active tab's, or with a search text the rows
 * of every tab whose label, tab name, heading, id or value contains it, in tab
 * order. The value is the one the popup found: a row whose value you change
 * stays in the list under the cursor.
 */
export function currentFields(state: DashboardState): DashboardField[] {
  const q = needle(state);

  if (!q) {
    return tabFields(state, activeTab(state));
  }

  return DASHBOARD_TABS.flatMap((t) => tabFields(state, t.id)).filter((f) =>
    [
      f.label,
      tabLabel(f.tab),
      f.group ?? "",
      f.id,
      state.base[f.id] ?? "",
    ].some((s) => s.toLowerCase().includes(q)),
  );
}

/** `state` with the cursor held inside the rows it walks, after rows came or went. */
function inList(state: DashboardState): DashboardState {
  const cursor = Math.max(
    0,
    Math.min(state.cursor, currentFields(state).length - 1),
  );

  return cursor === state.cursor ? state : { ...state, cursor };
}

/** `/dashboard [look|details|input|context|session|status]`, `/display ...`: a tab by name or number, else null. */
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

  // a Session row hidden again (a second settings.get failed) keeps its draft, but nobody sees it: it is not sent
  for (const f of DASHBOARD_FIELDS) {
    if (f.id.startsWith("section.") || !tabFields(state, f.tab).includes(f)) {
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
    | "backspace"
    | "ctrl"
    | "delete"
    | "downArrow"
    | "end"
    | "escape"
    | "home"
    | "leftArrow"
    | "meta"
    | "pageDown"
    | "pageUp"
    | "return"
    | "rightArrow"
    | "shift"
    | "tab"
    | "upArrow"
  >
>;

export interface DashboardKeyOpts {
  /** Height of the Status text window (`layout.bodyRows`): the page for PgUp/PgDn and where scrolling stops. */
  bodyRows?: number;
  /** An approval / question / password / confirm prompt is open: every key and click is ignored. */
  locked?: boolean;
}

/** The window height when the caller does not say. */
const DEFAULT_BODY_ROWS = 8;

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

/**
 * The value after `delta` steps through the row's chips (wraps: a row is a
 * ring of chips). A value that is none of them (a model the backend did not
 * list) steps onto the first chip going forward and the last going back; a
 * row with no chips steps onto nothing, which `setField` refuses.
 */
function stepped(
  state: DashboardState,
  f: DashboardField,
  value: string,
  delta: number,
): string {
  const chips = state.options[f.id] ?? f.options;
  const i = chips.indexOf(value);

  return i < 0
    ? chips[delta < 0 ? chips.length - 1 : 0]!
    : chips[(i + delta + chips.length) % chips.length]!;
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
    !tabFields(state, f.tab).includes(f) ||
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

/** A tab opens at its first row with no search: a search text belongs to the tab it was typed on. */
const moveTab = (state: DashboardState, tab: number): DashboardStep => {
  const n = DASHBOARD_TABS.length;
  const to = ((tab % n) + n) % n;

  return to === state.tab && !state.query && !state.searching
    ? { action: { type: "none" }, state }
    : {
        action: { type: "none" },
        state: { ...state, cursor: 0, query: "", searching: false, tab: to },
      };
};

const moveCursor = (state: DashboardState, cursor: number): DashboardStep => {
  const to = Math.max(0, Math.min(currentFields(state).length - 1, cursor));

  return {
    action: { type: "none" },
    state: to === state.cursor ? state : { ...state, cursor: to },
  };
};

// ── Status text ──────────────────────────────────────────────────────

const onStatus = (state: DashboardState) => activeTab(state) === "status";

const bodyRowsOf = (opts: DashboardKeyOpts) =>
  Math.max(1, opts.bodyRows ?? DEFAULT_BODY_ROWS);

/** The first line painted for a window of `rows` lines: `statusScroll`, held inside the text. */
const statusTop = (state: DashboardState, rows: number) =>
  Math.max(0, Math.min(state.statusScroll, state.statusLines.length - rows));

/** Scroll the Status text so line `to` is the first one painted, as far as the text allows. */
function scrollStatus(
  state: DashboardState,
  to: number,
  rows: number,
): DashboardStep {
  const top = Math.max(0, Math.min(to, state.statusLines.length - rows));

  return {
    action: { type: "none" },
    state: top === state.statusScroll ? state : { ...state, statusScroll: top },
  };
}

/** One row up or down: the cursor walks the list, on the Status tab the text scrolls. */
const walk = (
  state: DashboardState,
  delta: number,
  opts: DashboardKeyOpts,
): DashboardStep => {
  const rows = bodyRowsOf(opts);

  return onStatus(state)
    ? scrollStatus(state, statusTop(state, rows) + delta, rows)
    : moveCursor(state, state.cursor + delta);
};

// ── Search ───────────────────────────────────────────────────────────

/** The tab the cursor row lives on. With a search text the list spans every tab, so that is not the tab the popup was opened on. */
const shownTab = (state: DashboardState) =>
  DASHBOARD_TABS.findIndex(
    (t) =>
      t.id === (currentFields(state)[state.cursor]?.tab ?? activeTab(state)),
  );

/** Leave the search: no text, no typing. The cursor stays on its row, in that row's own tab. */
function clearSearch(state: DashboardState): DashboardStep {
  const field = currentFields(state)[state.cursor];
  const tab = shownTab(state);

  return {
    action: { type: "none" },
    state: {
      ...state,
      cursor: field
        ? tabFields(state, DASHBOARD_TABS[tab]!.id).indexOf(field)
        : 0,
      query: "",
      searching: false,
      tab,
    },
  };
}

/** Esc in the box, on a text typed from empty: the search never was. The tab and the cursor stay where they were. */
const cancelSearch = (state: DashboardState): DashboardStep => ({
  action: { type: "none" },
  state: {
    ...state,
    cursor: state.searchFrom ?? 0,
    query: "",
    searching: false,
  },
});

/**
 * A key while the search box has the keyboard: printable characters (digits,
 * h/l/j/k and space too) go into the text. Enter keeps the text and hands the
 * keys back; ↑/↓ and Tab hand them back and act; Esc drops the search.
 */
function searchKey(
  state: DashboardState,
  ch: string,
  key: DashboardKey,
  opts: DashboardKeyOpts,
): DashboardStep {
  const typed = (query: string): DashboardStep => ({
    action: { type: "none" },
    state: { ...state, cursor: 0, query },
  });

  if (key.escape) {
    return state.searchFrom === null ? clearSearch(state) : cancelSearch(state);
  }

  // Alt/Option and Ctrl chords are not the plain keys (Ink marks a plain Esc as meta too, hence after it)
  if (key.ctrl || key.meta) {
    return { action: { type: "none" }, state };
  }

  // a blank text is no search: Enter and ↑/↓/Tab drop it alike
  const kept = { ...state, query: needle(state) ? state.query : "" };

  if (key.return) {
    return { action: { type: "none" }, state: { ...kept, searching: false } };
  }

  if (key.upArrow || key.downArrow || key.tab) {
    return dashboardKey({ ...kept, searching: false }, ch, key, opts);
  }

  if (key.backspace || key.delete) {
    // by character, not by UTF-16 unit: one Backspace takes an emoji whole
    return typed(Array.from(state.query).slice(0, -1).join(""));
  }

  const text = ch.replace(/\p{Cc}/gu, "");

  return text ? typed(state.query + text) : { action: { type: "none" }, state };
}

/**
 * One keypress. ←/→ (h/l, space) change the row, ↑/↓ (k/j) move between rows
 * (and scroll the Status text), PgUp/PgDn, Home/End and g/G jump, Tab /
 * Shift+Tab and 1-6 change tab, `/` searches every tab's rows, Enter saves,
 * Esc clears the search and then puts everything back, `q` puts everything
 * back. An Alt/Option or Ctrl chord is none of these keys and does nothing.
 * Locked (an approval / question / password / confirm prompt is open) every
 * key is ignored, so the prompt owns the keyboard.
 */
export function dashboardKey(
  state: DashboardState,
  ch: string,
  key: DashboardKey,
  opts: DashboardKeyOpts = {},
): DashboardStep {
  const same: DashboardStep = { action: { type: "none" }, state };

  if (opts.locked) {
    return same;
  }

  if (state.searching) {
    return searchKey(state, ch, key, opts);
  }

  if (key.escape) {
    return state.query ? clearSearch(state) : cancelStep(state);
  }

  // Alt/Option and Ctrl chords are not the plain keys: Option+←/→ (a word jump) must not change a row, Alt+Enter not save.
  // Ink marks a plain Esc as meta too, hence after it.
  if (key.ctrl || key.meta) {
    return same;
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
    return moveTab(state, shownTab(state) + (key.shift ? -1 : 1));
  }

  if (key.upArrow) {
    return walk(state, -1, opts);
  }

  if (key.downArrow) {
    return walk(state, 1, opts);
  }

  // PgUp/PgDn, Home/End and g/G jump: down the Status text, and over the rows of any other tab (a search lists a dozen)
  const rows = bodyRowsOf(opts);
  const status = onStatus(state);
  const at = status ? statusTop(state, rows) : state.cursor;
  const end = status ? state.statusLines.length : currentFields(state).length;
  const goTo = (to: number) =>
    status ? scrollStatus(state, to, rows) : moveCursor(state, to);

  if (key.pageUp) {
    return goTo(at - rows);
  }

  if (key.pageDown) {
    return goTo(at + rows);
  }

  if (key.home) {
    return goTo(0);
  }

  if (key.end) {
    return goTo(end);
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

  switch (ch) {
    case "h":
      return change(-1);
    case "l":
    case " ":
      return change(1);
    case "k":
      return walk(state, -1, opts);
    case "j":
      return walk(state, 1, opts);
    case "g":
      return goTo(0);
    case "G":
      return goTo(end);
    case "/":
      return status
        ? same
        : {
            action: { type: "none" },
            state: {
              ...state,
              searchFrom: state.query ? null : state.cursor,
              searching: true,
            },
          };
    case "q":
      return cancelStep(state);
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
  /**
   * Rows left for the list or the Status text once the fixed lines are drawn,
   * so the popup stays inside the terminal. A search line takes one of them.
   */
  bodyRows: number;
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

/** The lines around the body: tabs, hint, keys; the roomy layout adds the title and two blanks. */
const COMPACT_FIXED = 1 + HINT_LINES + 1;
const ROOMY_FIXED = 1 + COMPACT_FIXED + 2;

/**
 * The tallest tab (title, tabs, 2 blanks, rows, hint, keys) must fit under the
 * chrome, else drop the roomy lines. What the fixed lines leave is `bodyRows`:
 * the Status text and the search results show that many lines.
 */
export function dashboardLayout(o: {
  cols: number;
  maxWidth?: number;
  rows: number;
}): DashboardLayout {
  const tallest = Math.max(...DASHBOARD_TABS.map((t) => fieldsOf(t.id).length));
  const room = o.rows - DASHBOARD_OVERLAY_CHROME;
  const compact = room < ROOMY_FIXED + tallest;

  return {
    bodyRows: Math.max(1, room - (compact ? COMPACT_FIXED : ROOMY_FIXED)),
    compact,
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
  kind:
    | "blank"
    | "hint"
    | "keys"
    | "note"
    | "row"
    | "search"
    | "tabs"
    | "text"
    | "title";
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

/** Columns a cut end of a row of chips takes: `… ` before the first painted chip, ` …` after the last. */
const MORE = 2;

/**
 * The chips [from, to) a row (or the tab bar) paints in `room` columns: from
 * the first, or from the nearest later one that keeps the picked chip
 * (`picked`, -1 for none) in view when the list is wider than the popup.
 */
function chipSpan(
  texts: readonly string[],
  picked: number,
  room: number,
): [from: number, to: number] {
  const width = (from: number, to: number) =>
    texts.slice(from, to).reduce((n, t) => n + t.length, 0) +
    (from > 0 ? MORE : 0) +
    (to < texts.length ? MORE : 0);
  let from = 0;

  while (from < picked && width(from, picked + 1) > room) {
    from++;
  }

  let to = Math.max(from, picked + 1);

  while (to < texts.length && width(from, to + 1) <= room) {
    to++;
  }

  return [from, to];
}

function tabsLine(
  state: DashboardState,
  layout: DashboardLayout,
): DashboardLine {
  const segs: DashboardSeg[] = [seg(" ", "muted")];
  const targets: DashboardTarget[] = [];
  // with a search text the list spans every tab: none is the one shown
  const on = (i: number) => i === state.tab && !needle(state);
  const texts = DASHBOARD_TABS.map((t, i) =>
    on(i) ? `[${t.label}]` : ` ${t.label} `,
  );
  const [from, to] = chipSpan(texts, state.tab, layout.width - 1);
  let col = 1;

  if (from > 0) {
    segs.push(seg("… ", "muted"));
    col += MORE;
  }

  for (let i = from; i < to; i++) {
    const text = texts[i]!;

    targets.push({ end: col + text.length, index: i, start: col, type: "tab" });
    segs.push(
      seg(text, on(i) ? "accent" : "muted", on(i) ? { bold: true } : {}),
    );
    col += text.length;
  }

  if (to < texts.length) {
    segs.push(seg(" …", "muted"));
  }

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
  const value = state.values[f.id] ?? "";
  const segs: DashboardSeg[] = [];
  const targets: DashboardTarget[] = [];
  const head = `${active ? "❯ " : "  "}${f.label.padEnd(labelW)}  `;
  const chips = state.options[f.id] ?? f.options;
  const [from, to] = chipSpan(
    chips.map((option) => (option === value ? `[${option}]` : ` ${option} `)),
    chips.indexOf(value),
    layout.width - head.length,
  );

  segs.push(seg(head, "plain"));

  let col = head.length;

  if (from > 0) {
    segs.push(seg("… ", "muted"));
    col += MORE;
  }

  for (const option of chips.slice(from, to)) {
    const picked = option === value;
    const text = picked ? `[${option}]` : ` ${option} `;

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

  if (to < chips.length) {
    segs.push(seg(" …", "muted"));
    col += MORE;
  }

  // a row in a search result says which tab it lives on
  const notes = [
    defaultNote(state, f),
    needle(state) ? ` · ${tabLabel(f.tab)}` : "",
  ];

  for (const note of notes) {
    if (note && col + note.length <= layout.width) {
      segs.push(seg(note, "muted"));
      col += note.length;
    }
  }

  return { active, field: index, kind: "row", segs, targets };
}

/** First row of a window of `height` rows that keeps `cursor` in view, in the middle when it can be. */
const windowStart = (cursor: number, total: number, height: number) =>
  total <= height
    ? 0
    : Math.min(Math.max(0, cursor - Math.floor(height / 2)), total - height);

const KEYS_SEARCH =
  "←/→ change · ↑/↓ row · Tab tab · / search · Enter save · Esc revert";
const KEYS = "←/→ change · ↑/↓ row · Tab tab · Enter save · Esc revert";
const KEYS_SHORT = "←/→ ↑/↓ Tab · Enter save · Esc revert";
const KEYS_FILTER = "←/→ change · ↑/↓ row · / edit · Enter save · Esc clear";
const KEYS_FILTER_SHORT = "←/→ ↑/↓ · Enter save · Esc clear";
const KEYS_TYPING = "type to filter · Enter done · Esc clear";
const KEYS_STATUS = "↑/↓ PgUp/PgDn scroll · Tab next · Enter save · Esc revert";
const KEYS_STATUS_SHORT = "↑/↓ scroll · Tab · Enter save · Esc";
const LOCKED = "Answer the question above first.";

/** The first line that fits `width`, else the last (the shortest). */
const fitting = (width: number, ...lines: string[]) =>
  lines.find((l) => l.length <= width) ?? lines.at(-1)!;

function keysLine(state: DashboardState, width: number): string {
  if (state.searching) {
    return KEYS_TYPING;
  }

  if (onStatus(state)) {
    return fitting(width, KEYS_STATUS, KEYS_STATUS_SHORT);
  }

  return state.query
    ? fitting(width, KEYS_FILTER, KEYS_FILTER_SHORT)
    : fitting(width, KEYS_SEARCH, KEYS, KEYS_SHORT);
}

/** The search line: the text, with a caret while it is being typed; the tail of a text too long for the popup. */
function searchLine(state: DashboardState, width: number): DashboardLine {
  const chars = Array.from(
    `search: ${state.query}${state.searching ? "▌" : ""}`,
  );

  return plain(
    "search",
    seg(
      chars.length > width
        ? `…${chars.slice(1 - width).join("")}`
        : chars.join(""),
      state.searching ? "accent" : "muted",
    ),
  );
}

/** Why a tab has no rows to show: the snapshot is on its way, or failed. */
function waiting(state: DashboardState, loading: string): string {
  return state.session === "failed"
    ? `settings unavailable${state.sessionError ? `: ${state.sessionError}` : ""}`
    : loading;
}

/** The popup as lines, top to bottom. */
export function dashboardScreen(
  state: DashboardState,
  layout: DashboardLayout,
  opts: { locked?: boolean } = {},
): DashboardLine[] {
  const fields = currentFields(state);
  const status = onStatus(state);
  const searching = state.searching || !!state.query;
  const labelW = Math.max(0, ...fields.map((f) => f.label.length));
  const lines: DashboardLine[] = [];
  const spacer = () => !layout.compact && lines.push(blank());
  const note = (text: string) =>
    lines.push(plain("note", seg(cut(text, layout.width), "muted")));
  let hint = fields[state.cursor]?.hint ?? "";

  if (!layout.compact) {
    lines.push(plain("title", seg("Settings", "label", { bold: true })));
  }

  lines.push(tabsLine(state, layout));

  if (searching) {
    lines.push(searchLine(state, layout.width));
  }

  spacer();

  if (status) {
    const rows = layout.bodyRows;
    const top = statusTop(state, rows);
    // the text of an earlier answer is not painted under the reason a later one failed
    const shown =
      state.session === "ready" ? state.statusLines.slice(top, top + rows) : [];

    if (state.session !== "ready") {
      note(waiting(state, "loading…"));
    } else if (!shown.length) {
      note("no status text");
    }

    for (const l of shown) {
      lines.push(plain("text", seg(cut(l, layout.width) || " ", "plain")));
    }

    hint =
      shown.length && state.statusLines.length > rows
        ? `lines ${top + 1}-${top + shown.length} of ${state.statusLines.length}`
        : "";
  } else if (fields.length) {
    const height = Math.max(1, layout.bodyRows - (searching ? 1 : 0));
    const start = windowStart(state.cursor, fields.length, height);

    fields.slice(start, start + height).forEach((f, i) => {
      lines.push(rowLine(state, layout, f, start + i, labelW));
    });
  } else {
    note(
      needle(state)
        ? "no setting matches"
        : waiting(state, "loading settings…"),
    );
  }

  spacer();

  const wrapped = wrap(hint, layout.width - 1, HINT_LINES);

  for (let i = 0; i < HINT_LINES; i++) {
    lines.push(plain("hint", seg(wrapped[i] ?? " ", "muted")));
  }

  lines.push(
    plain(
      "keys",
      seg(opts.locked ? LOCKED : keysLine(state, layout.width), "muted"),
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

/** Wheel over the popup walks the rows, as ↑/↓ do, and scrolls the Status text. */
export const dashboardWheel = (
  state: DashboardState,
  dir: -1 | 1,
  opts: DashboardKeyOpts = {},
): DashboardStep =>
  opts.locked ? { action: { type: "none" }, state } : walk(state, dir, opts);

/**
 * The summary the transcript gets after Enter: the `config.set` changes. A
 * change with a `command` is left out, since the command says what it did, or
 * why it could not, when it answers; "" when nothing is left to say.
 */
export function dashboardSummary(changes: DashboardChange[]): string {
  const saved = changes.filter((c) => !c.command);

  if (!saved.length) {
    return "";
  }

  const shown = saved
    .filter((c) => !c.key.startsWith("details_mode."))
    .map((c) => `${c.key}=${c.value}`);
  const sections = saved.filter((c) => c.key.startsWith("details_mode."));

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
