import { describe, expect, it } from "vitest";

import {
  buildTuneRows,
  DEFAULT_ROW_LABEL,
  initTuneState,
  lineText,
  normalizeTuneSnapshot,
  TUNE_MIN_LIST,
  TUNE_OVERLAY_CHROME,
  tuneKey,
  tuneLayout,
  tuneNote,
  tuneParams,
  tuneScreen,
  type TuneSnapshot,
  type TuneState,
  tuneSummary,
  tuneWindow,
} from "../domain/tune.js";

const wire = (over: Record<string, unknown> = {}) => ({
  default_model: "main",
  effort: "high",
  efforts: ["low", "medium", "high", "xhigh", "max"],
  has_session: true,
  model: "fast",
  models: [
    {
      description: "The everyday model",
      effort: true,
      key: "main",
      resolved: ["claude-main-4"],
    },
    {
      description: "",
      effort: true,
      key: "fast",
      resolved: ["claude-fast-4", "backup-fast"],
    },
    { description: "Tiny", effort: false, key: "tiny", resolved: ["tiny"] },
  ],
  ultra_mode: "off",
  ultra_modes: ["off", "ultracode"],
  wake_words: ["ultracode", "ultraplan", "ultraresearch"],
  wake_words_enabled: true,
  ...over,
});

const snapshot = (over: Record<string, unknown> = {}): TuneSnapshot =>
  normalizeTuneSnapshot(wire(over))!;

const state = (over: Record<string, unknown> = {}) =>
  initTuneState(snapshot(over));

/** Press keys in order; the last step's action is what the popup would do. */
const press = (
  start: TuneState,
  ...keys: Array<[string, Record<string, boolean>?]>
) => {
  let s = start;
  let last = tuneKey(s, "", {});

  for (const [ch, key] of keys) {
    last = tuneKey(s, ch, key ?? {}, {
      sessionId: s.hasSession ? "sid-1" : null,
    });
    s = last.state;
  }

  return { action: last.action, state: s };
};

const DOWN: [string, Record<string, boolean>] = ["", { downArrow: true }];
const UP: [string, Record<string, boolean>] = ["", { upArrow: true }];
const LEFT: [string, Record<string, boolean>] = ["", { leftArrow: true }];
const RIGHT: [string, Record<string, boolean>] = ["", { rightArrow: true }];
const TAB: [string, Record<string, boolean>] = ["", { tab: true }];
const ENTER: [string, Record<string, boolean>] = ["", { return: true }];
const ESC: [string, Record<string, boolean>] = ["", { escape: true }];

describe("normalizeTuneSnapshot", () => {
  it("rejects anything that is not a tune.get shape with a model", () => {
    expect(normalizeTuneSnapshot(null)).toBeNull();
    expect(normalizeTuneSnapshot([])).toBeNull();
    expect(normalizeTuneSnapshot({ models: [] })).toBeNull();
  });

  it("fills what the gateway left out", () => {
    const snap = normalizeTuneSnapshot({ model: "main" })!;

    expect(snap).toMatchObject({
      defaultModel: "main",
      effort: null,
      efforts: ["low", "medium", "high", "xhigh", "max"],
      hasSession: false,
      ultraOn: false,
      wakeWordsEnabled: true,
    });
    expect(snap.wakeWords).toEqual(["ultracode", "ultraplan", "ultraresearch"]);
  });

  it("reads the effort level, treating default/empty as no override", () => {
    expect(snapshot({ effort: "XHigh" }).effort).toBe("xhigh");
    expect(snapshot({ effort: null }).effort).toBeNull();
    expect(snapshot({ effort: "default" }).effort).toBeNull();
  });

  it("drops duplicate and keyless models and a stray `default` stop", () => {
    const snap = snapshot({
      efforts: ["default", "low", "low", "high"],
      models: [{ key: "a" }, { key: "a" }, { key: "" }, 7, null],
    });

    expect(snap.models.map((m) => m.key)).toEqual(["a"]);
    expect(snap.efforts).toEqual(["low", "high"]);
  });
});

describe("buildTuneRows", () => {
  it("puts the configured default first, once, labelled Default (recommended)", () => {
    const rows = buildTuneRows(snapshot());

    expect(rows.map((r) => r.key)).toEqual(["main", "fast", "tiny"]);
    expect(rows[0]).toMatchObject({
      isDefault: true,
      label: DEFAULT_ROW_LABEL,
      resolved: "claude-main-4",
    });
    expect(rows.filter((r) => r.key === "main")).toHaveLength(1);
  });

  it("marks exactly the session's model as current and shows the fallback chain", () => {
    const rows = buildTuneRows(snapshot());

    expect(rows.filter((r) => r.current).map((r) => r.key)).toEqual(["fast"]);
    expect(rows[1]!.resolved).toBe("claude-fast-4 → backup-fast");
  });

  it("does not repeat a model id that is the key itself", () => {
    expect(buildTuneRows(snapshot())[2]!.resolved).toBe("");
  });

  it("keeps the current model visible even when the catalog dropped it", () => {
    const rows = buildTuneRows(snapshot({ model: "retired" }));

    expect(rows.at(-1)).toMatchObject({ current: true, key: "retired" });
  });

  it("builds a row for the default even when the catalog does not list it", () => {
    const rows = buildTuneRows(snapshot({ models: [{ key: "fast" }] }));

    expect(rows.map((r) => r.key)).toEqual(["main", "fast"]);
  });
});

describe("initTuneState", () => {
  it("starts on the current model, effort and ultracode setting", () => {
    const s = state({ effort: "xhigh", ultra_mode: "ultracode" });

    expect(s.rows[s.cursor]!.key).toBe("fast");
    expect(s.stops).toEqual([
      "default",
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    expect(s.stops[s.stop]).toBe("xhigh");
    expect(s.ultra).toBe(true);
  });

  it("starts the effort mark on `default` when the session has no override", () => {
    const s = state({ effort: null });

    expect(s.stop).toBe(0);
  });

  it("hides the wake-word hint when the core switched wake words off", () => {
    expect(state({ wake_words_enabled: false }).wakeWords).toEqual([]);
  });
});

describe("tuneKey", () => {
  it("moves the model cursor with up/down and stops at the ends", () => {
    const s = state();

    expect(s.cursor).toBe(1);
    expect(press(s, DOWN).state.cursor).toBe(2);
    expect(press(s, DOWN, DOWN, DOWN).state.cursor).toBe(2);
    expect(press(s, UP, UP, UP).state.cursor).toBe(0);
  });

  it("jumps to the row a digit names, ignoring digits that name no row", () => {
    const s = state();

    expect(press(s, ["3"]).state.cursor).toBe(2);
    expect(press(s, ["1"]).state.cursor).toBe(0);
    expect(press(s, ["9"]).state.cursor).toBe(1);
    expect(press(s, ["0"]).state.cursor).toBe(1);
  });

  it("does not take a Ctrl/Meta chord for a digit or `s`", () => {
    const s = state();

    expect(press(s, ["1", { ctrl: true }]).state.cursor).toBe(1);
    expect(press(s, ["s", { meta: true }]).action.type).toBe("none");
  });

  it("moves the effort mark with left/right across `default` .. the top level", () => {
    const s = state({ effort: "high" });

    expect(s.stops[press(s, LEFT).state.stop]).toBe("medium");
    expect(s.stops[press(s, RIGHT).state.stop]).toBe("xhigh");
    expect(press(s, RIGHT, RIGHT, RIGHT).state.stop).toBe(5);
    expect(press(s, LEFT, LEFT, LEFT, LEFT, LEFT, LEFT).state.stop).toBe(0);
  });

  it("toggles ultracode with Tab", () => {
    const s = state();

    expect(press(s, TAB).state.ultra).toBe(true);
    expect(press(s, TAB, TAB).state.ultra).toBe(false);
  });

  it("closes on Esc and changes nothing", () => {
    const s = state();
    const moved = press(s, DOWN, RIGHT, TAB, ESC);

    expect(moved.action).toEqual({ type: "cancel" });
  });

  it("returns the very same state object for a key that does nothing", () => {
    const s = state();

    expect(tuneKey(s, "x", {}).state).toBe(s);
    expect(press(s, UP, UP).state).toEqual(press(s, UP).state);
  });
});

describe("tune.set payload", () => {
  it("Enter sends model + effort + ultracode in ONE tune.set with scope default", () => {
    const { action } = press(state(), UP, RIGHT, TAB, ENTER);

    expect(action).toEqual({
      params: {
        effort: "xhigh",
        model: "main",
        scope: "default",
        session_id: "sid-1",
        ultra_mode: "ultracode",
      },
      type: "apply",
    });
  });

  it("`s` sends the same payload with scope session", () => {
    const { action } = press(state(), DOWN, ["s"]);

    expect(action).toMatchObject({
      params: { model: "tiny", scope: "session" },
      type: "apply",
    });
  });

  it("sends only the fields that changed", () => {
    expect(press(state(), RIGHT, ENTER).action).toEqual({
      params: {
        effort: "xhigh",
        scope: "default",
        session_id: "sid-1",
      },
      type: "apply",
    });
    expect(press(state(), TAB, ["s"]).action).toEqual({
      params: {
        scope: "session",
        session_id: "sid-1",
        ultra_mode: "ultracode",
      },
      type: "apply",
    });
  });

  it("sends `default` when the mark goes back to the leftmost stop", () => {
    const { action } = press(state({ effort: "low" }), LEFT, ENTER);

    expect(action).toMatchObject({ params: { effort: "default" } });
  });

  it("sends ultra_mode off when ultracode was on and Tab turned it off", () => {
    const { action } = press(state({ ultra_mode: "ultracode" }), TAB, ENTER);

    expect(action).toMatchObject({ params: { ultra_mode: "off" } });
  });

  it("a move that is undone again sends nothing", () => {
    expect(press(state(), RIGHT, LEFT, TAB, TAB, ENTER).action).toEqual({
      type: "cancel",
    });
  });

  it("never rewrites the default model on the side: Enter on an untouched model sends no model", () => {
    // current = fast, default = main: only the effort moved.
    const { action } = press(state(), RIGHT, ENTER);

    expect(action).toEqual({
      params: { effort: "xhigh", scope: "default", session_id: "sid-1" },
      type: "apply",
    });
    expect(press(state(), ENTER).action).toEqual({ type: "cancel" });
    expect(press(state(), ["s"]).action).toEqual({ type: "cancel" });
  });

  it("sends the model when the pick moved off the session's model, to whichever scope", () => {
    expect(press(state(), UP, ENTER).action).toMatchObject({
      params: { model: "main", scope: "default" },
    });
    expect(press(state(), UP, ["s"]).action).toMatchObject({
      params: { model: "main", scope: "session" },
    });
    // moving off and back is no change
    expect(press(state(), UP, DOWN, ENTER).action).toEqual({ type: "cancel" });
  });

  it("omits session_id when the popup has no session id", () => {
    expect(tuneParams(press(state(), DOWN).state, "default", null)).toEqual({
      model: "tiny",
      scope: "default",
    });
  });
});

describe("without a session", () => {
  const none = () => state({ effort: null, has_session: false, model: "main" });

  it("ignores the effort keys, Tab and `s`, but still moves through the models", () => {
    const s = none();

    expect(press(s, RIGHT).state).toBe(s);
    expect(press(s, LEFT).state).toBe(s);
    expect(press(s, TAB).state).toBe(s);
    expect(press(s, ["s"]).action).toEqual({ type: "none" });
    expect(press(s, DOWN).state.cursor).toBe(1);
  });

  it("can still set the default model, with default scope and no effort or ultracode", () => {
    expect(press(none(), DOWN, ENTER).action).toEqual({
      params: { model: "fast", scope: "default" },
      type: "apply",
    });
  });

  it("greys the ladder and the ultracode block and says why", () => {
    const s = none();

    const screen = tuneScreen(
      s,
      tuneLayout({
        cols: 80,
        rows: 24,
        stops: s.stops,
        total: s.rows.length,
        wakeWords: s.wakeWords,
      }),
    );

    const effort = screen.filter((l) => l.kind === "effort");

    expect(effort.length).toBeGreaterThanOrEqual(3);

    for (const l of effort) {
      expect(l.segs.every((x) => x.tone !== "accent")).toBe(true);
    }

    expect(tuneNote(s)).toMatch(/active session/);
    expect(screen.find((l) => l.kind === "keys")?.segs[0]?.text).not.toMatch(
      /effort|ultracode/,
    );
  });
});

describe("locked while a prompt is open", () => {
  it("ignores every key, Esc and Enter included", () => {
    const s = state();

    for (const [ch, key] of [
      DOWN,
      UP,
      LEFT,
      RIGHT,
      TAB,
      ENTER,
      ESC,
      ["s"],
      ["2"],
    ] as const) {
      const step = tuneKey(s, ch, key ?? {}, { locked: true });

      expect(step.action).toEqual({ type: "none" });
      expect(step.state).toBe(s);
    }
  });
});

describe("tuneNote", () => {
  it("says the highlighted model ignores effort", () => {
    const s = state({ model: "tiny" });

    expect(s.rows[s.cursor]!.effort).toBe(false);
    expect(tuneNote(s)).toMatch(/ignores effort/);
    expect(tuneNote(press(s, UP).state)).toBe("");
  });

  it("describes ultracode only while it is on", () => {
    expect(tuneNote(state())).toBe("");
    expect(tuneNote(press(state(), TAB).state)).toMatch(
      /multi-agent pipeline on every task/,
    );
  });
});

const models = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    description: `model number ${i}`,
    effort: true,
    key: i === 0 ? "main" : `m${i}`,
    resolved: [`vendor/model-${i}`],
  }));

describe("tuneWindow", () => {
  it("shows everything when it fits", () => {
    expect(tuneWindow(4, 2, 6)).toEqual({
      above: 0,
      below: 0,
      end: 4,
      start: 0,
    });
  });

  it("keeps the cursor near the middle and counts what scrolled out", () => {
    expect(tuneWindow(20, 0, 7)).toEqual({
      above: 0,
      below: 13,
      end: 7,
      start: 0,
    });
    expect(tuneWindow(20, 10, 7)).toEqual({
      above: 7,
      below: 6,
      end: 14,
      start: 7,
    });
    expect(tuneWindow(20, 19, 7)).toEqual({
      above: 13,
      below: 0,
      end: 20,
      start: 13,
    });
  });
});

describe("tuneScreen", () => {
  const screenFor = (
    over: Record<string, unknown>,
    cols: number,
    rows: number,
    keys: Array<[string, Record<string, boolean>?]> = [],
  ) => {
    const s = press(state(over), ...keys).state;

    const layout = tuneLayout({
      cols,
      rows,
      stops: s.stops,
      total: s.rows.length,
      wakeWords: s.wakeWords,
    });

    return { layout, lines: tuneScreen(s, layout), state: s };
  };

  const many = { model: "m4", models: models(14) };

  it("lays out the popup the way the spec draws it", () => {
    const { lines } = screenFor(wire(), 100, 40);
    const text = lines.map(lineText);

    expect(text[0]).toBe("Tune");
    expect(text[1]).toMatch(/^Switch between models\. Enter makes your pick/);
    expect(text[3]).toMatch(
      /^ {2}1\. Default \(recommended\) {2}claude-main-4/,
    );
    expect(text[4]).toMatch(/^❯ 2\. fast ✓/);
    expect(text.find((l) => l.startsWith("Effort"))).toMatch(
      /Faster +Smarter +Ultracode {2}off/,
    );
    expect(text.some((l) => /^ {12}─+▲─+ +Tab to toggle$/.test(l))).toBe(true);
    expect(
      text.some((l) => /^ {12}default +low +medium +high +xhigh +max$/.test(l)),
    ).toBe(true);
    expect(text.at(-2)).toMatch(/Esc cancel$/);
    expect(text.at(-1)).toMatch(
      /^Say ultracode \/ ultraplan \/ ultraresearch in a prompt to run that mode once\.$/,
    );
  });

  it("marks the cursor row with ❯ and paints the current model green with ✓", () => {
    const { lines } = screenFor(wire(), 100, 40, [DOWN]);
    const rows = lines.filter((l) => l.kind === "model");

    expect(rows.map((l) => l.active ?? false)).toEqual([false, false, true]);
    expect(lineText(rows[2]!)).toMatch(/^❯ 3\. tiny/);

    const current = rows[1]!;

    expect(lineText(current)).toContain("fast ✓");
    expect(current.segs[0]!.tone).toBe("ok");
  });

  it("puts the effort ▲ under the picked stop", () => {
    const { lines } = screenFor(wire({ effort: "low" }), 100, 40);
    const text = lines.map(lineText);
    const ruler = text.find((l) => l.includes("▲"))!;
    const ladder = text.find((l) => /^ +default +low/.test(l))!;
    const mark = ruler.indexOf("▲");

    expect(ladder.slice(mark - 1, mark + 2)).toBe("low");
  });

  it("scrolls with ↑/↓ edge markers and a count of what is hidden", () => {
    const top = screenFor(many, 80, 24, [UP, UP, UP, UP, UP]).lines;
    const topRows = top.filter((l) => l.kind === "model").map(lineText);

    expect(topRows[0]).toMatch(/^❯ {2}1\. Default \(recommended\)/);
    expect(topRows.at(-1)).toMatch(/^↓ /);
    expect(lineText(top.find((l) => l.kind === "more")!)).toMatch(
      /… \+\d+ more$/,
    );

    const mid = screenFor(many, 80, 24).lines.filter((l) => l.kind === "model");

    expect(lineText(mid[0]!)).toMatch(/^↑ /);
    expect(lineText(mid.at(-1)!)).toMatch(/^↓ /);

    const bottom = screenFor(many, 80, 24, [
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
      DOWN,
    ]);

    expect(lineText(bottom.lines.find((l) => l.kind === "more")!)).toMatch(
      /… \+\d+ above$/,
    );
    expect(
      lineText(bottom.lines.filter((l) => l.kind === "model").at(-1)!),
    ).toMatch(/^❯ 14\. m13/);
  });

  it("numbers the rows so the digits line up", () => {
    const rows = screenFor(many, 100, 40).lines.filter(
      (l) => l.kind === "model",
    );

    expect(lineText(rows[0]!)).toMatch(/^ {3}1\. /);
    expect(lineText(rows[9]!)).toMatch(/^ {2}10\. /);
  });

  it("fits 80x24 even with a long model list: the list window gives way, never the rest", () => {
    const { layout, lines } = screenFor(many, 80, 24);

    expect(lines.length + TUNE_OVERLAY_CHROME).toBeLessThanOrEqual(24);
    expect(layout.windowed).toBe(true);
    expect(layout.listRows).toBeLessThan(14);
    expect(layout.listRows).toBeGreaterThanOrEqual(TUNE_MIN_LIST);

    // nothing is truncated at 80 columns: every line is within the popup width
    for (const l of lines) {
      expect(lineText(l).length, lineText(l)).toBeLessThanOrEqual(layout.width);
    }

    // the ladder, the ultracode block and both footers are all on screen
    const kinds = lines.map((l) => l.kind);

    expect(kinds.filter((k) => k === "effort")).toHaveLength(3);
    expect(kinds).toContain("keys");
    expect(kinds).toContain("wake");
  });

  it("fits other small terminals too, down to the three-row floor", () => {
    for (const [cols, rows] of [
      [80, 24],
      [100, 24],
      [60, 24],
      [80, 30],
      [120, 40],
      [50, 22],
    ] as const) {
      const { layout, lines } = screenFor(many, cols, rows);

      expect(layout.width).toBeLessThanOrEqual(Math.max(40, cols - 6));
      expect(
        lines.length + TUNE_OVERLAY_CHROME,
        `${cols}x${rows}`,
      ).toBeLessThanOrEqual(rows);
    }
  });

  it("grows the list window with the terminal, up to twelve rows", () => {
    const small = screenFor(many, 80, 24).layout.listRows;
    const tall = screenFor(many, 80, 30).layout.listRows;
    const huge = screenFor(many, 80, 60).layout.listRows;

    expect(tall).toBeGreaterThan(small);
    expect(huge).toBe(12);
  });

  it("shows every model, with no more-line, when they all fit", () => {
    const { layout, lines } = screenFor(wire(), 80, 24);

    expect(layout.windowed).toBe(false);
    expect(lines.filter((l) => l.kind === "model")).toHaveLength(3);
    expect(lines.some((l) => l.kind === "more")).toBe(false);
  });

  it("drops the description column below 70 columns", () => {
    const wide = screenFor(wire(), 80, 30).lines.map(lineText).join("\n");
    const narrow = screenFor(wire(), 66, 30).lines.map(lineText).join("\n");

    expect(wide).toContain("The everyday model");
    expect(narrow).not.toContain("The everyday model");
    expect(narrow).toContain("claude-main-4");
  });

  it("puts the ultracode block under the ladder when the width cannot hold both", () => {
    const wide = screenFor(wire(), 80, 30);
    const narrow = screenFor(wire(), 56, 30);

    expect(wide.layout.effort.stacked).toBe(false);
    expect(narrow.layout.effort.stacked).toBe(true);

    const text = narrow.lines.map(lineText);

    expect(
      text.some((l) => /^ +Ultracode {2}off · Tab to toggle$/.test(l)),
    ).toBe(true);
    expect(narrow.lines.filter((l) => l.kind === "effort")).toHaveLength(4);
  });

  it("spaces the ladder so it never overflows a narrow popup", () => {
    for (const cols of [46, 50, 56, 64, 72, 80, 100]) {
      const { layout, lines } = screenFor(wire(), cols, 30);

      for (const l of lines.filter((x) => x.kind === "effort")) {
        expect(
          lineText(l).length,
          `${cols}: ${lineText(l)}`,
        ).toBeLessThanOrEqual(layout.width);
      }
    }
  });

  it("describes ultracode only while it is on", () => {
    const off = screenFor(wire(), 100, 30).lines.map(lineText).join("\n");
    const on = screenFor(wire(), 100, 30, [TAB]).lines.map(lineText).join("\n");

    expect(off).not.toContain("multi-agent pipeline");
    expect(on).toContain(
      "Ultracode: runs the multi-agent pipeline on every task",
    );
    expect(on).toMatch(/Ultracode {2}on/);
  });

  it("shows the effort note for a model that ignores effort", () => {
    const { lines } = screenFor(wire({ model: "tiny" }), 100, 30);

    expect(lines.find((l) => l.kind === "note")!.segs[0]!.text).toMatch(
      /ignores effort/,
    );
  });

  it("leaves the wake-word hint out when wake words are off or the popup is too narrow", () => {
    const off = screenFor(wire({ wake_words_enabled: false }), 100, 30).lines;
    const none = screenFor(wire({ wake_words: [] }), 100, 30).lines;
    const tiny = screenFor(wire(), 46, 30).lines;

    expect(off.some((l) => l.kind === "wake")).toBe(false);
    expect(none.some((l) => l.kind === "wake")).toBe(false);
    expect(tiny.some((l) => l.kind === "wake")).toBe(false);
  });

  it("lists only the enabled wake words", () => {
    const { lines } = screenFor(wire({ wake_words: ["ultracode"] }), 100, 30);

    expect(lineText(lines.find((l) => l.kind === "wake")!)).toBe(
      "Say ultracode in a prompt to run that mode once.",
    );
  });

  it("swaps the key hints for a pause notice while a prompt is open", () => {
    const s = state();

    const layout = tuneLayout({
      cols: 100,
      rows: 30,
      stops: s.stops,
      total: s.rows.length,
      wakeWords: s.wakeWords,
    });

    const keys = tuneScreen(s, layout, { locked: true }).find(
      (l) => l.kind === "keys",
    )!;

    expect(lineText(keys)).toMatch(/^Paused/);
  });

  it("renders the same text on every platform: only fixed glyphs, no OS branches", () => {
    const text = screenFor(many, 80, 24).lines.map(lineText).join("\n");

    // eslint-disable-next-line no-control-regex
    expect(text).not.toMatch(/[\u0000-\u0008\u000b-\u001f]/);
    expect(text).toMatch(/[❯↑↓✓▲─…·]/);
  });
});

describe("tuneSummary", () => {
  it("names what was set and for whom", () => {
    expect(
      tuneSummary(
        {
          effort: "high",
          model: "main",
          scope: "default",
          ultra_mode: "ultracode",
        },
        { changed: ["model", "effort", "ultra_mode"], ok: true },
      ),
    ).toBe(
      "tune → model main (default for new sessions) · effort high · ultracode on",
    );
    expect(tuneSummary({ model: "fast", scope: "session" }, { ok: true })).toBe(
      "tune → model fast",
    );
    expect(tuneSummary({ scope: "session", ultra_mode: "off" }, null)).toBe(
      "tune → ultracode off",
    );
  });

  it("says so when the gateway reports nothing changed", () => {
    expect(
      tuneSummary(
        { model: "main", scope: "session" },
        { changed: [], ok: true },
      ),
    ).toBe("tune: nothing changed");
  });
});
