import { describe, expect, it } from "vitest";

import {
  applySnapshot,
  autocompactLabel,
  currentFields,
  DASHBOARD_FIELDS,
  DASHBOARD_OVERLAY_CHROME,
  DASHBOARD_TABS,
  dashboardChanges,
  dashboardClick,
  dashboardClickAt,
  type DashboardHit,
  dashboardHit,
  dashboardKey,
  dashboardLayout,
  dashboardPatch,
  dashboardRevert,
  dashboardScreen,
  type DashboardSnapshot,
  type DashboardState,
  dashboardSummary,
  type DashboardTabId,
  type DashboardUi,
  dashboardWheel,
  dashboardWidth,
  failSnapshot,
  fieldsOf,
  initDashboardState,
  lineText,
  normalizeDashboardSnapshot,
  parseDashboardTab,
  SECTION_DEFAULT,
  setField,
} from "../domain/dashboard.js";

const UI: DashboardUi = {
  autocompact: "auto",
  battery: false,
  busyInputMode: "queue",
  compact: false,
  detailsMode: "collapsed",
  detailsModeCommandOverride: false,
  focusView: false,
  indicatorStyle: "kaomoji",
  mouseTracking: "all",
  sections: {},
  showReasoning: false,
  statusBar: "top",
};

/** What `settings.get` said: a model `a` of two, plan mode available, two output styles, a 30 line status text. */
const SNAP: DashboardSnapshot = {
  effort: "default",
  model: "a",
  modelKeys: ["a", "b"],
  outputStyle: "default",
  outputStyles: ["default", "concise"],
  permissionMode: "default",
  permissionModes: ["default", "plan"],
  statusLines: Array.from({ length: 30 }, (_, i) => `status line ${i + 1}`),
  theme: "auto",
};

const LAYOUT = dashboardLayout({ cols: 100, rows: 40 });

const press = (
  state: DashboardState,
  ch: string,
  key: Parameters<typeof dashboardKey>[2] = {},
  opts: { locked?: boolean } = {},
) => dashboardKey(state, ch, key, opts);

/** Walk a sequence of steps, keeping the state and every action. */
const run = (
  state: DashboardState,
  ...keys: [string, Parameters<typeof dashboardKey>[2]?][]
) => {
  let cur = state;
  const actions = [];

  for (const [ch, key] of keys) {
    const step = press(cur, ch, key ?? {});

    cur = step.state;
    actions.push(step.action);
  }

  return { actions, state: cur };
};

const onTab = (tab: DashboardTabId) => initDashboardState(UI, tab);

/** A popup whose `settings.get` answer has arrived. */
const ready = (tab: DashboardTabId, ui: DashboardUi = UI) =>
  applySnapshot(initDashboardState(ui, tab), SNAP);

/** Type `text` one character at a time. */
const typeKeys = (state: DashboardState, text: string) =>
  [...text].reduce((s, ch) => press(s, ch).state, state);

/** `/`, then `text` into the box. */
const searchFor = (state: DashboardState, text: string) =>
  typeKeys(press(state, "/").state, text);

/** The text typed and Enter: the box is closed and the text kept. */
const kept = (state: DashboardState, text: string) =>
  press(searchFor(state, text), "", { return: true }).state;

const ids = (state: DashboardState) => currentFields(state).map((f) => f.id);

const screenText = (
  state: DashboardState,
  layout = LAYOUT,
  opts: { locked?: boolean } = {},
) => dashboardScreen(state, layout, opts).map(lineText);

const lineOf = (
  state: DashboardState,
  kind: ReturnType<typeof dashboardScreen>[number]["kind"],
  layout = LAYOUT,
) => lineText(dashboardScreen(state, layout).find((l) => l.kind === kind)!);

const rowOf = (id: string) => fieldsOf("session").findIndex((f) => f.id === id);

const L8024 = dashboardLayout({ cols: 80, rows: 24 });
const L6016 = dashboardLayout({ cols: 60, rows: 16 });

describe("dashboard fields", () => {
  it("lists the look, details, input, context and session rows, each with distinct ids and keys; Status has none", () => {
    expect(DASHBOARD_TABS.map((t) => t.id)).toEqual([
      "look",
      "details",
      "input",
      "context",
      "session",
      "status",
    ]);
    expect(fieldsOf("status")).toEqual([]);
    expect(new Set(DASHBOARD_FIELDS.map((f) => f.id)).size).toBe(
      DASHBOARD_FIELDS.length,
    );
    expect(new Set(DASHBOARD_FIELDS.map((f) => f.key)).size).toBe(
      DASHBOARD_FIELDS.length,
    );
    expect(fieldsOf("look").map((f) => f.key)).toEqual([
      "statusbar",
      "indicator",
      "density",
      "battery",
    ]);
    expect(fieldsOf("input").map((f) => f.key)).toEqual([
      "mouse",
      "busy",
      "focus",
    ]);
  });

  it("starts every row on the value the ui has, a section without an override on default", () => {
    const state = initDashboardState({
      ...UI,
      battery: true,
      mouseTracking: "wheel",
      sections: { tools: "hidden" },
    });

    expect(state.values).toMatchObject({
      battery: "on",
      details: "collapsed",
      mouse: "wheel",
      "section.thinking": SECTION_DEFAULT,
      "section.tools": "hidden",
    });

    // the rows the backend answers for have no value until the snapshot arrives
    const fromBackend = ["effort", "model", "permissions", "style", "theme"];

    for (const f of DASHBOARD_FIELDS.filter(
      (f) => !fromBackend.includes(f.id),
    )) {
      expect(f.options, f.id).toContain(state.values[f.id]);
    }

    for (const id of fromBackend) {
      expect(state.values[id]).toBeUndefined();
    }
  });

  it("opens on the tab it is asked for and knows tabs by name or number", () => {
    expect(onTab("input").tab).toBe(2);
    expect(parseDashboardTab("Details")).toBe("details");
    expect(parseDashboardTab(" 3 ")).toBe("input");
    expect(parseDashboardTab("Session")).toBe("session");
    expect(parseDashboardTab("5")).toBe("session");
    expect(parseDashboardTab("status")).toBe("status");
    expect(parseDashboardTab("6")).toBe("status");
    expect(parseDashboardTab("7")).toBeNull();
    expect(parseDashboardTab("nope")).toBeNull();
    expect(parseDashboardTab("")).toBeNull();
  });
});

describe("dashboardKey", () => {
  it("moves the cursor with the arrows and j/k and stops at the ends", () => {
    const { state } = run(
      onTab("look"),
      ["", { upArrow: true }],
      ["j"],
      ["j"],
      ["", { downArrow: true }],
      ["j"],
      ["j"],
    );

    expect(state.cursor).toBe(3);
    expect(run(state, ["k"], ["", { upArrow: true }]).state.cursor).toBe(1);
  });

  it("changes the row with ←/→, h/l and space, wrapping round the ring of chips", () => {
    const start = onTab("look");

    expect(press(start, "", { rightArrow: true }).state.values.statusbar).toBe(
      "bottom",
    );
    expect(press(start, "l").state.values.statusbar).toBe("bottom");
    expect(press(start, " ").state.values.statusbar).toBe("bottom");
    // left from the first option wraps to the last
    expect(press(start, "", { leftArrow: true }).state.values.statusbar).toBe(
      "off",
    );
    expect(press(start, "h").state.values.statusbar).toBe("off");
  });

  it("changes tab with Tab, Shift+Tab and the digits, and the cursor starts at the top of the new tab", () => {
    const moved = run(onTab("look"), ["j"], ["j"]).state;

    expect(moved.cursor).toBe(2);

    const next = press(moved, "", { tab: true }).state;

    expect(next.tab).toBe(1);
    expect(next.cursor).toBe(0);
    expect(press(next, "", { shift: true, tab: true }).state.tab).toBe(0);
    // from the first tab, Shift+Tab wraps round to the last
    expect(press(onTab("look"), "", { shift: true, tab: true }).state.tab).toBe(
      5,
    );
    expect(press(onTab("status"), "", { tab: true }).state.tab).toBe(0);
    expect(press(onTab("look"), "", { tab: true }).state.tab).toBe(1);
    expect(press(onTab("look"), "3").state.tab).toBe(2);

    const look = onTab("look");

    expect(press(look, "7").state).toBe(look);
    expect(press(look, "4").state.tab).toBe(3);
    expect(press(look, "5").state.tab).toBe(4);
    expect(press(look, "6").state.tab).toBe(5);
  });

  it("a number past the last tab changes nothing, whichever tab it is pressed on (it must not wrap round)", () => {
    for (const tab of DASHBOARD_TABS) {
      const start = onTab(tab.id);

      for (const digit of ["7", "8", "9", "0"]) {
        const step = press(start, digit);

        expect(step.state, `${digit} on ${tab.id}`).toBe(start);
        expect(step.action).toEqual({ type: "none" });
      }
    }
  });

  it("previews a field live and leaves a field that waits for Enter alone", () => {
    const look = press(onTab("look"), "", { rightArrow: true });

    expect(look.action).toEqual({
      patch: expect.objectContaining({ statusBar: "bottom" }),
      type: "preview",
    });

    const focus = run(onTab("input"), ["j"], ["j"], ["l"]);

    expect(focus.state.values.focus).toBe("on");
    expect(focus.actions.at(-1)).toEqual({ type: "none" });
  });

  it("Esc closes and hands back the ui exactly as it was found", () => {
    const { state } = run(onTab("look"), ["l"], ["j"], ["l"]);
    const step = press(state, "", { escape: true });

    expect(step.action).toEqual({ revert: UI, type: "cancel" });
  });

  it("Enter on an untouched popup just closes it", () => {
    expect(press(onTab("look"), "", { return: true }).action.type).toBe(
      "cancel",
    );
  });

  it("Enter sends one change per moved key and a preview of the whole draft", () => {
    const { state } = run(onTab("input"), ["l"], ["j"], ["l"], ["j"], ["l"]);
    const step = press(state, "", { return: true });

    expect(step.action).toMatchObject({
      changes: [
        { key: "mouse", value: "buttons" },
        { key: "busy", value: "steer" },
        { key: "focus", value: "on" },
      ],
      patch: {
        busyInputMode: "steer",
        focusView: true,
        mouseTracking: "buttons",
      },
      type: "save",
    });
  });

  it("a value moved and moved back is not a change", () => {
    const { state } = run(onTab("look"), ["l"], ["h"]);

    expect(dashboardChanges(state)).toEqual([]);
    expect(press(state, "", { return: true }).action.type).toBe("cancel");
  });

  it("ignores every key while a prompt is waiting for an answer", () => {
    const start = onTab("look");

    for (const [ch, key] of [
      ["l", {}],
      ["", { escape: true }],
      ["", { return: true }],
      ["", { tab: true }],
      ["j", {}],
    ] as const) {
      const step = press(start, ch, key, { locked: true });

      expect(step.state).toBe(start);
      expect(step.action).toEqual({ type: "none" });
    }
  });

  it("ignores ctrl and meta chords and unknown characters", () => {
    const start = onTab("look");

    expect(press(start, "l", { ctrl: true }).state).toBe(start);
    expect(press(start, "l", { meta: true }).state).toBe(start);
    expect(press(start, "z").state).toBe(start);
  });
});

describe("details tab", () => {
  it("the global mode sets all four sections, and Enter re-pins every one", () => {
    const start = onTab("details");
    const step = press(start, "", { rightArrow: true });

    expect(step.state.values).toMatchObject({
      details: "expanded",
      "section.activity": "expanded",
      "section.subagents": "expanded",
      "section.thinking": "expanded",
      "section.tools": "expanded",
    });
    expect(step.action).toMatchObject({
      patch: {
        detailsMode: "expanded",
        // the config poll turns the flag off again, so the draft does not claim it (every row is pinned anyway)
        detailsModeCommandOverride: false,
        sections: {
          activity: "expanded",
          subagents: "expanded",
          thinking: "expanded",
          tools: "expanded",
        },
      },
      type: "preview",
    });
    expect(dashboardChanges(step.state)).toEqual([
      { key: "details_mode", value: "expanded" },
      { key: "details_mode.thinking", value: "expanded" },
      { key: "details_mode.tools", value: "expanded" },
      { key: "details_mode.subagents", value: "expanded" },
      { key: "details_mode.activity", value: "expanded" },
    ]);
  });

  it("one section moved alone sends only that section, and default clears its override", () => {
    const state = initDashboardState(
      { ...UI, sections: { tools: "hidden" } },
      "details",
    );
    const toolsRow = fieldsOf("details").findIndex(
      (f) => f.id === "section.tools",
    );
    const onTools = { ...state, cursor: toolsRow };
    // hidden -> collapsed -> expanded... walk back to default
    const back = press(onTools, "h").state;

    expect(back.values["section.tools"]).toBe(SECTION_DEFAULT);
    expect(dashboardChanges(back)).toEqual([
      { key: "details_mode.tools", value: "" },
    ]);
    expect(dashboardChanges(onTools)).toEqual([]);
  });

  it("a default row says what it follows", () => {
    const state = initDashboardState(UI, "details");
    const text = dashboardScreen(state, LAYOUT).map(lineText);

    // thinking and tools are expanded by default, activity hidden, sub-agents follow the global (collapsed)
    expect(text.find((l) => l.includes("Thinking"))).toContain("→ expanded");
    expect(text.find((l) => l.includes("Activity"))).toContain("→ hidden");
    expect(text.find((l) => l.includes("Sub-agents"))).toContain("→ collapsed");

    const moved = press(state, "", { rightArrow: true }).state;
    const after = dashboardScreen(moved, LAYOUT).map(lineText);

    // the global is expanded now and /details-style override applies: defaults follow it
    expect(after.find((l) => l.includes("Activity"))).not.toContain("→");
  });

  it("the preview keeps the command-override flag the ui had, even when the global moved", () => {
    const origin = { ...UI, detailsModeCommandOverride: true };
    const values = initDashboardState(origin, "details").values;

    expect(
      dashboardPatch({ ...values, "section.tools": "hidden" }, origin),
    ).toMatchObject({
      detailsModeCommandOverride: true,
      sections: { tools: "hidden" },
    });
    expect(dashboardPatch(values, UI)).toMatchObject({
      detailsModeCommandOverride: false,
    });
    expect(dashboardPatch({ ...values, details: "hidden" }, UI)).toMatchObject({
      detailsModeCommandOverride: false,
    });
  });

  it("a row put back on default says what the next config poll makes of it, not what the global mode moved to", () => {
    // collapsed -> hidden pins all four; Thinking goes back to default, whose built-in mode is expanded
    const hidden = press(onTab("details"), "", { leftArrow: true }).state;
    const step = setField(hidden, "section.thinking", SECTION_DEFAULT);
    const rows = dashboardScreen(step.state, LAYOUT).map(lineText);

    expect(rows.find((l) => l.includes("Thinking"))).toContain("→ expanded");
    expect(step.action).toEqual({
      patch: expect.objectContaining({
        detailsMode: "hidden",
        detailsModeCommandOverride: false,
        sections: { activity: "hidden", subagents: "hidden", tools: "hidden" },
      }),
      type: "preview",
    });
  });

  it("after a /details command a default row follows the global mode, as the ui does until the next poll", () => {
    const state = initDashboardState(
      { ...UI, detailsModeCommandOverride: true },
      "details",
    );
    const rows = dashboardScreen(state, LAYOUT).map(lineText);

    expect(rows.find((l) => l.includes("Thinking"))).toContain("→ collapsed");
    expect(rows.find((l) => l.includes("Activity"))).toContain("→ collapsed");
  });

  it("no section row left on default pins the invalid mode default", () => {
    const { values } = onTab("details");
    const pinned = dashboardPatch({ ...values, "section.tools": "hidden" }, UI);

    expect(pinned.sections).toEqual({ tools: "hidden" });
    // a Look preview and the save both leave every unpinned row out of the sections
    expect(dashboardPatch(values, UI).sections).toEqual({});
    expect(dashboardPatch(values, UI, false).sections).toEqual({});
    expect(press(onTab("look"), "", { rightArrow: true }).action).toEqual({
      patch: expect.objectContaining({ sections: {} }),
      type: "preview",
    });
  });
});

describe("what Esc puts back", () => {
  it("is the painted rows the draft moved, at the values the popup found", () => {
    expect(dashboardRevert(onTab("look"))).toEqual({});
    expect(
      dashboardRevert(run(onTab("look"), ["l"], ["j"], ["l"]).state),
    ).toEqual({ indicatorStyle: "kaomoji", statusBar: "top" });
    // moved and moved back: the last preview already painted the original
    expect(dashboardRevert(run(onTab("look"), ["l"], ["h"]).state)).toEqual({});
  });

  it("is the mode and the sections for a moved global details mode, never the command-override flag", () => {
    const moved = press(onTab("details"), "", { rightArrow: true }).state;

    expect(dashboardRevert(moved)).toEqual({
      detailsMode: "collapsed",
      sections: {},
    });
    expect(
      dashboardRevert({
        ...moved,
        origin: { ...moved.origin, detailsModeCommandOverride: true },
      }),
    ).not.toHaveProperty("detailsModeCommandOverride");
  });

  it("leaves a field the popup never paints alone: the Context chip, Focus view, Show thinking and the Session rows", () => {
    const focus = run(onTab("input"), ["j"], ["j"], ["l"]).state;
    const context = run(onTab("context"), ["l"]).state;
    const session = run(ready("session"), ["l"], ["j"], ["l"]).state;

    expect(focus.values.focus).toBe("on");
    expect(context.values.autocompact).toBe("50%");
    expect(session.values.model).toBe("b");

    for (const state of [focus, context, session]) {
      expect(dashboardRevert(state)).toEqual({});
    }
  });
});

describe("setField", () => {
  it("refuses an option the field does not have", () => {
    const start = onTab("look");
    const step = setField(start, "statusbar", "sideways");

    expect(step.state).toBe(start);
    expect(step.action).toEqual({ type: "none" });
    expect(setField(start, "nope", "x").state).toBe(start);
    expect(setField(start, "statusbar", "top").state).toBe(start);
  });
});

describe("dashboardScreen", () => {
  it("paints a title, the tabs, one line per row, a hint and the keys", () => {
    const text = dashboardScreen(onTab("look"), LAYOUT).map(lineText);

    expect(text[0]).toContain("Settings");
    expect(text[1]).toContain("[Look]");
    expect(text[1]).toContain(" Details ");
    expect(text.filter((l) => l.startsWith("❯ "))).toHaveLength(1);
    expect(text.find((l) => l.includes("Status bar"))).toContain("[top]");
    expect(text.at(-1)).toContain("Enter save");
    expect(text.at(-1)).toContain("Esc revert");
  });

  it("marks the cursor row with the chip and the picked chips in brackets", () => {
    const state = run(onTab("input"), ["j"], ["l"]).state;
    const lines = dashboardScreen(state, LAYOUT);
    const active = lines.filter((l) => l.active);

    expect(active).toHaveLength(1);
    expect(lineText(active[0]!)).toContain("While busy");
    expect(lineText(active[0]!)).toContain("[steer]");
    expect(lineText(active[0]!)).toContain(" queue ");
  });

  it("shows every hint whole at 80 columns: none is cut with an ellipsis", () => {
    const layout = dashboardLayout({ cols: 80, rows: 24 });

    for (const f of DASHBOARD_FIELDS) {
      const tab = DASHBOARD_TABS.find((t) => t.id === f.tab)!.id;
      const state = ready(tab);
      const row = fieldsOf(tab).indexOf(f);
      const shown = dashboardScreen({ ...state, cursor: row }, layout)
        .filter((l) => l.kind === "hint")
        .map((l) => lineText(l).trim())
        .join(" ");

      expect(shown.replace(/\s+/g, " ").trim(), f.id).toBe(f.hint);
    }
  });

  it("shows the mouse trade-off under the mouse row", () => {
    const hint = dashboardScreen(onTab("input"), LAYOUT)
      .filter((l) => l.kind === "hint")
      .map(lineText)
      .join(" ");

    expect(hint).toContain("Shift-drag");
  });

  it("does not promise native selection for wheel: it reports the mouse like all, and only off gives it back", () => {
    const mouse = DASHBOARD_FIELDS.find((f) => f.id === "mouse")!;

    // 1000 + 1006 is enough to take plain drags from the terminal; only "off" sends no mode at all
    expect(mouse.options).toEqual(["all", "buttons", "wheel", "off"]);
    expect(mouse.hint).not.toMatch(/keeps selection|still selects/);
    expect(mouse.hint).toMatch(/Shift-drag[^.]*; off gives the mouse back\./);
    expect(mouse.hint.indexOf("wheel:")).toBeLessThan(
      mouse.hint.indexOf("Shift-drag"),
    );
  });

  it("tells a locked popup to wait for the question", () => {
    const text = dashboardScreen(onTab("look"), LAYOUT, { locked: true }).map(
      lineText,
    );

    expect(text.at(-1)).toContain("Answer the question above first");
  });

  it("fits 80x24, 60x16 and 44x15 with no line wider than the popup", () => {
    for (const [cols, rows] of [
      [80, 24],
      [60, 16],
      [44, 15],
    ] as const) {
      const layout = dashboardLayout({ cols, rows });

      for (const { id: tab } of DASHBOARD_TABS) {
        const lines = dashboardScreen(ready(tab), layout);

        expect(lines.length, tab).toBeLessThanOrEqual(rows - 6);

        for (const l of lines) {
          expect(lineText(l).length).toBeLessThanOrEqual(layout.width);
        }
      }
    }
  });

  it("drops the title and the blank lines on a short terminal, keeping every row", () => {
    const roomy = dashboardScreen(
      onTab("details"),
      dashboardLayout({ cols: 100, rows: 40 }),
    );
    // 15 rows leave 9 under the chrome: the five rows of Details, its tabs, hint and keys
    const compact = dashboardScreen(
      onTab("details"),
      dashboardLayout({ cols: 100, rows: 15 }),
    );

    expect(dashboardLayout({ cols: 100, rows: 15 }).compact).toBe(true);
    expect(compact.length).toBeLessThan(roomy.length);
    expect(compact.filter((l) => l.kind === "row")).toHaveLength(5);
    expect(compact.some((l) => l.kind === "title")).toBe(false);
  });

  it("popup width follows the terminal and the grid cell", () => {
    expect(dashboardWidth(200)).toBe(78);
    expect(dashboardWidth(80)).toBe(74);
    expect(dashboardWidth(50)).toBe(44);
    expect(dashboardWidth(30)).toBe(26);
    expect(dashboardWidth(200, 60)).toBe(60);
  });
});

describe("clicks", () => {
  const lines = dashboardScreen(onTab("look"), LAYOUT);
  const rowOf = (label: string) =>
    lines.findIndex((l) => lineText(l).includes(label));
  const colOf = (label: string, chip: string) =>
    lineText(lines[rowOf(label)]!).indexOf(chip);

  it("finds the tab, the chip and the row under a cell", () => {
    expect(
      dashboardHit(lines, 1, lineText(lines[1]!).indexOf("Details") + 1),
    ).toEqual({ index: 1, type: "tab" });
    expect(
      dashboardHit(
        lines,
        rowOf("Indicator"),
        colOf("Indicator", "unicode") + 1,
      ),
    ).toEqual({ field: 1, option: "unicode", type: "chip" });
    // the label, left of the chips, is the row
    expect(dashboardHit(lines, rowOf("Indicator"), 4)).toEqual({
      field: 1,
      type: "row",
    });
    expect(dashboardHit(lines, 0, 3)).toEqual({ type: "none" });
    expect(dashboardHit(lines, 999, 3)).toEqual({ type: "none" });
    expect(dashboardHit(lines, -1, 3)).toEqual({ type: "none" });
  });

  it("a click on a tab or a row only looks around: it changes no value", () => {
    const start = onTab("look");
    const tab = dashboardClick(start, { index: 2, type: "tab" });
    const row = dashboardClick(start, { field: 2, type: "row" });

    expect(tab.state.tab).toBe(2);
    expect(tab.action).toEqual({ type: "none" });
    expect(row.state.cursor).toBe(2);
    expect(row.action).toEqual({ type: "none" });
    expect(row.state.values).toEqual(start.values);
  });

  it("a click on a chip moves to its row and picks the option", () => {
    const hit: DashboardHit = { field: 1, option: "ascii", type: "chip" };
    const step = dashboardClick(onTab("look"), hit);

    expect(step.state.cursor).toBe(1);
    expect(step.state.values.indicator).toBe("ascii");
    expect(step.action).toMatchObject({
      patch: { indicatorStyle: "ascii" },
      type: "preview",
    });
  });

  it("a click on the chip already picked still moves the cursor there", () => {
    const step = dashboardClick(onTab("look"), {
      field: 2,
      option: "off",
      type: "chip",
    });

    expect(step.state.cursor).toBe(2);
    expect(step.action).toEqual({ type: "none" });
  });

  it("a chip click on a field that waits for Enter changes the draft, not the ui", () => {
    const step = dashboardClick(onTab("input"), {
      field: 2,
      option: "on",
      type: "chip",
    });

    expect(step.state.values.focus).toBe("on");
    expect(step.action).toEqual({ type: "none" });
    expect(dashboardChanges(step.state)).toEqual([
      { key: "focus", value: "on" },
    ]);
  });

  it("a click event is read from the root Box's own corner: the painted line is localRow, the column localCol", () => {
    const start = onTab("look");
    const at = (label: string, chip: string) => ({
      localCol: lineText(lines[rowOf(label)]!).indexOf(chip) + 1,
      localRow: rowOf(label),
    });

    expect(
      dashboardClickAt(start, LAYOUT, at("Indicator", "ascii")).state.values
        .indicator,
    ).toBe("ascii");
    expect(
      dashboardClickAt(start, LAYOUT, { localCol: 14, localRow: 1 }).state.tab,
    ).toBe(1);
    // below the last line, and the title line: nothing
    expect(
      dashboardClickAt(start, LAYOUT, { localCol: 3, localRow: 99 }).state,
    ).toBe(start);
    expect(
      dashboardClickAt(start, LAYOUT, { localCol: 3, localRow: 0 }).state,
    ).toBe(start);
    expect(
      dashboardClickAt(start, LAYOUT, at("Indicator", "ascii"), {
        locked: true,
      }).state,
    ).toBe(start);
  });

  it("clicks and the wheel do nothing while a prompt is open", () => {
    const start = onTab("look");

    expect(
      dashboardClick(
        start,
        { field: 1, option: "ascii", type: "chip" },
        { locked: true },
      ).state,
    ).toBe(start);
    expect(
      dashboardClick(start, { index: 1, type: "tab" }, { locked: true }).state,
    ).toBe(start);
    expect(dashboardWheel(start, 1, { locked: true }).state).toBe(start);
  });

  it("the wheel walks the rows", () => {
    const start = onTab("look");

    expect(dashboardWheel(start, 1).state.cursor).toBe(1);
    expect(dashboardWheel(start, -1).state.cursor).toBe(0);
    expect(dashboardWheel(onTab("details"), 1).state.cursor).toBe(1);
  });

  it("every chip painted on every tab is hit at each of its columns", () => {
    for (const { id: tab } of DASHBOARD_TABS) {
      const state = ready(tab);
      const screen = dashboardScreen(state, LAYOUT);

      screen.forEach((line, row) => {
        for (const t of line.targets) {
          for (let col = t.start; col < t.end; col++) {
            const hit = dashboardHit(screen, row, col);

            expect(hit.type).toBe(t.type);
          }
        }
      });
      expect(currentFields(state).length).toBe(
        screen.filter((l) => l.kind === "row").length,
      );
    }
  });
});

describe("dashboardSummary", () => {
  it("lists the keys and values, sections together", () => {
    expect(
      dashboardSummary([
        { key: "mouse", value: "wheel" },
        { key: "details_mode.tools", value: "" },
        { key: "details_mode.activity", value: "hidden" },
      ]),
    ).toBe("display: mouse=wheel  tools=default activity=hidden");
  });

  it("leaves out a change a command applies: the command says what it did, or why it could not", () => {
    expect(
      dashboardSummary([
        { command: "/reasoning high", key: "effort", value: "high" },
        { key: "mouse", value: "wheel" },
        {
          command: "/permissions mode yolo",
          key: "permissions",
          value: "yolo",
        },
      ]),
    ).toBe("display: mouse=wheel");
  });

  it("has nothing to say when every change is a command, or there is none", () => {
    expect(dashboardSummary([])).toBe("");
    expect(
      dashboardSummary([
        { command: "/reasoning low", key: "effort", value: "low" },
        {
          command: "/permissions mode yolo",
          key: "permissions",
          value: "yolo",
        },
      ]),
    ).toBe("");
  });
});

describe("the Context tab (/autocompact)", () => {
  it("has the autocompact row and opens by name or number", () => {
    expect(DASHBOARD_TABS.map((t) => t.id)).toContain("context");
    expect(fieldsOf("context").map((f) => f.key)).toEqual(["autocompact"]);
    expect(parseDashboardTab("context")).toBe("context");
    expect(parseDashboardTab("4")).toBe("context");
  });

  it("names the saved setting with the words /autocompact takes", () => {
    for (const [context, label] of [
      [undefined, "auto"],
      [null, "auto"],
      [{}, "auto"],
      [{ autocompact: false }, "off"],
      [{ autocompact: false, compact_at_tokens: 120_000 }, "off"],
      [{ compact_at_tokens: 120_000 }, "120k"],
      [{ compact_at_tokens: 2_000_000 }, "2m"],
      [{ compact_at_tokens: 123_456 }, "123456"],
      [{ compact_at_ratio: 0.65 }, "65%"],
      [{ compact_at_ratio: 1 }, "100%"],
      [{ compact_at_ratio: 2 }, "auto"],
      [{ compact_at_tokens: "lots", compact_at_ratio: "x" }, "auto"],
    ] as const) {
      expect(autocompactLabel(context)).toBe(label);
    }
  });

  it("a limit the list does not have is a chip too, so the popup shows what is saved", () => {
    const state = initDashboardState({ ...UI, autocompact: "120k" }, "context");
    const text = dashboardScreen(state, LAYOUT).map(lineText);

    expect(state.values.autocompact).toBe("120k");
    expect(text.find((l) => l.includes("Autocompact"))).toContain("[120k]");
    expect(text.find((l) => l.includes("Autocompact"))).toContain(" off ");
    // the chip is the last one: → wraps round to auto, ← goes back to off
    expect(
      press(state, "", { rightArrow: true }).state.values.autocompact,
    ).toBe("auto");
    expect(press(state, "", { leftArrow: true }).state.values.autocompact).toBe(
      "off",
    );
    // a row whose value is in the list gets no extra chip
    expect(
      dashboardScreen(onTab("context"), LAYOUT)
        .map(lineText)
        .find((l) => l.includes("Autocompact")),
    ).not.toContain("120k");
  });

  it("is applied by its own command on Enter, never previewed and never sent as a config key", () => {
    const moved = press(onTab("context"), "", { rightArrow: true });

    expect(moved.state.values.autocompact).toBe("50%");
    expect(moved.action).toEqual({ type: "none" });

    const save = press(moved.state, "", { return: true }).action;

    expect(save).toMatchObject({
      changes: [
        { command: "/autocompact 50%", key: "autocompact", value: "50%" },
      ],
      type: "save",
    });
    // the command saves it and the config sync reads it back: the ui is not painted with it
    expect(save.type === "save" && "autocompact" in save.patch).toBe(false);
  });

  it("choosing off or auto sends exactly those words", () => {
    const off = press(onTab("context"), "", { leftArrow: true });

    expect(off.state.values.autocompact).toBe("off");
    expect(dashboardChanges(off.state)).toEqual([
      { command: "/autocompact off", key: "autocompact", value: "off" },
    ]);

    const auto = initDashboardState({ ...UI, autocompact: "70%" }, "context");

    expect(
      dashboardChanges(press(auto, "", { leftArrow: true }).state)[0]?.command,
    ).toBe("/autocompact 60%");
    expect(dashboardChanges(auto)).toEqual([]);
  });

  it("the summary leaves it to the command to say what it did", () => {
    expect(
      dashboardSummary([
        { command: "/autocompact off", key: "autocompact", value: "off" },
      ]),
    ).toBe("");
  });
});

describe("the settings.get snapshot", () => {
  it("is read from the result's settings view and its output text", () => {
    expect(
      normalizeDashboardSnapshot({
        output: "model:  a\nmode:   plan\n\n",
        settings: {
          effort: "high",
          model_key: "b",
          model_keys: ["a", "b", "a", "", 3],
          output_style: "concise",
          output_styles: ["default", "concise"],
          permission_mode: "plan",
          permission_modes: ["default", "plan"],
          theme: "dark",
        },
      }),
    ).toEqual({
      effort: "high",
      model: "b",
      modelKeys: ["a", "b"],
      outputStyle: "concise",
      outputStyles: ["default", "concise"],
      permissionMode: "plan",
      permissionModes: ["default", "plan"],
      statusLines: ["model:  a", "mode:   plan"],
      theme: "dark",
    });
  });

  it("gives a missing or odd field its default", () => {
    const bare = {
      effort: "default",
      model: "",
      modelKeys: [],
      outputStyle: "default",
      outputStyles: [],
      permissionMode: "default",
      permissionModes: [],
      statusLines: [],
      theme: "auto",
    };

    for (const raw of [
      undefined,
      null,
      "text",
      5,
      [],
      {},
      { settings: null },
      { output: 7, settings: "x" },
      { settings: { effort: 3, model_keys: "a", theme: 1 } },
    ]) {
      expect(normalizeDashboardSnapshot(raw)).toEqual(bare);
    }
  });

  it("keeps the blank lines inside the text and drops the ones after it", () => {
    expect(
      normalizeDashboardSnapshot({ output: "a\r\n\r\nb\n\n" }).statusLines,
    ).toEqual(["a", "", "b"]);
    expect(normalizeDashboardSnapshot({ output: "\n\n" }).statusLines).toEqual(
      [],
    );
  });

  it("calls a theme the backend names default, or none, auto and keeps the others", () => {
    for (const [theme, want] of [
      ["default", "auto"],
      ["", "auto"],
      [undefined, "auto"],
      ["auto", "auto"],
      ["light", "light"],
      ["dark", "dark"],
    ] as const) {
      expect(normalizeDashboardSnapshot({ settings: { theme } }).theme).toBe(
        want,
      );
    }
  });

  it("starts a popup that is still loading, with no search and the text at the top", () => {
    expect(onTab("session")).toMatchObject({
      query: "",
      searching: false,
      session: "loading",
      sessionError: "",
      statusLines: [],
      statusScroll: 0,
    });
  });
});

describe("Session tab", () => {
  it("shows no rows, only a loading line, until the snapshot arrives", () => {
    const state = onTab("session");
    const lines = dashboardScreen(state, LAYOUT);

    expect(currentFields(state)).toEqual([]);
    expect(lines.filter((l) => l.kind === "row")).toEqual([]);
    expect(lines.filter((l) => l.kind === "note").map(lineText)).toEqual([
      "loading settings…",
    ]);
    expect(lineOf(state, "tabs")).toContain("[Session]");
    expect(ids(applySnapshot(state, SNAP))).toEqual([
      "model",
      "effort",
      "permissions",
      "style",
      "theme",
      "reasoning",
    ]);
  });

  it("takes the values and the option lists from the snapshot", () => {
    const state = applySnapshot(onTab("session"), {
      ...SNAP,
      effort: "high",
      model: "b",
      outputStyle: "concise",
      permissionMode: "plan",
      theme: "dark",
    });

    expect(state.session).toBe("ready");
    expect(state.statusLines).toBe(SNAP.statusLines);
    expect(state.values).toMatchObject({
      effort: "high",
      model: "b",
      permissions: "plan",
      reasoning: "off",
      style: "concise",
      theme: "dark",
    });
    expect(state.options).toMatchObject({
      effort: ["default", "low", "medium", "high", "xhigh", "max"],
      model: ["a", "b"],
      permissions: ["default", "plan"],
      style: ["default", "concise"],
      theme: ["auto", "light", "dark"],
    });

    const text = screenText(state);

    expect(text.find((l) => l.includes("Model"))).toContain("[b]");
    expect(text.find((l) => l.includes("Model"))).toContain(" a ");
    expect(text.find((l) => l.includes("Reasoning effort"))).toContain(
      "[high]",
    );
    expect(text.find((l) => l.includes("Permission mode"))).toContain("[plan]");
    expect(text.find((l) => l.includes("Output style"))).toContain("[concise]");
    expect(text.find((l) => l.includes("Theme"))).toContain("[dark]");
    // what the popup found is not a change: Enter on it just closes
    expect(dashboardChanges(state)).toEqual([]);
    expect(press(state, "", { return: true }).action.type).toBe("cancel");
  });

  it("a current value the lists do not have becomes the last chip, and the arrows wrap through it", () => {
    const state = applySnapshot(onTab("session"), {
      ...SNAP,
      effort: "ultra",
      model: "z",
      outputStyle: "terse",
      permissionMode: "yolo",
      theme: "solarized",
    });

    expect(state.options).toMatchObject({
      effort: ["default", "low", "medium", "high", "xhigh", "max", "ultra"],
      model: ["a", "b", "z"],
      permissions: ["default", "plan", "yolo"],
      style: ["default", "concise", "terse"],
      theme: ["auto", "light", "dark", "solarized"],
    });
    expect(screenText(state).find((l) => l.includes("Model"))).toContain("[z]");
    expect(press(state, "", { rightArrow: true }).state.values.model).toBe("a");
    expect(press(state, "", { leftArrow: true }).state.values.model).toBe("b");
  });

  it("applying it keeps a Look value changed before it arrived, and touches nothing else", () => {
    const early = run(onTab("look"), ["l"], ["j"], ["l"]).state;
    const after = applySnapshot(early, SNAP);

    expect(early.values).toMatchObject({
      indicator: "unicode",
      statusbar: "bottom",
    });
    expect(after.values).toMatchObject({
      indicator: "unicode",
      statusbar: "bottom",
    });
    expect(dashboardChanges(after)).toEqual([
      { key: "statusbar", value: "bottom" },
      { key: "indicator", value: "unicode" },
    ]);
    expect(after).toMatchObject({
      cursor: early.cursor,
      origin: early.origin,
      tab: early.tab,
    });

    const sessionIds = fieldsOf("session").map((f) => f.id);

    for (const id of Object.keys(early.values)) {
      if (!sessionIds.includes(id)) {
        expect(after.values[id], id).toBe(early.values[id]);
        expect(after.base[id], id).toBe(early.base[id]);
      }
    }
  });

  it("applying it after a failure shows the rows again", () => {
    const failed = failSnapshot(onTab("session"), "no gateway");
    const after = applySnapshot(failed, SNAP);

    expect(after).toMatchObject({ session: "ready", sessionError: "" });
    expect(currentFields(after)).toHaveLength(6);
  });

  it("failing says why, on the Session and the Status tab, and shows no rows", () => {
    const failed = failSnapshot(onTab("session"), "gateway is down");

    expect(failed).toMatchObject({
      session: "failed",
      sessionError: "gateway is down",
    });
    expect(currentFields(failed)).toEqual([]);

    const notes = (tab: DashboardTabId) =>
      dashboardScreen(failSnapshot(onTab(tab), "gateway is down"), LAYOUT)
        .filter((l) => l.kind === "note")
        .map(lineText);

    expect(notes("session")).toEqual(["settings unavailable: gateway is down"]);
    expect(notes("status")).toEqual(["settings unavailable: gateway is down"]);
    expect(
      screenText(failSnapshot(onTab("session"), "")).find((l) =>
        l.startsWith("settings"),
      ),
    ).toBe("settings unavailable");
  });

  it("Show thinking starts from the ui, loaded or not, and Esc hands it back", () => {
    const on = { ...UI, showReasoning: true };

    expect(onTab("session").values.reasoning).toBe("off");
    expect(initDashboardState(on, "session").values.reasoning).toBe("on");
    expect(
      press(initDashboardState(on, "session"), "", { escape: true }).action,
    ).toEqual({ revert: on, type: "cancel" });
  });

  it("a theme the backend calls default is auto, and a pick is applied as /theme <value>", () => {
    const state = applySnapshot(
      onTab("session"),
      normalizeDashboardSnapshot({ settings: { theme: "default" } }),
    );

    expect(state.values.theme).toBe("auto");
    expect(state.options.theme).not.toContain("default");

    const dark = run({ ...state, cursor: rowOf("theme") }, ["l"], ["l"]).state;

    expect(dark.values.theme).toBe("dark");
    expect(dashboardChanges(dark)).toEqual([
      { command: "/theme dark", key: "theme", value: "dark" },
    ]);
  });

  it.each([
    ["model", ["l"], "/model b", "b"],
    ["effort", ["l", "l", "l"], "/reasoning high", "high"],
    ["permissions", ["l"], "/permissions mode plan", "plan"],
    ["style", ["l"], "/output-style concise", "concise"],
    ["reasoning", ["l"], "/reasoning show", "on"],
  ])(
    "%s is applied by its own command on Enter, never previewed",
    (id, presses, command, value) => {
      let moved: ReturnType<typeof press> = {
        action: { type: "none" },
        state: { ...ready("session"), cursor: rowOf(id) },
      };

      for (const ch of presses) {
        moved = press(moved.state, ch);
      }

      expect(moved.state.values[id]).toBe(value);
      expect(moved.action).toEqual({ type: "none" });

      const save = press(moved.state, "", { return: true }).action;

      expect(save).toMatchObject({
        changes: [{ command, key: id, value }],
        type: "save",
      });
      // the command applies it: the ui is not painted with it (nor with Show thinking)
      expect(save.type === "save" && Object.keys(save.patch)).not.toContain(
        id === "reasoning" ? "showReasoning" : id,
      );
    },
  );

  it("Show thinking off is /reasoning hide", () => {
    const state = ready("session", { ...UI, showReasoning: true });
    const off = press({ ...state, cursor: rowOf("reasoning") }, "l").state;

    expect(off.values.reasoning).toBe("off");
    expect(dashboardChanges(off)).toEqual([
      { command: "/reasoning hide", key: "reasoning", value: "off" },
    ]);
  });

  it("several changes go out in the table's order, the Look ones among them as config keys", () => {
    let state = ready("session");

    state = setField(state, "reasoning", "on").state;
    state = setField(state, "theme", "dark").state;
    state = setField(state, "model", "b").state;
    state = setField(state, "statusbar", "off").state;

    expect(dashboardChanges(state)).toEqual([
      { key: "statusbar", value: "off" },
      { command: "/model b", key: "model", value: "b" },
      { command: "/theme dark", key: "theme", value: "dark" },
      { command: "/reasoning show", key: "reasoning", value: "on" },
    ]);
    expect(dashboardSummary(dashboardChanges(state))).toBe(
      "display: statusbar=off",
    );
  });

  it("a row cannot be set while the Session tab is hidden", () => {
    const loading = onTab("session");

    for (const [id, value] of [
      ["effort", "high"],
      ["reasoning", "on"],
      ["theme", "dark"],
    ]) {
      expect(setField(loading, id, value).state).toBe(loading);
    }

    expect(dashboardChanges(loading)).toEqual([]);
  });

  it("a row with no chips or no pick still steps: onto the first going forward, the last going back", () => {
    const empty = applySnapshot(onTab("session"), {
      ...SNAP,
      model: "",
      modelKeys: [],
    });

    expect(press(empty, "", { rightArrow: true }).state).toBe(empty);
    expect(press(empty, "h").state).toBe(empty);

    const unpicked = applySnapshot(onTab("session"), { ...SNAP, model: "" });

    expect(press(unpicked, "l").state.values.model).toBe("a");
    expect(press(unpicked, "h").state.values.model).toBe("b");
  });

  it("an output style with a space in its name is quoted, so the gateway reads it as one word", () => {
    const state = applySnapshot(initDashboardState(UI, "session"), {
      ...SNAP,
      outputStyles: ["default", "concise", "my style"],
    });

    expect(
      dashboardChanges(setField(state, "style", "my style").state),
    ).toEqual([
      { command: '/output-style "my style"', key: "style", value: "my style" },
    ]);
    expect(dashboardChanges(setField(state, "style", "concise").state)).toEqual(
      [{ command: "/output-style concise", key: "style", value: "concise" }],
    );
  });

  it("a Session row hidden again by a failed answer keeps its draft but is not sent", () => {
    let state = ready("session");

    state = setField(state, "model", "b").state;
    state = setField(state, "statusbar", "off").state;
    expect(dashboardChanges(state).map((c) => c.key)).toEqual([
      "statusbar",
      "model",
    ]);

    const failed = failSnapshot(state, "gateway restarting");

    expect(dashboardChanges(failed)).toEqual([
      { key: "statusbar", value: "off" },
    ]);
    // nothing but the Look change is left: Enter saves it, the hidden /model b is not part of it
    expect(press(failed, "", { return: true }).action).toMatchObject({
      changes: [{ key: "statusbar", value: "off" }],
      type: "save",
    });
    expect(
      press(failSnapshot(ready("session"), "boom"), "", { return: true }).action
        .type,
    ).toBe("cancel");
  });

  it("a click on a chip picks it without painting anything", () => {
    const step = dashboardClick(ready("session"), {
      field: rowOf("model"),
      option: "b",
      type: "chip",
    });

    expect(step.state.values.model).toBe("b");
    expect(step.action).toEqual({ type: "none" });
  });
});

describe("search", () => {
  it("/ opens the box, and every printable key then goes into the text", () => {
    const typing = press(onTab("look"), "/");

    expect(typing.action).toEqual({ type: "none" });
    expect(typing.state.searching).toBe(true);

    // h, l, j, k, space, digits and q are text here, not commands
    const typed = typeKeys(typing.state, "hljk 12q/");

    expect(typed).toMatchObject({
      query: "hljk 12q/",
      searching: true,
      tab: 0,
    });
    expect(typed.values).toEqual(onTab("look").values);
  });

  it("does not open on the Status tab", () => {
    const status = ready("status");

    expect(press(status, "/").state).toBe(status);
  });

  it("Backspace and Delete take the last character off, and an empty box stays open", () => {
    const typing = searchFor(onTab("look"), "mou");

    expect(press(typing, "", { backspace: true }).state.query).toBe("mo");
    expect(press(typing, "", { delete: true }).state.query).toBe("mo");

    const empty = press(press(onTab("look"), "/").state, "", {
      backspace: true,
    }).state;

    expect(empty).toMatchObject({ query: "", searching: true });
  });

  it("takes pasted text whole, drops control characters, and ignores ctrl and meta chords", () => {
    const typing = press(onTab("look"), "/").state;

    expect(press(typing, "mouse").state.query).toBe("mouse");
    expect(press(typing, "mo\nuse\u007f").state.query).toBe("mouse");
    expect(press(typing, "x", { ctrl: true }).state).toBe(typing);
    expect(press(typing, "x", { meta: true }).state).toBe(typing);
    expect(press(typing, "", { leftArrow: true }).state).toBe(typing);
  });

  it("each edit puts the cursor back on the first match, and / again goes on editing the kept text", () => {
    const moved = press(kept(onTab("look"), "o"), "j").state;

    expect(moved).toMatchObject({ cursor: 1, query: "o", searching: false });

    const again = press(press(moved, "/").state, "n").state;

    expect(again).toMatchObject({ cursor: 0, query: "on", searching: true });
  });

  it("lists the rows of every tab that match, case-insensitively, in tab order", () => {
    const look = ready("look");

    // label
    expect(ids(searchFor(look, "MOUSE"))).toEqual(["mouse"]);
    // tab name
    expect(ids(searchFor(look, "input"))).toEqual(["mouse", "busy", "focus"]);
    // id
    expect(ids(searchFor(look, "section.tools"))).toEqual(["section.tools"]);
    // current value
    expect(
      ids(searchFor(ready("look", { ...UI, mouseTracking: "wheel" }), "wheel")),
    ).toEqual(["mouse"]);
    // Details comes before Session
    expect(ids(searchFor(look, "think"))).toEqual([
      "section.thinking",
      "reasoning",
    ]);
    // the spaces around the text do not count
    expect(ids(searchFor(look, "  mouse "))).toEqual(["mouse"]);
  });

  it("does not see the Session rows until the snapshot is in, and then does", () => {
    const loading = searchFor(onTab("look"), "session");

    expect(ids(loading)).toEqual([]);
    expect(ids(applySnapshot(loading, SNAP))).toEqual(
      fieldsOf("session").map((f) => f.id),
    );
    expect(ids(searchFor(onTab("look"), "think"))).toEqual([
      "section.thinking",
    ]);
  });

  it("says so when nothing matches, and the keys have nothing to act on", () => {
    const none = kept(onTab("look"), "zzz");
    const lines = dashboardScreen(none, LAYOUT);

    expect(lines.filter((l) => l.kind === "note").map(lineText)).toEqual([
      "no setting matches",
    ]);
    expect(lines.filter((l) => l.kind === "row")).toEqual([]);
    expect(press(none, "l").state).toBe(none);
    expect(press(none, "", { downArrow: true }).state).toBe(none);
    expect(dashboardChanges(none)).toEqual([]);
  });

  it("Enter keeps the text and hands the keys back; the next Enter saves", () => {
    const typing = searchFor(onTab("look"), "mouse");
    const done = press(typing, "", { return: true });

    expect(done.action).toEqual({ type: "none" });
    expect(done.state).toMatchObject({ query: "mouse", searching: false });

    // the one row listed lives on the Input tab; l changes it as it would there
    const moved = press(done.state, "l");

    expect(moved.state.values.mouse).toBe("buttons");
    expect(moved.action).toMatchObject({ type: "preview" });
    expect(press(moved.state, "", { return: true }).action).toMatchObject({
      changes: [{ key: "mouse", value: "buttons" }],
      type: "save",
    });
  });

  it("Enter on a blank text drops it", () => {
    expect(kept(onTab("look"), "  ")).toMatchObject({
      query: "",
      searching: false,
    });
  });

  it("Esc drops the search first and only then closes", () => {
    const typing = searchFor(onTab("look"), "mouse");
    const first = press(typing, "", { escape: true });

    expect(first.action).toEqual({ type: "none" });
    expect(first.state).toMatchObject({ query: "", searching: false });
    expect(press(first.state, "", { escape: true }).action).toEqual({
      revert: UI,
      type: "cancel",
    });

    // after Enter the text stays: one Esc clears it, a second closes
    const cleared = press(kept(onTab("look"), "mouse"), "", { escape: true });

    expect(cleared.action).toEqual({ type: "none" });
    expect(cleared.state.query).toBe("");
    expect(press(cleared.state, "", { escape: true }).action.type).toBe(
      "cancel",
    );
  });

  it("Esc works in the box although Ink marks every plain Esc as meta", () => {
    const typing = searchFor(onTab("look"), "mouse");
    const step = press(typing, "", { escape: true, meta: true });

    expect(step.state).toMatchObject({ query: "", searching: false });
    expect(step.action).toEqual({ type: "none" });
  });

  it("after Esc the cursor sits on the row it was on, in that row's own tab", () => {
    const back = press(kept(onTab("look"), "busy"), "", { escape: true }).state;

    expect(DASHBOARD_TABS[back.tab]!.id).toBe("input");
    expect(currentFields(back)[back.cursor]!.id).toBe("busy");

    // nothing listed: stays where it was
    expect(
      press(searchFor(onTab("details"), "zzz"), "", { escape: true }).state,
    ).toMatchObject({ cursor: 0, tab: 1 });

    // an empty box: the cursor was not moved
    const down = run(onTab("look"), ["j"], ["j"]).state;

    expect(
      press(press(down, "/").state, "", { escape: true }).state,
    ).toMatchObject({ cursor: 2, tab: 0 });
  });

  it("↑/↓ leave the box and move the cursor; Tab leaves it and changes tab", () => {
    const typing = searchFor(onTab("look"), "o");

    expect(press(typing, "", { downArrow: true }).state).toMatchObject({
      cursor: 1,
      query: "o",
      searching: false,
    });
    expect(press(typing, "", { upArrow: true }).state).toMatchObject({
      cursor: 0,
      query: "o",
      searching: false,
    });
    expect(press(typing, "", { tab: true }).state).toMatchObject({
      cursor: 0,
      query: "",
      searching: false,
      tab: 1,
    });
    expect(press(typing, "", { shift: true, tab: true }).state).toMatchObject({
      query: "",
      searching: false,
      tab: 5,
    });
  });

  it("changing tab clears the text: by Tab, a digit or a click, the tab already open included", () => {
    const filtered = kept(onTab("look"), "mouse");

    // from the tab of the row the list shows (Input), not the one the popup was opened on (Look)
    expect(press(filtered, "", { tab: true }).state).toMatchObject({
      query: "",
      tab: 3,
    });
    expect(press(filtered, "3").state).toMatchObject({ query: "", tab: 2 });
    expect(press(filtered, "1").state).toMatchObject({ query: "", tab: 0 });
    expect(
      dashboardClick(filtered, { index: 0, type: "tab" }).state,
    ).toMatchObject({ query: "", searching: false, tab: 0 });
    expect(
      dashboardClick(filtered, { index: 4, type: "tab" }).state,
    ).toMatchObject({ query: "", tab: 4 });
    // an open box with nothing in it is closed by the click too
    expect(
      dashboardClick(press(onTab("look"), "/").state, {
        index: 0,
        type: "tab",
      }).state.searching,
    ).toBe(false);
  });

  it("paints the search line under the tabs: a caret while typing, none after Enter", () => {
    const lines = (state: DashboardState) => screenText(state);

    expect(lines(searchFor(onTab("look"), "mou"))[2]).toBe("search: mou▌");
    expect(lines(kept(onTab("look"), "mou"))[2]).toBe("search: mou");
    expect(lines(press(onTab("look"), "/").state)[2]).toBe("search: ▌");
    expect(lines(onTab("look")).some((l) => l.startsWith("search"))).toBe(
      false,
    );
    // painted as live while typing, dim once Enter has ended it
    expect(
      dashboardScreen(searchFor(onTab("look"), "m"), LAYOUT)[2]!.segs[0]!.tone,
    ).toBe("accent");
    expect(
      dashboardScreen(kept(onTab("look"), "m"), LAYOUT)[2]!.segs[0]!.tone,
    ).toBe("muted");
  });

  it("a text longer than the popup shows its end, caret included", () => {
    const layout = dashboardLayout({ cols: 60, rows: 30 });
    const line = lineOf(
      searchFor(onTab("look"), "x".repeat(80)),
      "search",
      layout,
    );

    expect(line).toHaveLength(layout.width);
    expect(line.startsWith("…")).toBe(true);
    expect(line.endsWith("x▌")).toBe(true);
  });

  it("the tab bar marks no tab while a text is set, and the open tab again when there is none", () => {
    const tabs = (state: DashboardState) => lineOf(state, "tabs");

    expect(tabs(searchFor(onTab("details"), "mou"))).not.toContain("[");
    expect(tabs(kept(onTab("details"), "mou"))).not.toContain("[");
    expect(tabs(press(onTab("details"), "/").state)).toContain("[Details]");
    expect(tabs(onTab("details"))).toContain("[Details]");
  });

  it("lists each match with the tab it lives on, and its hint under the list", () => {
    const state = searchFor(onTab("look"), "mouse");
    const lines = dashboardScreen(state, LAYOUT);
    const rows = lines.filter((l) => l.kind === "row");

    expect(rows).toHaveLength(1);
    expect(lineText(rows[0]!)).toContain("Mouse");
    expect(lineText(rows[0]!)).toContain("· Input");
    expect(
      lines
        .filter((l) => l.kind === "hint")
        .map(lineText)
        .join(" "),
    ).toContain("Shift-drag");
    // a tab's own list needs no such note
    expect(lineText(dashboardScreen(onTab("input"), LAYOUT)[3]!)).not.toContain(
      "· ",
    );
  });

  it("the cursor walks the matches, and a click on a chip picks it whichever tab the row lives on", () => {
    const state = kept(onTab("look"), "off");

    expect(ids(state)).toEqual(["density", "battery", "focus"]);
    expect(press(state, "j").state.cursor).toBe(1);
    expect(dashboardWheel(state, 1).state.cursor).toBe(1);

    const lines = dashboardScreen(state, LAYOUT);
    const row = lines.findIndex((l) => l.field === 2);
    const step = dashboardClickAt(state, LAYOUT, {
      localCol: lineText(lines[row]!).indexOf(" on ") + 1,
      localRow: row,
    });

    expect(step.state.cursor).toBe(2);
    expect(step.state.values.focus).toBe("on");
    expect(step.state.values.density).toBe("off");
  });

  it("scrolls the matches to keep the cursor in view; the rows keep their index in the list", () => {
    let state = kept(ready("look"), "o");

    for (let i = 0; i < 9; i++) {
      state = press(state, "j").state;
    }

    const rows = dashboardScreen(state, L6016).filter((l) => l.kind === "row");

    expect(currentFields(state).length).toBeGreaterThan(rows.length);
    // 6 body rows, one of them the search line
    expect(rows).toHaveLength(L6016.bodyRows - 1);
    expect(rows.filter((l) => l.active).map((l) => l.field)).toEqual([9]);
    expect(rows[0]!.field).toBeGreaterThan(0);
  });

  it("keeps the cursor row in the middle of the painted matches, and the first and last window full", () => {
    const state = kept(ready("look"), "o");
    const firstRow = (cursor: number) =>
      dashboardScreen({ ...state, cursor }, L6016).find(
        (l) => l.kind === "row",
      )!.field;

    // 18 matches, 5 of them painted
    expect(currentFields(state)).toHaveLength(18);
    expect([0, 2, 3, 9, 15, 17].map(firstRow)).toEqual([0, 0, 1, 7, 13, 13]);
  });
});

// What the /settings panel's tests pinned, now that /settings opens this popup on its Session tab.
describe("search, once rows change under it", () => {
  it("a row found by its value stays in the list when that value changes: the next → changes the same row", () => {
    let state = kept(ready("look"), "off");
    const found = ["density", "battery", "focus", "reasoning"];

    expect(ids(state)).toEqual(found);

    state = press(state, "", { rightArrow: true }).state;
    expect(ids(state)).toEqual(found);
    expect(state.cursor).toBe(0);

    state = press(state, "", { rightArrow: true }).state;
    expect(state.values).toMatchObject({ battery: "off", density: "off" });
    expect(state.cursor).toBe(0);
  });

  it("the last row found can be changed and the cursor stays on it", () => {
    const last = run(
      kept(ready("look"), "off"),
      ["j"],
      ["j"],
      ["j"],
      ["l"],
    ).state;

    expect(last.values.reasoning).toBe("on");
    expect(currentFields(last)[last.cursor]?.id).toBe("reasoning");
    expect(
      lineText(dashboardScreen(last, LAYOUT).find((l) => l.active)!),
    ).toContain("Show thinking");
  });

  it("a click on a chip of a row found by its value leaves the cursor on that row", () => {
    const state = kept(ready("look"), "off");
    const step = dashboardClick(state, {
      field: 1,
      option: "on",
      type: "chip",
    });

    expect(step.state.values.battery).toBe("on");
    expect(ids(step.state)).toEqual(ids(state));
    expect(step.state.cursor).toBe(1);
  });

  it("what the search matches is the value the popup found, not the one picked since", () => {
    const state = setField(
      ready("look", { ...UI, mouseTracking: "wheel" }),
      "mouse",
      "off",
    ).state;

    expect(ids(searchFor(state, "wheel"))).toEqual(["mouse"]);
    expect(ids(searchFor(state, "off"))).not.toContain("mouse");
  });

  it("a cursor on a row the snapshot took away is pulled back into the list", () => {
    const state = run(
      kept(ready("session"), "session"),
      ["j"],
      ["j"],
      ["j"],
    ).state;

    expect(state.cursor).toBe(3);
    expect(failSnapshot(state, "gateway restarting").cursor).toBe(0);
    expect(currentFields(failSnapshot(state, "gateway restarting"))).toEqual(
      [],
    );
  });

  it("finds the rows by the heading the /settings panel filed them under", () => {
    const look = ready("look");
    const filed: [string, string][] = [
      ["Model", "model"],
      ["Model", "effort"],
      ["Model", "permissions"],
      ["Model", "style"],
      ["Display", "theme"],
      ["Display", "reasoning"],
      ["Display", "details"],
      ["Display", "density"],
      ["Display", "statusbar"],
      ["Display", "indicator"],
      ["Input", "busy"],
      ["Input", "mouse"],
    ];

    for (const [heading, id] of filed) {
      expect(ids(searchFor(look, heading.toLowerCase())), heading).toContain(
        id,
      );
    }

    // the headings group the rows: Display is not a tab, and does not list the Input rows
    expect(ids(searchFor(look, "display"))).not.toContain("mouse");
    expect(ids(searchFor(look, "display"))).toContain("section.tools");
    // the old panel had no Autocompact row: a search for a heading does not lead to it
    for (const heading of ["model", "display", "input"]) {
      expect(ids(searchFor(look, heading)), heading).not.toContain(
        "autocompact",
      );
    }
  });

  it("Esc in the box puts the popup back as it was: the tab and the row it was on", () => {
    const down = run(onTab("look"), ["j"], ["j"]).state;
    const back = press(typeKeys(press(down, "/").state, "mouse"), "", {
      escape: true,
    });

    expect(back.action).toEqual({ type: "none" });
    expect(back.state).toMatchObject({
      cursor: 2,
      query: "",
      searching: false,
      tab: 0,
    });

    // a text kept with Enter and opened again is a kept filter: Esc clears it and lands on the row's own tab
    const again = press(press(kept(onTab("look"), "busy"), "/").state, "", {
      escape: true,
    }).state;

    expect(DASHBOARD_TABS[again.tab]!.id).toBe("input");
    expect(currentFields(again)[again.cursor]!.id).toBe("busy");
  });

  it("Tab and Shift+Tab go on from the tab of the row the list shows", () => {
    const filtered = kept(ready("session"), "mouse");

    expect(press(filtered, "", { tab: true }).state.tab).toBe(3);
    expect(press(filtered, "", { shift: true, tab: true }).state.tab).toBe(1);
  });

  it("a blank text is dropped by ↑/↓ and Tab as Enter drops it, so the next Esc closes", () => {
    const typing = searchFor(onTab("look"), "  ");

    for (const key of [{ downArrow: true }, { upArrow: true }, { tab: true }]) {
      expect(press(typing, "", key).state, JSON.stringify(key)).toMatchObject({
        query: "",
        searching: false,
      });
    }

    const down = press(typing, "", { downArrow: true }).state;

    expect(press(down, "", { escape: true }).action.type).toBe("cancel");
  });

  it("Backspace takes an emoji whole, not half of its two UTF-16 units", () => {
    const typing = typeKeys(press(onTab("look"), "/").state, "a😀");

    expect(typing.query).toBe("a😀");
    expect(press(typing, "", { backspace: true }).state.query).toBe("a");
    expect(press(typing, "", { delete: true }).state.query).toBe("a");
  });

  it("a text too long for the popup is cut between characters, never inside an emoji", () => {
    const line = lineText(
      dashboardScreen(
        searchFor(onTab("look"), "😀".repeat(LAYOUT.width)),
        LAYOUT,
      ).find((l) => l.kind === "search")!,
    );

    expect(line.startsWith("…")).toBe(true);
    expect(line).not.toMatch(/[\ud800-\udbff](?![\udc00-\udfff])/);
    expect(line).not.toMatch(/(?<![\ud800-\udbff])[\udc00-\udfff]/);
  });

  it("PgUp/PgDn, Home/End and g/G walk the rows of a long result", () => {
    const state = kept(ready("look"), "e");
    const rows = ids(state).length;

    expect(rows).toBeGreaterThan(8);
    expect(press(state, "G").state.cursor).toBe(rows - 1);
    expect(press(state, "", { end: true }).state.cursor).toBe(rows - 1);
    expect(
      press(state, "", { pageDown: true }, { bodyRows: 5 }).state.cursor,
    ).toBe(5);
    expect(
      press(press(state, "G").state, "", { pageUp: true }, { bodyRows: 5 })
        .state.cursor,
    ).toBe(rows - 6);
    expect(press(press(state, "G").state, "g").state.cursor).toBe(0);
  });
});

describe("chords that are not the plain keys", () => {
  it("Alt/Option and Ctrl with an arrow, Tab or Enter do nothing: Option+← is a word jump, not a ←", () => {
    const look = ready("look");

    for (const key of [
      { meta: true, rightArrow: true },
      { ctrl: true, rightArrow: true },
      { meta: true, leftArrow: true },
      { ctrl: true, leftArrow: true },
      { meta: true, downArrow: true },
      { meta: true, upArrow: true },
      { meta: true, tab: true },
      { ctrl: true, tab: true },
      { meta: true, return: true },
      { ctrl: true, return: true },
    ]) {
      const step = press(look, "", key);

      expect(step.state, JSON.stringify(key)).toBe(look);
      expect(step.action, JSON.stringify(key)).toEqual({ type: "none" });
    }

    // a draft is not saved by Alt+Enter
    const changed = setField(look, "density", "on").state;

    expect(press(changed, "", { meta: true, return: true }).action).toEqual({
      type: "none",
    });
    expect(press(changed, "", { return: true }).action.type).toBe("save");
  });

  it("do nothing in the search box either, and Esc still works there although Ink marks it meta", () => {
    const typing = searchFor(onTab("look"), "mo");

    for (const key of [
      { meta: true, downArrow: true },
      { ctrl: true, tab: true },
      { meta: true, return: true },
      { meta: true, backspace: true },
    ]) {
      expect(press(typing, "", key).state, JSON.stringify(key)).toBe(typing);
    }

    expect(
      press(typing, "", { escape: true, meta: true }).state.searching,
    ).toBe(false);
    expect(
      press(onTab("look"), "", { escape: true, meta: true }).action.type,
    ).toBe("cancel");
  });
});

describe("what the /settings panel did, on the Session tab", () => {
  it("the cursor starts on Model and ↑/↓ walk the six rows (the old panel wrapped at the ends; the dashboard stops)", () => {
    const start = ready("session");

    expect(start.cursor).toBe(rowOf("model"));
    expect(press(start, "", { upArrow: true }).state.cursor).toBe(0);

    const down = run(start, ...Array(8).fill(["", { downArrow: true }]));

    expect(down.state.cursor).toBe(5);
    expect(run(down.state, ["", { upArrow: true }], ["k"]).state.cursor).toBe(
      3,
    );
  });

  it("→ steps the first row and Enter hands its slash command over", () => {
    const step = press(ready("session"), "", { rightArrow: true });

    expect(step.state.values.model).toBe("b");
    expect(step.action).toEqual({ type: "none" });
    expect(press(step.state, "", { return: true }).action).toMatchObject({
      changes: [{ command: "/model b", key: "model", value: "b" }],
      type: "save",
    });
  });

  it("/ finds a row, Enter keeps it, and flipping a toggle there is saved like on its own tab", () => {
    // Show thinking: the old panel's on/off row
    const think = kept(ready("session"), "show");

    expect(ids(think)).toEqual(["reasoning"]);

    const on = press(think, "l");

    expect(on.state.values.reasoning).toBe("on");
    expect(press(on.state, "", { return: true }).action).toMatchObject({
      changes: [{ command: "/reasoning show", key: "reasoning", value: "on" }],
      type: "save",
    });

    // Mouse is not on/off here: ← from `all` wraps to the `off` preset
    const mouse = kept(ready("session"), "mous");

    expect(ids(mouse)).toEqual(["mouse"]);

    const off = press(mouse, "", { leftArrow: true });

    expect(off.state.values.mouse).toBe("off");
    expect(off.action).toMatchObject({
      patch: { mouseTracking: "off" },
      type: "preview",
    });
    expect(press(off.state, "", { return: true }).action).toMatchObject({
      changes: [{ key: "mouse", value: "off" }],
      type: "save",
    });
  });

  it("Esc closes, Tab goes on to Status, and a prompt that is open ignores every key", () => {
    const start = ready("session");

    expect(press(start, "", { escape: true }).action).toEqual({
      revert: UI,
      type: "cancel",
    });
    expect(press(start, "", { tab: true }).state.tab).toBe(5);

    for (const [ch, key] of [
      ["q", {}],
      ["l", {}],
      ["", { escape: true }],
      ["", { return: true }],
      ["", { tab: true }],
    ] as const) {
      const step = press(start, ch, key, { locked: true });

      expect(step.state).toBe(start);
      expect(step.action).toEqual({ type: "none" });
    }
  });
});

describe("q", () => {
  it("closes like Esc and hands back the ui as it was found", () => {
    const moved = run(onTab("look"), ["l"]).state;

    expect(press(moved, "q").action).toEqual({ revert: UI, type: "cancel" });
  });

  it("closes from every tab and with a text kept, but is only text in the search box", () => {
    for (const { id } of DASHBOARD_TABS) {
      expect(press(ready(id), "q").action.type, id).toBe("cancel");
    }

    expect(press(kept(onTab("look"), "mouse"), "q").action.type).toBe("cancel");

    const typed = press(press(onTab("look"), "/").state, "q");

    expect(typed.action).toEqual({ type: "none" });
    expect(typed.state.query).toBe("q");
  });

  it("is not Ctrl+Q", () => {
    const start = onTab("look");

    expect(press(start, "q", { ctrl: true }).state).toBe(start);
    expect(press(start, "q", { ctrl: true }).action.type).toBe("none");
  });
});

describe("a prompt is open", () => {
  it("every key and the wheel are ignored, the search box and the Status text included", () => {
    const typing = press(onTab("look"), "/").state;
    const status = ready("status");
    const rows = { bodyRows: 10, locked: true };

    for (const [state, ch, key] of [
      [typing, "m", {}],
      [typing, "", { escape: true }],
      [typing, "", { return: true }],
      [typing, "", { backspace: true }],
      [typing, "", { downArrow: true }],
      [onTab("look"), "/", {}],
      [onTab("look"), "q", {}],
      [status, "", { downArrow: true }],
      [status, "j", {}],
      [status, "G", {}],
      [status, "", { pageDown: true }],
      [status, "", { end: true }],
    ] as const) {
      const step = press(state, ch, key, rows);

      expect(step.state).toBe(state);
      expect(step.action).toEqual({ type: "none" });
    }

    expect(dashboardWheel(status, 1, rows).state).toBe(status);
  });
});

describe("Status tab", () => {
  const status = ready("status");
  const rows = { bodyRows: 10 };
  const scroll = (
    state: DashboardState,
    ch: string,
    key: Parameters<typeof dashboardKey>[2] = {},
    opts = rows,
  ) => press(state, ch, key, opts).state.statusScroll;

  it("has no rows to move over or change", () => {
    expect(currentFields(status)).toEqual([]);

    for (const ch of ["h", "l", " ", "/"]) {
      expect(press(status, ch).state).toBe(status);
    }
  });

  it("↑/↓ and j/k scroll by one line and stop at both ends", () => {
    const two = { ...status, statusScroll: 2 };

    expect(scroll(status, "", { downArrow: true })).toBe(1);
    expect(scroll(status, "j")).toBe(1);
    expect(scroll(two, "", { upArrow: true })).toBe(1);
    expect(scroll(two, "k")).toBe(1);
    expect(press(status, "k", {}, rows).state).toBe(status);
    expect(press(status, "", { upArrow: true }, rows).state).toBe(status);

    // 30 lines in a window of 10: the last window starts at line 20
    const end = { ...status, statusScroll: 20 };

    expect(press(end, "j", {}, rows).state).toBe(end);
    expect(press(end, "", { downArrow: true }, rows).state).toBe(end);
  });

  it("PgUp/PgDn move a window's worth, Home/g and End/G jump, and none goes past the text", () => {
    const at = (n: number) => ({ ...status, statusScroll: n });

    expect(scroll(status, "", { pageDown: true })).toBe(10);
    expect(scroll(at(10), "", { pageDown: true })).toBe(20);
    expect(scroll(at(15), "", { pageDown: true })).toBe(20);
    expect(scroll(at(15), "", { pageUp: true })).toBe(5);
    expect(scroll(at(5), "", { pageUp: true })).toBe(0);
    expect(scroll(status, "", { pageDown: true }, { bodyRows: 4 })).toBe(4);
    expect(scroll(at(7), "", { home: true })).toBe(0);
    expect(scroll(at(7), "g")).toBe(0);
    expect(scroll(at(7), "", { end: true })).toBe(20);
    expect(scroll(at(7), "G")).toBe(20);
  });

  it("scrolls a window of 8 lines when the caller does not say", () => {
    expect(press(status, "", { pageDown: true }).state.statusScroll).toBe(8);
    expect(press(status, "G").state.statusScroll).toBe(22);
    // and a window of no rows is one row
    expect(
      press(status, "", { pageDown: true }, { bodyRows: 0 }).state.statusScroll,
    ).toBe(1);
  });

  it("text that fits the window does not scroll", () => {
    const short = { ...status, statusLines: ["a", "b", "c"] };

    for (const [ch, key] of [
      ["j", {}],
      ["", { downArrow: true }],
      ["", { pageDown: true }],
      ["", { end: true }],
      ["G", {}],
    ] as const) {
      expect(press(short, ch, key, rows).state).toBe(short);
    }
  });

  it("the wheel scrolls it a line a notch, and still walks the rows on the other tabs", () => {
    expect(dashboardWheel(status, 1, rows).state.statusScroll).toBe(1);
    expect(
      dashboardWheel({ ...status, statusScroll: 3 }, -1, rows).state
        .statusScroll,
    ).toBe(2);
    expect(dashboardWheel(status, -1, rows).state).toBe(status);
    expect(dashboardWheel(onTab("look"), 1, rows).state.cursor).toBe(1);
  });

  it("g, G, PgUp/PgDn and Home/End jump over the rows of a list tab and leave the Status text alone", () => {
    // the text was scrolled before the tab changed
    const look = { ...ready("look"), statusScroll: 5 };
    const cursorAfter = (from: DashboardState, ch: string, key = {}) =>
      press(from, ch, key, rows).state.cursor;
    const last = fieldsOf("look").length - 1;

    expect(cursorAfter(look, "G")).toBe(last);
    expect(cursorAfter(look, "", { end: true })).toBe(last);
    expect(cursorAfter({ ...look, cursor: last }, "g")).toBe(0);
    expect(cursorAfter({ ...look, cursor: last }, "", { home: true })).toBe(0);
    // a page is the rows the window paints (10 here), and the cursor stops at the ends
    expect(cursorAfter(look, "", { pageDown: true })).toBe(last);
    expect(cursorAfter({ ...look, cursor: last }, "", { pageUp: true })).toBe(
      0,
    );
    expect(press(look, "G", {}, rows).state.statusScroll).toBe(5);
  });

  it("paints the window the scroll points at and where it is in the text", () => {
    const at = { ...status, statusScroll: 4 };
    const lines = dashboardScreen(at, L8024);
    const text = lines.filter((l) => l.kind === "text").map(lineText);

    expect(L8024.bodyRows).toBe(11);
    expect(text).toHaveLength(11);
    expect(text[0]).toBe("status line 5");
    expect(text.at(-1)).toBe("status line 15");
    expect(lines.find((l) => l.kind === "hint")).toBeDefined();
    expect(lineText(lines.find((l) => l.kind === "hint")!)).toBe(
      "lines 5-15 of 30",
    );
    expect(lineText(lines.at(-1)!)).toContain("scroll");
    expect(lineOf(at, "tabs")).toContain("[Status]");
  });

  it("shows no position when the whole text fits", () => {
    const short = { ...status, statusLines: ["a", "b"] };
    const lines = dashboardScreen(short, L8024);

    expect(lines.filter((l) => l.kind === "text").map(lineText)).toEqual([
      "a",
      "b",
    ]);
    expect(lines.filter((l) => l.kind === "hint").map(lineText)).toEqual([
      " ",
      " ",
    ]);
  });

  it("holds a scroll that is too far for the window inside the text, and scrolls on from there", () => {
    const stale = { ...status, statusScroll: 29 };
    const text = dashboardScreen(stale, L8024)
      .filter((l) => l.kind === "text")
      .map(lineText);

    expect(text[0]).toBe("status line 20");
    expect(text.at(-1)).toBe("status line 30");
    expect(
      press(stale, "", { upArrow: true }, { bodyRows: 11 }).state.statusScroll,
    ).toBe(18);
  });

  it("paints a blank line as a space and cuts a long line to the popup", () => {
    const odd = { ...status, statusLines: ["", "x".repeat(200)] };
    const text = dashboardScreen(odd, L8024)
      .filter((l) => l.kind === "text")
      .map(lineText);

    expect(text[0]).toBe(" ");
    expect(text[1]).toHaveLength(L8024.width);
    expect(text[1]!.endsWith("…")).toBe(true);
  });

  it("says it is loading, or why it failed, instead of the text; and says when there is none", () => {
    const notes = (state: DashboardState) =>
      dashboardScreen(state, LAYOUT)
        .filter((l) => l.kind === "note" || l.kind === "text")
        .map(lineText);

    expect(notes(onTab("status"))).toEqual(["loading…"]);
    expect(notes(ready("status"))[0]).toBe("status line 1");
    expect(
      notes(applySnapshot(onTab("status"), { ...SNAP, statusLines: [] })),
    ).toEqual(["no status text"]);
  });

  it("after a text came and a later answer failed it paints the reason alone, within the row budget", () => {
    const failed = failSnapshot(status, "gateway restarting");
    const lines = dashboardScreen(failed, L8024);
    const text = lines.map(lineText);

    expect(text).toContain("settings unavailable: gateway restarting");
    expect(text.join("\n")).not.toContain("status line");
    expect(lines.filter((l) => l.kind === "text")).toEqual([]);
    expect(lines.filter((l) => l.kind === "hint").map(lineText)).toEqual([
      " ",
      " ",
    ]);
    expect(lines.length).toBeLessThanOrEqual(24 - DASHBOARD_OVERLAY_CHROME);
  });

  it("Enter closes it, or saves what was changed on another tab", () => {
    expect(press(status, "", { return: true }).action.type).toBe("cancel");

    const changed = press(run(onTab("look"), ["l"]).state, "6").state;

    expect(changed.tab).toBe(5);
    expect(press(changed, "", { return: true }).action).toMatchObject({
      changes: [{ key: "statusbar", value: "bottom" }],
      type: "save",
    });
  });
});

describe("layout", () => {
  it("leaves the body the rows the fixed lines do not use", () => {
    expect(dashboardLayout({ cols: 80, rows: 24 })).toMatchObject({
      bodyRows: 11,
      compact: false,
    });
    expect(dashboardLayout({ cols: 60, rows: 16 })).toMatchObject({
      bodyRows: 6,
      compact: true,
    });
    expect(dashboardLayout({ cols: 44, rows: 15 }).bodyRows).toBe(5);
    expect(dashboardLayout({ cols: 80, rows: 3 }).bodyRows).toBe(1);
  });

  it("goes roomy when the tallest tab, Session's six rows, fits with the title and the blanks", () => {
    // 13 rows under the chrome: title, tabs, blank, 6 rows, blank, 2 hint lines, keys
    expect(
      dashboardLayout({ cols: 80, rows: 12 + DASHBOARD_OVERLAY_CHROME }),
    ).toMatchObject({ bodyRows: 8, compact: true });
    expect(
      dashboardLayout({ cols: 80, rows: 13 + DASHBOARD_OVERLAY_CHROME }),
    ).toMatchObject({ bodyRows: 6, compact: false });
  });

  it("fits every screen under the chrome at 80x24, 60x16 and 44x15, none line wider than the popup", () => {
    const long = {
      ...SNAP,
      statusLines: Array.from(
        { length: 200 },
        (_, i) => `status line ${i} ${"x".repeat(i)}`,
      ),
    };
    const loaded = (tab: DashboardTabId) =>
      applySnapshot(initDashboardState(UI, tab), long);
    const states: [string, DashboardState][] = [
      ["status", loaded("status")],
      ["status end", press(loaded("status"), "G").state],
      ["status loading", onTab("status")],
      ["session", loaded("session")],
      ["session loading", onTab("session")],
      ["session failed", failSnapshot(onTab("session"), "boom ".repeat(30))],
      ["empty box", press(loaded("look"), "/").state],
      ["many matches", searchFor(loaded("look"), "o")],
      ["no match", searchFor(loaded("look"), "zzz")],
      ["long text", searchFor(loaded("look"), "x".repeat(90))],
      [
        "matches, cursor at the end",
        press(kept(loaded("look"), "o"), "j").state,
      ],
    ];

    for (const [cols, rows] of [
      [80, 24],
      [60, 16],
      [44, 15],
    ] as const) {
      const layout = dashboardLayout({ cols, rows });

      for (const [name, state] of states) {
        for (const locked of [false, true]) {
          const lines = dashboardScreen(state, layout, { locked });

          expect(
            lines.length,
            `${name} at ${cols}x${rows}`,
          ).toBeLessThanOrEqual(rows - DASHBOARD_OVERLAY_CHROME);

          for (const l of lines) {
            expect(lineText(l).length).toBeLessThanOrEqual(layout.width);
          }
        }
      }

      // a long text fills the window, not less and not more
      expect(
        dashboardScreen(loaded("status"), layout).filter(
          (l) => l.kind === "text",
        ),
      ).toHaveLength(layout.bodyRows);
    }
  });

  it("keeps the picked chip in view when the chips are wider than the popup", () => {
    const keys = Array.from({ length: 9 }, (_, i) => `model-${i + 1}`);
    const row = (model: string) =>
      dashboardScreen(
        applySnapshot(onTab("session"), { ...SNAP, model, modelKeys: keys }),
        L8024,
      ).find((l) => l.kind === "row")!;
    const last = row("model-9");
    const first = row("model-1");

    expect(lineText(last)).toContain("[model-9]");
    expect(lineText(last)).toContain("… ");
    expect(lineText(last).endsWith("…")).toBe(false);
    expect(lineText(last).length).toBeLessThanOrEqual(L8024.width);
    expect(last.targets.at(-1)).toMatchObject({ option: "model-9" });
    expect(lineText(first)).toContain("[model-1]");
    expect(lineText(first).endsWith(" …")).toBe(true);
    expect(lineText(first)).not.toContain("… ");
    expect(lineText(first).length).toBeLessThanOrEqual(L8024.width);
    expect(first.targets[0]).toMatchObject({ option: "model-1" });
  });

  it("scrolls the tab bar to keep the open tab in view on a narrow popup", () => {
    for (const cols of [44, 50, 60, 100]) {
      const layout = dashboardLayout({ cols, rows: 40 });

      for (const { id, label } of DASHBOARD_TABS) {
        const bar = lineOf(ready(id), "tabs", layout);

        expect(bar, `${label} at ${cols}`).toContain(`[${label}]`);
        expect(bar.length).toBeLessThanOrEqual(layout.width);
      }
    }

    const narrow = dashboardLayout({ cols: 44, rows: 40 });

    // a cut end of the bar says so
    expect(lineOf(onTab("look"), "tabs", narrow)).toMatch(/ …$/);
    expect(lineOf(onTab("look"), "tabs", narrow)).not.toMatch(/^ … /);
    expect(lineOf(onTab("status"), "tabs", narrow)).toMatch(/^ … /);
    expect(lineOf(onTab("status"), "tabs", narrow)).not.toMatch(/ …$/);

    const lines = dashboardScreen(onTab("status"), narrow);
    const row = lines.findIndex((l) => l.kind === "tabs");

    // every painted tab is hit at each of its columns
    for (const t of lines[row]!.targets) {
      expect(
        dashboardClickAt(onTab("status"), narrow, {
          localCol: t.start,
          localRow: row,
        }).state.tab,
      ).toBe(t.type === "tab" ? t.index : -1);
    }
  });

  it("the keys line follows the mode and still fits a narrow popup", () => {
    const keys = (state: DashboardState, layout = LAYOUT) =>
      lineText(dashboardScreen(state, layout).at(-1)!);
    const narrow = dashboardLayout({ cols: 44, rows: 40 });

    expect(keys(onTab("look"))).toContain("/ search");
    expect(keys(press(onTab("look"), "/").state)).toContain("type to filter");
    expect(keys(kept(onTab("look"), "mouse"))).toContain("Esc clear");
    expect(keys(ready("status"))).toContain("scroll");

    for (const state of [
      onTab("look"),
      press(onTab("look"), "/").state,
      kept(onTab("look"), "mouse"),
      ready("status"),
    ]) {
      expect(keys(state, narrow).length).toBeLessThanOrEqual(narrow.width);
      expect(keys(state, narrow)).toContain("Esc");
    }
  });
});
