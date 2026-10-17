/**
 * The /settings panel (Claude Code's /config, k3code style): a Config tab of
 * live, arrow-key-editable settings and a Status tab with the read-only view.
 *
 * Pure state + key handling; the Ink component only paints what this returns.
 * Every change is applied by dispatching the matching slash command (`/theme
 * dark`, `/permissions mode plan`, ...), so the panel reuses each command's
 * live-apply and persistence instead of re-implementing them.
 */
import type { Key } from "@k3code/ink";

export type SettingKind = "bool" | "enum";

export interface SettingItem {
  /** Slash command that applies `value`. */
  command: (value: string) => string;
  help: string;
  id: string;
  kind: SettingKind;
  label: string;
  /** For bool: ["off", "on"]. */
  options: string[];
  section: string;
  value: string;
}

/** Everything the panel shows, gathered from settings.get + the TUI's own state. */
export interface SettingsSnapshot {
  /** "Start k3code in agent view" (display.tui_start_agents_view). */
  agentsView: boolean;
  busy: string;
  compact: boolean;
  details: string;
  effort: string;
  indicator: string;
  indicators: string[];
  model: string;
  modelKeys: string[];
  mouse: boolean;
  outputStyle: string;
  outputStyles: string[];
  permissionMode: string;
  permissionModes: string[];
  showReasoning: boolean;
  statusBar: string;
  /** The Status tab: the plain /settings text, one entry per line. */
  statusLines: string[];
  theme: string;
}

export const EFFORTS = ["default", "low", "medium", "high", "xhigh", "max"];
const ONOFF = ["off", "on"];

const uniq = (xs: string[], cur: string) =>
  xs.includes(cur) ? xs : [...xs, cur].filter(Boolean);

const onoff = (b: boolean) => (b ? "on" : "off");

export function buildSettingsItems(s: SettingsSnapshot): SettingItem[] {
  const item = (
    section: string,
    id: string,
    label: string,
    value: string,
    options: string[],
    command: (v: string) => string,
    help: string,
    kind: SettingKind = "enum",
  ): SettingItem => ({
    command,
    help,
    id,
    kind,
    label,
    options: uniq(options, value),
    section,
    value,
  });

  return [
    item(
      "Model",
      "model",
      "Model",
      s.model,
      s.modelKeys,
      (v) => `/model ${v}`,
      "Model for this session",
    ),
    item(
      "Model",
      "effort",
      "Reasoning effort",
      s.effort || "default",
      EFFORTS,
      (v) => `/reasoning ${v}`,
      "How hard the model thinks",
    ),
    item(
      "Model",
      "permissions",
      "Permission mode",
      s.permissionMode,
      s.permissionModes,
      (v) => `/permissions mode ${v}`,
      "What runs without asking",
    ),
    item(
      "Model",
      "style",
      "Output style",
      s.outputStyle,
      s.outputStyles,
      (v) => `/output-style ${v}`,
      "How answers are written",
    ),
    item(
      "Display",
      "theme",
      "Theme",
      s.theme,
      ["auto", "light", "dark"],
      (v) => `/theme ${v}`,
      "Colour theme",
    ),
    item(
      "Display",
      "reasoning",
      "Show thinking",
      onoff(s.showReasoning),
      ONOFF,
      (v) => `/reasoning ${v === "on" ? "show" : "hide"}`,
      "Show the model's reasoning in the transcript",
      "bool",
    ),
    item(
      "Display",
      "details",
      "Tool details",
      s.details,
      ["hidden", "collapsed", "expanded"],
      (v) => `/details ${v}`,
      "How tool calls appear in the transcript",
    ),
    item(
      "Display",
      "compact",
      "Compact layout",
      onoff(s.compact),
      ONOFF,
      (v) => `/density ${v}`,
      "Tighter transcript spacing",
      "bool",
    ),
    item(
      "Display",
      "agents_view",
      "Start in agent view",
      onoff(s.agentsView),
      ONOFF,
      (v) => `/agents-view ${v === "on" ? "on" : "off"}`,
      "Open the full-screen agent view on startup, before a session exists",
      "bool",
    ),
    item(
      "Display",
      "statusbar",
      "Status bar",
      s.statusBar,
      ["top", "bottom", "off"],
      (v) => `/statusbar ${v}`,
      "Where the status bar sits",
    ),
    item(
      "Display",
      "indicator",
      "Busy indicator",
      s.indicator,
      s.indicators,
      (v) => `/indicator ${v}`,
      "Spinner style while working",
    ),
    item(
      "Input",
      "busy",
      "Enter while busy",
      s.busy,
      ["queue", "steer", "interrupt"],
      (v) => `/busy ${v}`,
      "What Enter does while a turn runs",
    ),
    item(
      "Input",
      "mouse",
      "Mouse",
      onoff(s.mouse),
      ONOFF,
      (v) => `/mouse ${v}`,
      "Mouse wheel + click tracking",
      "bool",
    ),
  ];
}

export type SettingsTab = "config" | "status";

export interface SettingsState {
  cursor: number;
  items: SettingItem[];
  query: string;
  searching: boolean;
  statusLines: string[];
  statusScroll: number;
  tab: SettingsTab;
}

export function initSettingsState(snap: SettingsSnapshot): SettingsState {
  return {
    cursor: 0,
    items: buildSettingsItems(snap),
    query: "",
    searching: false,
    statusLines: snap.statusLines,
    statusScroll: 0,
    tab: "config",
  };
}

/** The items the search box leaves visible (label, section, id or value). */
export function visibleItems(state: SettingsState): SettingItem[] {
  const q = state.query.trim().toLowerCase();

  if (!q) {
    return state.items;
  }

  return state.items.filter((it) =>
    [it.label, it.section, it.id, it.value].some((f) =>
      f.toLowerCase().includes(q),
    ),
  );
}

export type SettingsKey = Partial<
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

export type SettingsAction =
  | { type: "none" }
  | { type: "close" }
  | { command: string; id: string; type: "apply"; value: string };

export interface SettingsStep {
  action: SettingsAction;
  state: SettingsState;
}

const PAGE = 8;

function cycle(it: SettingItem, dir: 1 | -1): string {
  const n = it.options.length;
  const i = Math.max(0, it.options.indexOf(it.value));

  return it.options[(i + dir + n) % n] ?? it.value;
}

function setValue(
  state: SettingsState,
  it: SettingItem,
  value: string,
): SettingsStep {
  if (value === it.value) {
    return { action: { type: "none" }, state };
  }

  const items = state.items.map((x) => (x.id === it.id ? { ...x, value } : x));

  return {
    action: { command: it.command(value), id: it.id, type: "apply", value },
    state: { ...state, items },
  };
}

/**
 * One keypress. Config tab: ↑/↓ (j/k) move, PgUp/PgDn/Home/End jump, Enter or
 * Space flips a toggle / steps an option forward, ←/→ (h/l) step an option
 * back/forward, `/` searches, digits 1-9 jump. Tab switches to Status (↑/↓
 * scroll there). Esc clears the search first, then closes; q closes too.
 * Locked (an approval / question prompt is open) every key is ignored.
 */
export function settingsKey(
  state: SettingsState,
  ch: string,
  key: SettingsKey,
  opts: { locked?: boolean } = {},
): SettingsStep {
  const same: SettingsStep = { action: { type: "none" }, state };
  const next = (patch: Partial<SettingsState>): SettingsStep => ({
    action: { type: "none" },
    state: { ...state, ...patch },
  });

  // Ink flags every Esc as meta (a bare ESC byte parses as meta+escape): let it through, or the panel cannot be closed.
  if (opts.locked || key.ctrl || (key.meta && !key.escape)) {
    return same;
  }

  if (state.searching) {
    if (key.escape) {
      return next({ cursor: 0, query: "", searching: false });
    }

    if (key.return) {
      return next({ searching: false });
    }

    if (key.downArrow || key.upArrow || key.tab) {
      return settingsKey({ ...state, searching: false }, ch, key, opts);
    }

    if (key.backspace || key.delete) {
      return next({ cursor: 0, query: state.query.slice(0, -1) });
    }

    if (ch && ch >= " ") {
      return next({ cursor: 0, query: state.query + ch });
    }

    return same;
  }

  if (key.escape) {
    return state.query
      ? next({ cursor: 0, query: "" })
      : { action: { type: "close" }, state };
  }

  if (ch === "q") {
    return { action: { type: "close" }, state };
  }

  if (key.tab) {
    return next({ tab: state.tab === "config" ? "status" : "config" });
  }

  if (state.tab === "status") {
    const max = Math.max(0, state.statusLines.length - 1);
    const at = (n: number) =>
      next({ statusScroll: Math.min(max, Math.max(0, n)) });

    if (key.upArrow || ch === "k") return at(state.statusScroll - 1);
    if (key.downArrow || ch === "j") return at(state.statusScroll + 1);
    if (key.pageUp) return at(state.statusScroll - PAGE);
    if (key.pageDown) return at(state.statusScroll + PAGE);
    if (key.home || ch === "g") return at(0);
    if (key.end || ch === "G") return at(max);

    return same;
  }

  const items = visibleItems(state);
  const last = items.length - 1;

  if (last < 0) {
    return ch === "/" ? next({ searching: true }) : same;
  }

  const cur = Math.min(state.cursor, last);
  const it = items[cur]!;
  const move = (n: number) => next({ cursor: n });

  if (key.upArrow || ch === "k") return move(cur <= 0 ? last : cur - 1);
  if (key.downArrow || ch === "j") return move(cur >= last ? 0 : cur + 1);
  if (key.pageUp) return move(Math.max(0, cur - PAGE));
  if (key.pageDown) return move(Math.min(last, cur + PAGE));
  if (key.home || ch === "g") return move(0);
  if (key.end || ch === "G") return move(last);

  if (ch === "/") {
    return next({ searching: true });
  }

  if (/^[1-9]$/.test(ch)) {
    const n = Number(ch) - 1;

    return n <= last ? move(n) : same;
  }

  if (key.return || ch === " ") {
    return setValue(state, it, cycle(it, 1));
  }

  if (key.rightArrow || ch === "l") {
    return setValue(state, it, it.kind === "bool" ? "on" : cycle(it, 1));
  }

  if (key.leftArrow || ch === "h") {
    return setValue(state, it, it.kind === "bool" ? "off" : cycle(it, -1));
  }

  return same;
}

/** First row index to paint so the cursor stays inside `height` rows. */
export function settingsWindow(
  cursor: number,
  total: number,
  height: number,
): number {
  if (total <= height) {
    return 0;
  }

  return Math.min(Math.max(0, cursor - Math.floor(height / 2)), total - height);
}

/** Normalise the `settings.get` payload + local UI values into a snapshot. */
export function settingsSnapshot(
  raw: unknown,
  local: Pick<
    SettingsSnapshot,
    | "agentsView"
    | "busy"
    | "compact"
    | "details"
    | "indicator"
    | "indicators"
    | "mouse"
    | "showReasoning"
    | "statusBar"
  > & {
    effort?: string;
    /** The saved `display.tui_theme` pin (config.get "theme"); empty = auto. */
    theme?: string;
  },
): SettingsSnapshot {
  const r = (raw ?? {}) as Record<string, unknown>;
  const v = (r.settings ?? {}) as Record<string, unknown>;
  const str = (x: unknown, d = "") => (typeof x === "string" ? x : d);
  const strs = (x: unknown) =>
    Array.isArray(x) ? x.filter((s): s is string => typeof s === "string") : [];

  return {
    ...local,
    effort: str(v.effort, local.effort ?? "default"),
    model: str(v.model_key),
    modelKeys: strs(v.model_keys),
    outputStyle: str(v.output_style, "default"),
    outputStyles: strs(v.output_styles),
    permissionMode: str(v.permission_mode, "default"),
    permissionModes: strs(v.permission_modes),
    statusLines: str(r.output).split("\n"),
    // settings.get's `theme` is the gateway skin name; the row edits the
    // `display.tui_theme` mode pin that /theme writes, so read that instead.
    theme:
      local.theme === "light" || local.theme === "dark" ? local.theme : "auto",
  };
}
