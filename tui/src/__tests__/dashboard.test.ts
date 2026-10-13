import { describe, expect, it } from "vitest";

import {
  autocompactLabel,
  currentFields,
  DASHBOARD_FIELDS,
  DASHBOARD_TABS,
  dashboardChanges,
  dashboardClick,
  dashboardClickAt,
  type DashboardHit,
  dashboardHit,
  dashboardKey,
  dashboardLayout,
  dashboardPatch,
  dashboardScreen,
  type DashboardState,
  dashboardSummary,
  type DashboardUi,
  dashboardWheel,
  dashboardWidth,
  fieldsOf,
  initDashboardState,
  lineText,
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
  statusBar: "top",
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

const onTab = (tab: "context" | "details" | "input" | "look") =>
  initDashboardState(UI, tab);

describe("dashboard fields", () => {
  it("lists look, details, input and context rows, each with distinct ids and keys", () => {
    expect(DASHBOARD_TABS.map((t) => t.id)).toEqual([
      "look",
      "details",
      "input",
      "context",
    ]);
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

    for (const f of DASHBOARD_FIELDS) {
      expect(f.options).toContain(state.values[f.id]);
    }
  });

  it("opens on the tab it is asked for and knows tabs by name or number", () => {
    expect(onTab("input").tab).toBe(2);
    expect(parseDashboardTab("Details")).toBe("details");
    expect(parseDashboardTab(" 3 ")).toBe("input");
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
      3,
    );
    expect(press(onTab("look"), "", { tab: true }).state.tab).toBe(1);
    expect(press(onTab("look"), "3").state.tab).toBe(2);

    const look = onTab("look");

    expect(press(look, "5").state).toBe(look);
    expect(press(look, "4").state.tab).toBe(3);
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
        detailsModeCommandOverride: true,
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

  it("the preview keeps the command-override flag the ui had unless the global moved", () => {
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

    expect(text[0]).toContain("Display");
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
      const state = initDashboardState(UI, tab);
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

      for (const tab of ["look", "details", "input"] as const) {
        const lines = dashboardScreen(initDashboardState(UI, tab), layout);

        expect(lines.length).toBeLessThanOrEqual(rows - 6);

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
    const compact = dashboardScreen(
      onTab("details"),
      dashboardLayout({ cols: 100, rows: 14 }),
    );

    expect(dashboardLayout({ cols: 100, rows: 14 }).compact).toBe(true);
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
    for (const tab of ["look", "details", "input"] as const) {
      const state = initDashboardState(UI, tab);
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
    expect(dashboardSummary([])).toBe("display: no changes");
    expect(
      dashboardSummary([
        { key: "mouse", value: "wheel" },
        { key: "details_mode.tools", value: "" },
        { key: "details_mode.activity", value: "hidden" },
      ]),
    ).toBe("display: mouse=wheel  tools=default activity=hidden");
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

  it("the summary names it like any other key", () => {
    expect(
      dashboardSummary([
        { command: "/autocompact off", key: "autocompact", value: "off" },
      ]),
    ).toBe("display: autocompact=off");
  });
});
