import type { Key } from "@k3code/ink";
import { WAKE_WORDS } from "@k3code/shared/wake-words";

import { windowOffset } from "../components/overlayControls.js";
import { clampOverlayWidth } from "../components/overlayPrimitives.js";
import type {
  TuneGetResponse,
  TuneSetParams,
  TuneSetResponse,
} from "../gatewayTypes.js";

/**
 * The /tune popup (model list + effort slider + ultracode, one screen) as pure
 * functions: snapshot -> state, key -> next state + action, state -> lines.
 * `components/tunePicker.tsx` only paints the lines and feeds keys in, so the
 * rules below (what a key does, what is sent, what fits 80x24) are unit-tested
 * without a terminal.
 */

/** The leftmost effort stop: send no effort override. */
export const DEFAULT_STOP = "default";
export const DEFAULT_ROW_LABEL = "Default (recommended)";
export const ULTRA_ON = "ultracode";
export const ULTRA_OFF = "off";

const FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export const TUNE_SUBTITLE =
  "Switch between models. Enter makes your pick the default for new sessions.";
const TUNE_SUBTITLES = [
  TUNE_SUBTITLE,
  "Enter makes your pick the default for new sessions.",
  "Enter sets the default for new sessions.",
];
export const ULTRA_NOTE =
  "Ultracode: runs the multi-agent pipeline on every task";
const NO_SESSION_NOTE = "Effort and ultracode need an active session.";
const EFFORT_IGNORED_NOTE = "This model ignores effort.";

/** Longest model window, and the shortest one a tiny terminal may shrink it to. */
export const TUNE_MAX_LIST = 12;
export const TUNE_MIN_LIST = 3;
/** Below this many terminal columns the description column is dropped. */
export const TUNE_DESCRIPTION_COLS = 70;
const MIN_WIDTH = 40;
const MAX_WIDTH = 90;
/** Rows around the popup: its double border and top margin (3) plus the status rule and spacer under it (3). */
export const TUNE_OVERLAY_CHROME = 6;
const LABEL_MAX = 26;
const ULTRA_BLOCK_WIDTH = "Ultracode  off".length;
const ULTRA_GAP = 3;

// ── Snapshot ─────────────────────────────────────────────────────────

export interface TuneModel {
  description: string;
  /** Whether the model takes an effort level; null when unknown. */
  effort: boolean | null;
  key: string;
  /** The real model ids the key maps to, in chain order. */
  resolved: string[];
}

/** `tune.get` after validation: every field present, nothing the popup would have to guess. */
export interface TuneSnapshot {
  defaultModel: string;
  /** The session's level; null = default (no override). */
  effort: null | string;
  /** The ladder after the leftmost "default" stop. */
  efforts: string[];
  hasSession: boolean;
  model: string;
  models: TuneModel[];
  ultraOn: boolean;
  wakeWords: string[];
  wakeWordsEnabled: boolean;
}

const asString = (v: unknown) => (typeof v === "string" ? v : "");

const asStrings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : [];

/** `tune.get` / `tune.set` result -> snapshot, or null when it is not the shape (no model at all). */
export function normalizeTuneSnapshot(raw: unknown): null | TuneSnapshot {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }

  const r = raw as TuneGetResponse;
  const defaultModel = asString(r.default_model) || asString(r.model);
  const model = asString(r.model) || defaultModel;

  if (!model) {
    return null;
  }

  const seen = new Set<string>();
  const models: TuneModel[] = [];

  for (const m of Array.isArray(r.models) ? r.models : []) {
    const key = m && typeof m === "object" ? asString(m.key) : "";

    if (!key || seen.has(key)) {
      continue;
    }

    seen.add(key);
    models.push({
      description: asString(m.description).trim(),
      effort: typeof m.effort === "boolean" ? m.effort : null,
      key,
      resolved: asStrings(m.resolved),
    });
  }

  const efforts = [...new Set(asStrings(r.efforts))].filter(
    (e) => e && e !== DEFAULT_STOP,
  );

  const effort = asString(r.effort).trim().toLowerCase();

  return {
    defaultModel,
    effort: effort && effort !== DEFAULT_STOP ? effort : null,
    efforts: efforts.length ? efforts : FALLBACK_EFFORTS,
    hasSession: r.has_session === true,
    model,
    models,
    ultraOn: r.ultra_mode === ULTRA_ON,
    wakeWords: Array.isArray(r.wake_words)
      ? asStrings(r.wake_words)
      : Object.keys(WAKE_WORDS),
    wakeWordsEnabled: r.wake_words_enabled !== false,
  };
}

// ── State ────────────────────────────────────────────────────────────

export interface TuneRow {
  current: boolean;
  description: string;
  effort: boolean | null;
  isDefault: boolean;
  key: string;
  label: string;
  /** What the key resolves to ("a -> b" along the fallback chain); "" when it is the key itself. */
  resolved: string;
}

const resolvedText = (key: string, resolved: string[]) =>
  resolved.length === 1 && resolved[0] === key ? "" : resolved.join(" → ");

/**
 * Row 1 is the configured default key ("Default (recommended)"), the other
 * keys follow in catalog order; the default key is NOT repeated. A current
 * model the catalog no longer lists still gets a row, so the popup never hides
 * what the session is on.
 */
export function buildTuneRows(snap: TuneSnapshot): TuneRow[] {
  const byKey = new Map(snap.models.map((m) => [m.key, m]));

  const row = (key: string, label: string): TuneRow => {
    const m = byKey.get(key);

    return {
      current: key === snap.model,
      description: m?.description ?? "",
      effort: m?.effort ?? null,
      isDefault: key === snap.defaultModel,
      key,
      label,
      resolved: resolvedText(key, m?.resolved ?? []),
    };
  };

  const rows = [row(snap.defaultModel, DEFAULT_ROW_LABEL)];

  for (const m of snap.models) {
    if (m.key !== snap.defaultModel) {
      rows.push(row(m.key, m.key));
    }
  }

  if (!rows.some((r) => r.current)) {
    rows.push(row(snap.model, snap.model));
  }

  return rows;
}

export interface TuneState {
  /** What the session has now: a field is sent only when the popup moved off it. */
  base: { model: string; stop: number; ultra: boolean };
  cursor: number;
  hasSession: boolean;
  rows: TuneRow[];
  /** Index into `stops` of the effort mark. */
  stop: number;
  /** `default` first, then the ladder. */
  stops: string[];
  ultra: boolean;
  wakeWords: string[];
}

export function initTuneState(snap: TuneSnapshot): TuneState {
  const rows = buildTuneRows(snap);
  const stops = [DEFAULT_STOP, ...snap.efforts];
  const stop = Math.max(0, snap.effort ? stops.indexOf(snap.effort) : 0);

  return {
    base: {
      model: snap.model,
      stop,
      ultra: snap.ultraOn,
    },
    cursor: Math.max(
      0,
      rows.findIndex((r) => r.current),
    ),
    hasSession: snap.hasSession,
    rows,
    stop,
    stops,
    ultra: snap.ultraOn,
    wakeWords: snap.wakeWordsEnabled ? snap.wakeWords : [],
  };
}

// ── Keys ─────────────────────────────────────────────────────────────

export type TuneKey = Partial<
  Pick<
    Key,
    | "ctrl"
    | "downArrow"
    | "escape"
    | "leftArrow"
    | "meta"
    | "return"
    | "rightArrow"
    | "tab"
    | "upArrow"
  >
>;

export type TuneAction =
  | { type: "none" }
  | { type: "cancel" }
  | { params: TuneSetParams; type: "apply" };

export interface TuneStep {
  action: TuneAction;
  state: TuneState;
}

/**
 * The one `tune.set` the popup sends: scope plus ONLY the fields that moved off
 * what the session had. null when nothing would change, so Enter on an
 * untouched popup just closes it (and never rewrites the default model on the
 * side). Effort and ultracode need a session.
 */
export function tuneParams(
  state: TuneState,
  scope: TuneSetParams["scope"],
  sessionId: null | string | undefined,
): null | TuneSetParams {
  const { base } = state;
  const pick = state.rows[state.cursor]?.key ?? base.model;
  const params: TuneSetParams = { scope };

  if (pick !== base.model) {
    params.model = pick;
  }

  if (state.hasSession && state.stop !== base.stop) {
    params.effort = state.stops[state.stop];
  }

  if (state.hasSession && state.ultra !== base.ultra) {
    params.ultra_mode = state.ultra ? ULTRA_ON : ULTRA_OFF;
  }

  if (!params.model && !params.effort && !params.ultra_mode) {
    return null;
  }

  if (sessionId) {
    params.session_id = sessionId;
  }

  return params;
}

/**
 * One keypress: ↑/↓ move the model cursor (digits 1-9 jump to that row), ←/→
 * the effort mark, Tab toggles ultracode, Enter applies with scope `default`,
 * `s` with scope `session`, Esc closes. Locked (an approval / question /
 * password / confirm prompt is open) every key is ignored, and without a
 * session effort, ultracode and `s` are too.
 */
export function tuneKey(
  state: TuneState,
  ch: string,
  key: TuneKey,
  opts: { locked?: boolean; sessionId?: null | string } = {},
): TuneStep {
  const same: TuneStep = { action: { type: "none" }, state };

  if (opts.locked) {
    return same;
  }

  const to = (patch: Partial<TuneState>): TuneStep =>
    Object.keys(patch).every(
      (k) => patch[k as keyof TuneState] === state[k as keyof TuneState],
    )
      ? same
      : { action: { type: "none" }, state: { ...state, ...patch } };

  const apply = (scope: TuneSetParams["scope"]): TuneStep => {
    const params = tuneParams(state, scope, opts.sessionId);

    return {
      action: params ? { params, type: "apply" } : { type: "cancel" },
      state,
    };
  };

  if (key.escape) {
    return { action: { type: "cancel" }, state };
  }

  if (key.return) {
    return apply("default");
  }

  if (key.upArrow) {
    return to({ cursor: Math.max(0, state.cursor - 1) });
  }

  if (key.downArrow) {
    return to({ cursor: Math.min(state.rows.length - 1, state.cursor + 1) });
  }

  if (key.leftArrow) {
    return state.hasSession ? to({ stop: Math.max(0, state.stop - 1) }) : same;
  }

  if (key.rightArrow) {
    return state.hasSession
      ? to({ stop: Math.min(state.stops.length - 1, state.stop + 1) })
      : same;
  }

  if (key.tab) {
    return state.hasSession ? to({ ultra: !state.ultra }) : same;
  }

  if (key.ctrl || key.meta) {
    return same;
  }

  if (ch === "s" || ch === "S") {
    return state.hasSession ? apply("session") : same;
  }

  if (/^[1-9]$/.test(ch) && Number(ch) <= state.rows.length) {
    return to({ cursor: Number(ch) - 1 });
  }

  return same;
}

// ── Layout ───────────────────────────────────────────────────────────

/** Where the ultracode block sits and how the effort ladder is spaced at this width. */
export interface EffortGeometry {
  /** Columns between two stop labels. */
  gap: number;
  /** Columns left of the ladder ("Effort" lives there). */
  gutter: number;
  sliderWidth: number;
  /** Too narrow to put the ultracode block beside the ladder: it goes on its own line. */
  stacked: boolean;
}

export function effortGeometry(stops: string[], width: number): EffortGeometry {
  const labels = stops.reduce((n, s) => n + s.length, 0);
  const slider = (gap: number) => labels + gap * Math.max(0, stops.length - 1);
  // "Effort" lives in the gutter; at the narrowest popup (40) the ladder at gap 1 still fits beside it.
  const gutter = Math.max(7, Math.min(width >= 70 ? 12 : 8, width - slider(1)));

  for (const gap of [4, 3, 2, 1]) {
    if (gutter + slider(gap) + ULTRA_GAP + ULTRA_BLOCK_WIDTH <= width) {
      return { gap, gutter, sliderWidth: slider(gap), stacked: false };
    }
  }

  const gap = [3, 2, 1].find((g) => gutter + slider(g) <= width) ?? 1;

  return { gap, gutter, sliderWidth: slider(gap), stacked: true };
}

export interface TuneLayout {
  /** Show the description column. */
  descriptions: boolean;
  effort: EffortGeometry;
  /** Model rows on screen at once. */
  listRows: number;
  /** The wake-word hint line; "" when no word is enabled or none fits the width. */
  wake: string;
  /** More models than rows: edge markers and a "+N more" line. */
  windowed: boolean;
  width: number;
}

/** Popup width: what ModelPicker uses, so the two overlays line up; a grid cell may cap it. */
export const tuneWidth = (cols: number, maxWidth?: number) =>
  clampOverlayWidth(
    Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, cols - 6)),
    maxWidth,
  );

/**
 * Fit the popup to `rows` x `cols`: the list window shrinks first (never below
 * three rows), the description column goes below 70 columns, the ultracode
 * block drops under the ladder when it no longer fits beside it.
 */
export function tuneLayout(o: {
  cols: number;
  maxWidth?: number;
  rows: number;
  stops: string[];
  total: number;
  wakeWords: string[];
}): TuneLayout {
  const width = tuneWidth(o.cols, o.maxWidth);
  const effort = effortGeometry(o.stops, width);
  const wake = wakeLine(o.wakeWords, width);
  // title, subtitle, blank | blank | effort block | note | keys | wake line
  const fixed = 3 + 1 + (effort.stacked ? 4 : 3) + 1 + 1 + (wake ? 1 : 0);
  const budget = o.rows - TUNE_OVERLAY_CHROME - fixed;
  const fits = Math.min(TUNE_MAX_LIST, Math.max(TUNE_MIN_LIST, budget));
  const windowed = o.total > fits;

  return {
    descriptions: o.cols >= TUNE_DESCRIPTION_COLS,
    effort,
    // one row of the budget goes to the "+N more" line
    listRows: windowed
      ? Math.min(
          TUNE_MAX_LIST,
          Math.max(TUNE_MIN_LIST, Math.min(budget - 1, o.total - 1)),
        )
      : Math.max(1, o.total),
    wake,
    width,
    windowed,
  };
}

export interface TuneWindow {
  /** Rows scrolled out above. */
  above: number;
  /** Rows scrolled out below. */
  below: number;
  end: number;
  start: number;
}

/** The slice of `count` rows shown with the cursor kept near the middle (the same window ModelPicker uses). */
export function tuneWindow(
  count: number,
  cursor: number,
  size: number,
): TuneWindow {
  const start = windowOffset(count, cursor, Math.min(size, count));
  const end = Math.min(count, start + size);

  return { above: start, below: count - end, end, start };
}

// ── Screen ───────────────────────────────────────────────────────────

export type TuneTone = "accent" | "label" | "muted" | "ok" | "plain";

export interface TuneSeg {
  bold?: boolean;
  text: string;
  tone: TuneTone;
}

export interface TuneLine {
  /** The cursor row: painted as the shared selection chip. */
  active?: boolean;
  kind:
    | "blank"
    | "effort"
    | "keys"
    | "model"
    | "more"
    | "note"
    | "subtitle"
    | "title"
    | "wake";
  segs: TuneSeg[];
}

export const lineText = (line: TuneLine) =>
  line.segs.map((s) => s.text).join("");

const seg = (text: string, tone: TuneTone, bold?: boolean): TuneSeg => ({
  ...(bold ? { bold } : {}),
  text,
  tone,
});

const line = (kind: TuneLine["kind"], ...segs: TuneSeg[]): TuneLine => ({
  kind,
  segs,
});

const blank = (): TuneLine => line("blank", seg(" ", "muted"));

const fit = (candidates: string[], width: number) =>
  candidates.find((c) => c.length <= width) ?? candidates.at(-1)!;

const cut = (text: string, max: number) =>
  text.length > max ? `${text.slice(0, Math.max(0, max - 1))}…` : text;

function modelLines(state: TuneState, layout: TuneLayout): TuneLine[] {
  const { rows, cursor } = state;
  const win = tuneWindow(rows.length, cursor, layout.listRows);
  const numW = String(rows.length).length;

  const labels = rows.map(
    (r) => cut(r.label, LABEL_MAX) + (r.current ? " ✓" : ""),
  );

  const labelW = Math.max(...labels.map((l) => l.length));
  const out: TuneLine[] = [];

  for (let i = win.start; i < win.end; i++) {
    const row = rows[i]!;
    const active = i === cursor;

    const marker = active
      ? "❯ "
      : i === win.start && win.above > 0
        ? "↑ "
        : i === win.end - 1 && win.below > 0
          ? "↓ "
          : "  ";

    const detail = [row.resolved, layout.descriptions ? row.description : ""]
      .filter(Boolean)
      .join(" · ");

    out.push({
      active,
      kind: "model",
      segs: [
        seg(
          `${marker}${String(i + 1).padStart(numW)}. ${labels[i]!.padEnd(labelW)}`,
          row.current ? "ok" : "plain",
        ),
        ...(detail ? [seg(`  ${detail}`, "muted")] : []),
      ],
    });
  }

  if (layout.windowed) {
    const hidden =
      win.above && win.below
        ? `+${win.above} above · +${win.below} below`
        : win.below
          ? `+${win.below} more`
          : win.above
            ? `+${win.above} above`
            : "";

    out.push(
      hidden
        ? line("more", seg(`${" ".repeat(numW + 2)}… ${hidden}`, "muted"))
        : { ...blank(), kind: "more" },
    );
  }

  return out;
}

/** Effort ladder (marker on the ruler, labels beneath) with the ultracode block beside it. */
function effortLines(state: TuneState, layout: TuneLayout): TuneLine[] {
  const { gap, gutter, sliderWidth, stacked } = layout.effort;
  const on = state.hasSession;
  const dim: TuneTone = "muted";
  const lit: TuneTone = on ? "accent" : dim;
  const pad = " ".repeat(gutter);
  const gapText = " ".repeat(gap);

  const starts: number[] = [];
  let at = 0;

  for (const label of state.stops) {
    starts.push(at);
    at += label.length + gap;
  }

  const picked = state.stops[state.stop] ?? DEFAULT_STOP;
  const mark = (starts[state.stop] ?? 0) + Math.floor(picked.length / 2);

  const captions = line(
    "effort",
    seg("Effort".padEnd(gutter), "label", true),
    seg(
      `Faster${" ".repeat(Math.max(1, sliderWidth - "Faster".length - "Smarter".length))}Smarter`,
      dim,
    ),
  );

  const ruler = line(
    "effort",
    seg(pad + "─".repeat(mark), dim),
    seg("▲", lit, true),
    seg("─".repeat(Math.max(0, sliderWidth - mark - 1)), dim),
  );

  const ladder = line(
    "effort",
    seg(pad, dim),
    ...state.stops.flatMap((label, i) => [
      ...(i ? [seg(gapText, dim)] : []),
      i === state.stop ? seg(label, lit, true) : seg(label, dim),
    ]),
  );

  const ultra: TuneSeg[] = [
    seg("Ultracode  ", on ? "plain" : dim),
    state.ultra ? seg("on", lit, true) : seg("off", dim),
  ];

  const toggle = seg("Tab to toggle", dim);
  const beside = " ".repeat(ULTRA_GAP);

  if (stacked) {
    return [
      captions,
      ruler,
      ladder,
      line("effort", seg(pad, dim), ...ultra, seg(" · ", dim), toggle),
    ];
  }

  return [
    { ...captions, segs: [...captions.segs, seg(beside, dim), ...ultra] },
    { ...ruler, segs: [...ruler.segs, seg(beside, dim), toggle] },
    ladder,
  ];
}

const KEYS_SESSION = [
  "↑/↓ model · ←/→ effort · Tab ultracode · Enter set as default · s this session only · Esc cancel",
  "↑/↓ model · ←/→ effort · Tab ultracode · Enter default · s session · Esc cancel",
  "↑↓ model · ←→ effort · Tab ultracode · Enter default · s session · Esc",
  "↑↓ ←→ Tab · Enter default · s session · Esc",
];

const KEYS_NO_SESSION = [
  "↑/↓ model · Enter set as default · Esc cancel",
  "↑↓ model · Enter default · Esc",
];

const KEYS_LOCKED = [
  "Paused: answer the open prompt first.",
  "Paused: answer the prompt",
];

/** The one note under the ladder: no session, else a model that ignores effort, else what ultracode does. */
export function tuneNote(state: TuneState): string {
  if (!state.hasSession) {
    return NO_SESSION_NOTE;
  }

  if (state.rows[state.cursor]?.effort === false) {
    return EFFORT_IGNORED_NOTE;
  }

  return state.ultra ? ULTRA_NOTE : "";
}

/** The wake-word hint under the keys; "" when no word is enabled or not even the shortest wording fits. */
function wakeLine(words: string[], width: number): string {
  const list = words.join(" / ");

  return (
    [
      `Say ${list} in a prompt to run that mode once.`,
      `Say ${list} in a prompt to run it once.`,
      `Wake words: ${words.join(" · ")}`,
    ].find((text) => words.length > 0 && text.length <= width) ?? ""
  );
}

/** The whole popup, top to bottom, as lines of tone-tagged text. */
export function tuneScreen(
  state: TuneState,
  layout: TuneLayout,
  opts: { locked?: boolean } = {},
): TuneLine[] {
  const keys = opts.locked
    ? KEYS_LOCKED
    : state.hasSession
      ? KEYS_SESSION
      : KEYS_NO_SESSION;

  const lines: TuneLine[] = [
    line("title", seg("Tune", "accent", true)),
    line("subtitle", seg(fit(TUNE_SUBTITLES, layout.width), "muted")),
    blank(),
    ...modelLines(state, layout),
    blank(),
    ...effortLines(state, layout),
    line("note", seg(tuneNote(state) || " ", "muted")),
    line("keys", seg(fit(keys, layout.width), "muted")),
  ];

  if (layout.wake) {
    lines.push(line("wake", seg(layout.wake, "muted")));
  }

  return lines;
}

// ── Result ───────────────────────────────────────────────────────────

/** The transcript line after a `tune.set`. */
export function tuneSummary(
  params: TuneSetParams,
  res: null | TuneSetResponse,
): string {
  if (res?.changed && res.changed.length === 0) {
    return "tune: nothing changed";
  }

  const parts: string[] = [];

  if (params.model) {
    parts.push(
      `model ${params.model}${params.scope === "default" ? " (default for new sessions)" : ""}`,
    );
  }

  if (params.effort) {
    parts.push(`effort ${params.effort}`);
  }

  if (params.ultra_mode) {
    parts.push(`ultracode ${params.ultra_mode === ULTRA_ON ? "on" : "off"}`);
  }

  return `tune → ${parts.join(" · ")}`;
}
