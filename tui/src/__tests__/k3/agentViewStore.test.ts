import { describe, expect, it } from "vitest";

import {
  buildLines,
  buildViewRows,
  IDLE_VIEW_NAV,
  navItems,
  type PastSessionRow,
  reduceViewKey,
  selectedIndex,
  toggleGroup,
  type ViewKey,
  type ViewNav,
  type ViewRow,
  visibleWindow,
} from "../../k3/agentViewStore.js";
import { buildStripRows, GLYPH } from "../../k3/agentStrip.js";
import type { SessionActiveItem } from "../../gatewayTypes.js";
import type { SubagentProgress } from "../../types.js";

const NOW = 2_000_000_000_000;
const CWD = "/work/proj";

const agent = (
  id: string,
  status: SubagentProgress["status"] = "running",
): SubagentProgress => ({
  depth: 0,
  goal: `goal ${id}`,
  id,
  index: 0,
  notes: [],
  parentId: null,
  startedAt: NOW - 10_000,
  status,
  taskCount: 1,
  thinking: [],
  toolCount: 0,
  tools: [],
});

const past = (
  id: string,
  started_at: number,
  cwd: null | string = CWD,
): PastSessionRow => ({
  id,
  started_at,
  title: `past ${id}`,
  ...(cwd === null ? {} : { cwd }),
});

const build = (
  opts: Partial<Parameters<typeof buildViewRows>[0]> = {},
): ViewRow[] =>
  buildViewRows({
    currentCwd: CWD,
    currentSid: "cur",
    nowMs: NOW,
    past: [],
    sessions: [],
    subagents: [],
    ...opts,
  });

describe("buildViewRows", () => {
  const sessions: SessionActiveItem[] = [
    { id: "w1", model: "model-a", status: "working", title: "W1" },
    { id: "d1", status: "idle", title: "D1" },
    { current: true, id: "cur", status: "working", title: "Here" },
    { id: "i1", last_active: 123, status: "waiting", title: "I1" },
    { id: "f1", status: "failed", title: "F1" },
  ];

  it("orders groups input, working, completed, keeps the strip order inside a group and ends with past rows", () => {
    const rows = build({
      past: [past("p1", 10)],
      sessions,
      subagents: [agent("a1"), agent("a2", "completed")],
    });

    expect(rows.map((r) => [r.id, r.group])).toEqual([
      ["i1", "input"],
      ["cur", "working"],
      ["w1", "working"],
      ["a1", "working"],
      ["f1", "completed"],
      ["d1", "completed"],
      ["a2", "completed"],
      ["p1", "completed"],
    ]);
    expect(rows.at(-1)).toMatchObject({ kind: "past", state: "done" });
  });

  it("includes the current session once, flagged, found by `current` or by id", () => {
    const rows = build({ sessions });
    const cur = rows.filter((r) => r.current);

    expect(cur.map((r) => r.id)).toEqual(["cur"]);
    expect(cur[0]).toMatchObject({ kind: "session", state: "working" });
    expect(rows.filter((r) => r.id === "cur")).toHaveLength(1);

    const byId = build({
      currentSid: "x",
      sessions: [{ id: "x", status: "waiting", title: "X" }],
    });

    expect(byId).toHaveLength(1);
    expect(byId[0]).toMatchObject({ current: true, group: "input", id: "x" });
  });

  it("shows the current session's last turn as failed (✗) or completed (✓), leading the Completed group", () => {
    for (const [status, state] of [
      ["failed", "failed"],
      ["completed", "done"],
    ] as const) {
      const rows = build({
        sessions: [
          { id: "w1", status: "working", title: "W1" },
          { id: "f1", status: "failed", title: "F1" },
          { current: true, id: "cur", status, title: "Here" },
        ],
      });
      const cur = rows.find((r) => r.current)!;

      expect(cur).toMatchObject({ group: "completed", id: "cur", state });
      expect(GLYPH[cur.state]).toBe(state === "failed" ? "✗" : "✓");
      expect(rows.map((r) => r.id)).toEqual(["w1", "cur", "f1"]);
      // the inline strip leaves the current session out, whatever its state
      expect(
        buildStripRows([], [{ current: true, id: "cur", status }], NOW).map(
          (r) => r.id,
        ),
      ).toEqual([]);
    }
  });

  it("shows a session with no turn yet as idle (○) in the Completed group, which it leads", () => {
    const rows = build({
      sessions: [
        { id: "f1", status: "failed", title: "F1" },
        { current: true, id: "cur", message_count: 0, status: "idle" },
        { id: "w1", status: "working", title: "W1" },
      ],
    });
    const cur = rows.find((r) => r.current)!;

    expect(cur).toMatchObject({
      group: "completed",
      id: "cur",
      state: "idle",
    });
    expect(GLYPH[cur.state]).toBe("○");
    expect(rows.map((r) => [r.id, r.group, r.state])).toEqual([
      ["w1", "working", "working"],
      ["cur", "completed", "idle"],
      ["f1", "completed", "failed"],
    ]);

    // Enter on it closes the view; x does not arm a stop on the current session.
    const idx = rows.indexOf(cur);

    expect(
      reduceViewKey({ confirmKey: null, index: idx }, rows, { return: true }, 5)
        .effect,
    ).toEqual({ type: "close" });
    expect(
      reduceViewKey({ confirmKey: null, index: idx }, rows, { ch: "x" }, 5).nav
        .confirmKey,
    ).toBeNull();
  });

  it("keeps an idle session that has messages (or an unknown count) as completed", () => {
    for (const message_count of [3, undefined]) {
      const rows = build({
        sessions: [
          {
            current: true,
            id: "cur",
            status: "idle",
            ...(message_count === undefined ? {} : { message_count }),
          },
        ],
      });

      expect(rows[0]).toMatchObject({
        group: "completed",
        id: "cur",
        state: "done",
      });
    }
  });

  it("leaves the inline strip's reading of an empty idle background session unchanged", () => {
    const bg: SessionActiveItem = {
      id: "bg",
      message_count: 0,
      status: "idle",
      title: "BG",
    };

    expect(buildStripRows([], [bg], NOW).map((r) => r.state)).toEqual(["done"]);
    // the view, which owns the idle reading, shows the same session as idle (in Completed)
    expect(build({ sessions: [bg] })).toMatchObject([
      { group: "completed", id: "bg", state: "idle" },
    ]);
  });

  it("picks the current row by id over a stale `current` flag, and by the flag only when the id is unknown", () => {
    const stale: SessionActiveItem[] = [
      { current: true, id: "old", status: "idle", title: "Old" },
      { id: "me", status: "working", title: "Me" },
    ];

    expect(
      build({ currentSid: "me", sessions: stale })
        .filter((r) => r.current)
        .map((r) => r.id),
    ).toEqual(["me"]);
    expect(
      build({ currentSid: null, sessions: stale })
        .filter((r) => r.current)
        .map((r) => r.id),
    ).toEqual(["old"]);
    expect(
      build({ currentSid: "gone", sessions: stale }).filter((r) => r.current),
    ).toEqual([]);
  });

  it("carries model and last_active from the live session", () => {
    const rows = build({ sessions });

    expect(rows.find((r) => r.id === "w1")?.model).toBe("model-a");
    expect(rows.find((r) => r.id === "i1")?.lastActive).toBe(123);
  });

  it("does not repeat the daemon's sub-agent session rows next to the roster agents", () => {
    const rows = build({
      sessions: [
        {
          id: "sa-1",
          origin: "subagent",
          status: "running",
          title: "child",
        } as SessionActiveItem,
      ],
      subagents: [agent("sa-1")],
    });

    expect(rows.map((r) => [r.id, r.kind])).toEqual([["sa-1", "agent"]]);
  });

  it("does not cap the number of sessions", () => {
    const many = Array.from({ length: 12 }, (_, i): SessionActiveItem => ({
      id: `s${i}`,
      status: "working",
    }));

    expect(build({ sessions: many })).toHaveLength(12);
  });

  it("shows past rows only for this cwd, newest first", () => {
    const rows = build({
      past: [
        past("old", 100),
        past("other", 500, "/work/elsewhere"),
        past("nocwd", 600, null),
        past("new", 300),
      ],
    });

    expect(rows.map((r) => r.id)).toEqual(["new", "old"]);
    expect(rows[0]).toMatchObject({
      current: false,
      group: "completed",
      key: "past:new",
      kind: "past",
      lastActive: 300,
      title: "past new",
    });
  });

  it("shows no past rows when the current cwd is unknown, even for rows without a cwd", () => {
    expect(
      build({
        currentCwd: undefined,
        past: [past("a", 1), past("b", 2, null)],
      }),
    ).toEqual([]);
  });

  it("hides earlier sessions that were never used", () => {
    const rows = build({
      past: [
        { ...past("empty", 300), message_count: 0 },
        { ...past("used", 200), message_count: 4 },
        past("unknown", 100),
      ],
    });

    expect(rows.map((r) => r.id)).toEqual(["used", "unknown"]);
  });

  it("keeps the project filter strict: another path or a missing cwd is excluded", () => {
    const rows = build({
      past: [
        past("same", 300),
        past("sub", 250, `${CWD}/sub`),
        past("prefix", 240, `${CWD}2`),
        past("none", 200, null),
        { ...past("nullcwd", 150), cwd: null },
      ],
    });

    expect(rows.map((r) => r.id)).toEqual(["same"]);
  });

  it("does not list a live session again as a past one", () => {
    const rows = build({
      past: [past("w1", 50), past("p1", 40)],
      sessions: [{ id: "w1", status: "working" }],
    });

    expect(rows.map((r) => r.key)).toEqual(["session:w1", "past:p1"]);
  });

  it("puts archived sessions in the Archived group, last, newest first, through the same filters", () => {
    const rows = build({
      archived: [
        past("a-old", 100),
        past("a-new", 300),
        past("a-other", 400, "/work/elsewhere"),
        { ...past("a-empty", 500), message_count: 0 },
        past("w1", 600),
      ],
      past: [past("p1", 50)],
      sessions: [{ id: "w1", status: "working" }],
    });

    expect(rows.map((r) => [r.key, r.group])).toEqual([
      ["session:w1", "working"],
      ["past:p1", "completed"],
      ["past:a-new", "archived"],
      ["past:a-old", "archived"],
    ]);
    expect(rows.at(-1)).toMatchObject({
      current: false,
      kind: "past",
      state: "done",
      title: "past a-old",
    });
  });

  it("shows the Archived header only when there are archived rows", () => {
    const headers = (rows: ViewRow[]) =>
      buildLines(rows, IDLE_VIEW_NAV.collapsedGroups)
        .filter((l) => l.type === "header")
        .map((l) => (l.type === "header" ? l.group : null));

    expect(headers(build({ past: [past("p1", 1)] }))).toEqual(["completed"]);
    expect(
      headers(build({ archived: [past("a1", 1)], past: [past("p1", 1)] })),
    ).toEqual(["completed", "archived"]);
  });

  it("dates a past row by its last activity (updated_at), falling back to its creation", () => {
    const rows = build({
      archived: [{ ...past("a1", 100), updated_at: 900 }],
      past: [{ ...past("p1", 200), updated_at: 800 }, past("p2", 150)],
    });

    expect(rows.map((r) => [r.id, r.lastActive])).toEqual([
      ["p1", 800],
      ["p2", 150],
      ["a1", 900],
    ]);
  });

  it("orders past rows by last activity, so the ages they show run newest to oldest", () => {
    // p1 was created last but p2 was used last.
    const rows = build({
      past: [
        { ...past("p1", 300), updated_at: 310 },
        { ...past("p2", 100), updated_at: 900 },
        past("p3", 500),
      ],
    });

    expect(rows.map((r) => r.id)).toEqual(["p2", "p3", "p1"]);
  });
});

const vrow = (id: string, over: Partial<ViewRow> = {}): ViewRow => ({
  activity: "",
  current: false,
  elapsedSeconds: null,
  group: "working",
  id,
  key: `session:${id}`,
  kind: "session",
  state: "working",
  title: id,
  ...over,
});

const ROWS: ViewRow[] = [
  vrow("cur", { current: true }),
  vrow("s1"),
  vrow("a1", { key: "agent:a1", kind: "agent" }),
  vrow("p1", {
    group: "completed",
    key: "past:p1",
    kind: "past",
    state: "done",
  }),
  vrow("s2"),
];

const press = (nav: ViewNav, key: ViewKey, rows = ROWS, page = 2) =>
  reduceViewKey(nav, rows, key, page);
const at = (index: number): ViewNav => ({ ...IDLE_VIEW_NAV, index });

describe("collapsible groups", () => {
  // Needs input: n1 · Working: w1, w2 · Completed: c1
  const G: ViewRow[] = [
    vrow("n1", { group: "input", state: "input" }),
    vrow("w1"),
    vrow("w2"),
    vrow("c1", { group: "completed", state: "done" }),
  ];
  const onHeader: ViewNav = {
    ...IDLE_VIEW_NAV,
    collapsedGroups: ["working"],
    header: "working",
    index: 1,
  };

  it("c collapses the selected row's group and the selection moves to its header", () => {
    const r = reduceViewKey(at(2), G, { ch: "c" }, 2);

    expect(r.effect).toBeNull();
    expect(r.nav).toMatchObject({
      collapsedGroups: ["archived", "working"],
      header: "working",
    });
  });

  it("↑↓ stop on a collapsed group's header and never on its hidden rows", () => {
    const down = reduceViewKey(onHeader, G, { down: true }, 2).nav;

    expect(down).toMatchObject({ header: null, index: 3 });
    expect(reduceViewKey(down, G, { up: true }, 2).nav).toMatchObject({
      header: "working",
    });
    expect(reduceViewKey(onHeader, G, { up: true }, 2).nav).toMatchObject({
      header: null,
      index: 0,
    });
  });

  it("⏎, → and c on a collapsed header expand it onto its first row, attaching nothing", () => {
    for (const key of [{ return: true }, { right: true }, { ch: "c" }]) {
      const r = reduceViewKey(onHeader, G, key, 2);

      expect(r.effect).toBeNull();
      expect(r.nav).toMatchObject({
        collapsedGroups: [],
        header: null,
        index: 1,
      });
    }
  });

  it("a header click collapses another group without moving the selection", () => {
    expect(toggleGroup(at(0), G, "working")).toMatchObject({
      collapsedGroups: ["archived", "working"],
      header: null,
      index: 0,
    });
    expect(toggleGroup(onHeader, G, "working")).toMatchObject({
      collapsedGroups: [],
      header: null,
      index: 1,
    });
  });
});

describe("archive and restore", () => {
  // Working: cur, s1 · Completed: p1, p2 (earlier sessions) · Archived: x1, x2
  const pastRow = (id: string, group: "archived" | "completed") =>
    vrow(id, { group, key: `past:${id}`, kind: "past", state: "done" });
  const A: ViewRow[] = [
    vrow("cur", { current: true }),
    vrow("s1"),
    pastRow("p1", "completed"),
    pastRow("p2", "completed"),
    pastRow("x1", "archived"),
    pastRow("x2", "archived"),
  ];
  // The Archived group opened, so its rows can be selected.
  const open = (index: number): ViewNav => ({
    ...IDLE_VIEW_NAV,
    collapsedGroups: [],
    index,
  });

  it("starts with Archived collapsed: its rows hidden, its header selectable, c expands it", () => {
    expect(IDLE_VIEW_NAV.collapsedGroups).toEqual(["archived"]);

    const lines = buildLines(A, IDLE_VIEW_NAV.collapsedGroups);

    expect(lines.at(-1)).toEqual({ group: "archived", type: "header" });
    expect(
      lines.some((l) => l.type === "row" && l.row.group === "archived"),
    ).toBe(false);
    expect(navItems(A, IDLE_VIEW_NAV.collapsedGroups).at(-1)).toEqual({
      group: "archived",
      type: "header",
    });

    const onHeader = press(at(3), { down: true }, A).nav;

    expect(onHeader).toMatchObject({ header: "archived", index: 4 });
    expect(press(onHeader, { ch: "c" }, A).nav).toMatchObject({
      collapsedGroups: [],
      header: null,
      index: 4,
    });
  });

  it("a archives an earlier session and u restores an archived one, the cursor stepping to a neighbour", () => {
    // The next row of the same group, else the item above: never the row that is leaving.
    for (const [nav, ch, row, type, index] of [
      [open(2), "a", A[2], "archive", 3],
      [open(3), "a", A[3], "archive", 2],
      [at(3), "a", A[3], "archive", 2],
      [open(4), "u", A[4], "unarchive", 5],
      [open(5), "u", A[5], "unarchive", 4],
    ] as const) {
      const r = press(nav, { ch }, A);

      expect(r.effect).toEqual({ row, type });
      expect(r.nav).toEqual({ ...nav, header: null, index });
    }

    // On the only row there is nowhere to step to: the cursor stays.
    const lone = [A[2]!];

    expect(press(open(0), { ch: "a" }, lone)).toEqual({
      consumed: true,
      effect: { row: A[2], type: "archive" },
      nav: open(0),
    });
  });

  it("a and u do nothing on rows they do not apply to, on a header, or with a stop pending", () => {
    const onHeader: ViewNav = { ...at(4), header: "archived" };

    for (const [nav, ch] of [
      [open(0), "a"], // this session
      [open(1), "a"], // a live session
      [open(4), "a"], // already archived
      [open(1), "u"], // a live session
      [open(2), "u"], // an earlier session that is not archived
      [onHeader, "a"],
      [onHeader, "u"],
    ] as const) {
      const r = press(nav, { ch }, A);

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(nav);
    }

    const agentRows = [vrow("a1", { key: "agent:a1", kind: "agent" })];

    expect(press(open(0), { ch: "a" }, agentRows).effect).toBeNull();

    // A pending stop is cancelled by a and u, like by any other key, and nothing is archived.
    const ask = press(open(1), { ch: "x" }, A).nav;

    for (const ch of ["a", "u"]) {
      const r = press(ask, { ch }, A);

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(open(1));
    }
  });

  it("a and u are list commands: they are not typed into the composer", () => {
    for (const ch of ["a", "u"]) {
      for (const index of [1, 2, 4]) {
        const r = press(open(index), { ch }, A);

        expect(r.nav.focus).toBe("list");
        expect(r.nav.draft).toBe("");
      }
    }
  });
});

describe("reduceViewKey", () => {
  it("moves with ↑/↓ and clamps at both ends without wrapping", () => {
    expect(press(at(0), { up: true }).nav).toEqual(at(0));
    expect(press(at(0), { down: true }).nav).toEqual(at(1));
    expect(press(at(4), { down: true }).nav).toEqual(at(4));
    expect(press(at(3), { up: true }).nav).toEqual(at(2));
  });

  it("jumps with home/end and pages by pageSize, clamped", () => {
    expect(press(at(3), { home: true }).nav.index).toBe(0);
    expect(press(at(1), { end: true }).nav.index).toBe(4);
    expect(press(at(0), { pageDown: true }).nav.index).toBe(2);
    expect(press(at(3), { pageDown: true }).nav.index).toBe(4);
    expect(press(at(3), { pageUp: true }).nav.index).toBe(1);
    expect(press(at(1), { pageUp: true }).nav.index).toBe(0);
    expect(press(at(0), { pageDown: true }, ROWS, 0).nav.index).toBe(1);
  });

  it("⏎ attaches to the selected row, and closes on the current one", () => {
    expect(press(at(1), { return: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
    expect(press(at(3), { return: true }).effect).toEqual({
      row: ROWS[3],
      type: "activate",
    });
    expect(press(at(0), { return: true }).effect).toEqual({ type: "close" });
  });

  it("→ attaches to the selected row like ⏎, and closes on the current one", () => {
    expect(press(at(1), { right: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
    expect(press(at(2), { right: true }).effect).toEqual({
      row: ROWS[2],
      type: "activate",
    });
    expect(press(at(3), { right: true }).effect).toEqual({
      row: ROWS[3],
      type: "activate",
    });
    expect(press(at(0), { right: true })).toEqual({
      consumed: true,
      effect: { type: "close" },
      nav: at(0),
    });
    expect(press(at(1), { right: true }, [])).toEqual({
      consumed: true,
      effect: null,
      nav: at(0),
    });
  });

  it("→ cancels a pending stop confirmation instead of stopping or attaching", () => {
    const ask = press(at(1), { ch: "x" }).nav;
    const r = press(ask, { right: true });

    expect(r.effect).toBeNull();
    expect(r.nav).toEqual(at(1));
    // the next → attaches as usual
    expect(press(r.nav, { right: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
  });

  it("x asks first; y or ⏎ stops", () => {
    const ask = press(at(1), { ch: "x" });

    expect(ask.effect).toBeNull();
    expect(ask.nav).toEqual({
      ...IDLE_VIEW_NAV,
      confirmKey: "session:s1",
      index: 1,
    });

    for (const key of [{ ch: "y" }, { ch: "Y" }, { return: true }]) {
      const r = press(ask.nav, key);

      expect(r.effect).toEqual({ row: ROWS[1], type: "stop" });
      expect(r.nav.confirmKey).toBeNull();
    }

    expect(press(at(2), { ch: "x" }).nav.confirmKey).toBe("agent:a1");
  });

  it("any other key cancels the confirmation and does nothing else", () => {
    const ask = press(at(1), { ch: "x" }).nav;

    for (const key of [
      { ch: "n" },
      { escape: true },
      { left: true },
      { down: true },
      { ch: "q" },
    ]) {
      const r = press(ask, key);

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual({ ...IDLE_VIEW_NAV, index: 1 });
    }
  });

  it("ignores x on past rows and on the current session", () => {
    for (const i of [0, 3]) {
      const r = press(at(i), { ch: "x" });

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual({ ...IDLE_VIEW_NAV, index: i });
    }
  });

  it("n and tab focus the composer; esc and ← close", () => {
    const focus = { draft: "", focus: "input" as const };

    for (const key of [{ ch: "n" }, { tab: true }]) {
      const result = press(at(2), key);

      expect(result.nav).toEqual({ ...IDLE_VIEW_NAV, index: 2, ...focus });
      expect(result.effect).toBeNull();
    }

    expect(press(at(2), { escape: true }).effect).toEqual({ type: "close" });
    expect(press(at(2), { left: true }).effect).toEqual({ type: "close" });
  });

  it("in input mode tab goes back to the list, keeping the draft", () => {
    const typing = { ...IDLE_VIEW_NAV, draft: "hi", focus: "input" as const };

    // printable keys belong to the composer, not the list
    expect(press(typing, { ch: "q" })).toEqual({
      consumed: true,
      effect: null,
      nav: typing,
    });

    const back = press(typing, { tab: true });

    expect(back.nav).toEqual({ ...at(0), draft: "hi" });
    expect(back.effect).toBeNull();
  });

  it("esc in input mode clears a typed draft first, closing only once empty", () => {
    const typing = { ...IDLE_VIEW_NAV, draft: "hi", focus: "input" as const };

    expect(press(typing, { escape: true }).nav).toEqual({
      ...at(0),
      draft: "",
      focus: "input",
    });
    expect(press({ ...typing, draft: "" }, { escape: true }).effect).toEqual({
      type: "close",
    });
  });

  it("in input mode even the list's own commands, like c, type into the draft", () => {
    const typing = { ...at(0), draft: "hi", focus: "input" as const };
    const r = press(typing, { ch: "c" });

    expect(r.consumed).toBe(true);
    expect(r.nav).toEqual(typing);
    expect(r.effect).toBeNull();
  });

  it("ignores n, x and y chords with Ctrl or Meta; arrows, ⏎ and esc still work", () => {
    for (const mod of [{ ctrl: true }, { meta: true }]) {
      for (const ch of ["n", "x", "y", "N"]) {
        const r = press(at(1), { ch, ...mod });

        expect(r.effect).toBeNull();
        expect(r.nav).toEqual(at(1));
      }

      const ask = press(at(1), { ch: "x" }).nav;

      expect(press(ask, { ch: "y", ...mod }).effect).toBeNull();
      expect(press(at(1), { down: true, ...mod }).nav).toEqual(at(2));
      expect(press(at(1), { return: true, ...mod }).effect).toEqual({
        row: ROWS[1],
        type: "activate",
      });
      expect(press(at(1), { escape: true, ...mod }).effect).toEqual({
        type: "close",
      });
    }
  });

  it("swallows every other key without an effect", () => {
    for (const key of [{}, { ch: "\x7f" }, { ch: "\x1b[1;5A" }]) {
      const r = press(at(2), key);

      expect(r.consumed).toBe(true);
      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(2));
    }
  });

  it("on the list, typed text other than c/x/n goes to the composer with the keyboard", () => {
    expect(press(at(2), { ch: "q" })).toEqual({
      consumed: true,
      effect: null,
      nav: { ...at(2), draft: "q", focus: "input" },
    });
    // The case is kept, a burst arrives whole, and an earlier draft is extended rather than replaced.
    expect(press({ ...at(2), draft: "fix " }, { ch: "Hello" }).nav).toEqual({
      ...at(2),
      draft: "fix Hello",
      focus: "input",
    });
    // The list's own letters stay commands while the list has the keyboard: c collapses, x asks, n composes.
    expect(press(at(1), { ch: "c" }).nav.focus).toBe("list");
    expect(press(at(1), { ch: "c" }).nav.draft).toBe("");
    expect(press(at(1), { ch: "x" }).nav).toEqual({
      ...at(1),
      confirmKey: ROWS[1]!.key,
    });
    expect(press(at(1), { ch: "n" }).nav).toEqual({
      ...at(1),
      focus: "input",
    });
    // A pending stop confirmation still eats the next key.
    const ask = press(at(1), { ch: "x" }).nav;

    expect(press(ask, { ch: "q" }).nav).toEqual(at(1));
  });

  it("in input mode ↑/↓ on an empty composer hand the keyboard to the list, the selection where it was", () => {
    const empty = { ...at(2), focus: "input" as const };

    expect(press(empty, { up: true })).toEqual({
      consumed: true,
      effect: null,
      nav: at(2),
    });
    expect(press(empty, { down: true }).nav).toEqual(at(2));
    // A collapsed header keeps the selection too.
    const onHeader: ViewNav = {
      ...empty,
      collapsedGroups: ["completed"],
      header: "completed",
    };

    expect(press(onHeader, { up: true }).nav).toEqual({
      ...onHeader,
      focus: "list",
    });
    // With text in it the arrows stay with the composer; with no rows there is no list to go to.
    expect(press({ ...empty, draft: "hi" }, { up: true }).nav).toEqual({
      ...empty,
      draft: "hi",
    });
    expect(
      press({ ...IDLE_VIEW_NAV, focus: "input" }, { up: true }, []).nav,
    ).toEqual({ ...at(0), focus: "input" });
  });

  it("with no rows only n, esc and ← do anything", () => {
    const none: ViewRow[] = [];

    for (const key of [
      { down: true },
      { up: true },
      { end: true },
      { pageDown: true },
      { return: true },
      { ch: "x" },
    ]) {
      const r = press(IDLE_VIEW_NAV, key, none);

      expect(r.consumed).toBe(true);
      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(0));
    }

    expect(press(IDLE_VIEW_NAV, { ch: "n" }, none).nav).toEqual({
      ...IDLE_VIEW_NAV,
      draft: "",
      focus: "input",
    });
    expect(press(IDLE_VIEW_NAV, { escape: true }, none).effect).toEqual({
      type: "close",
    });
    expect(press(IDLE_VIEW_NAV, { left: true }, none).effect).toEqual({
      type: "close",
    });
  });

  it("clamps the index when the rows shrink under it", () => {
    const short = ROWS.slice(0, 2);

    expect(press(at(4), { ch: "q" }, short).nav.index).toBe(1);
    expect(press(at(4), { up: true }, short).nav.index).toBe(0);
    expect(press(at(4), { return: true }, short).effect).toEqual({
      row: short[1],
      type: "activate",
    });
  });

  it("drops a confirmation whose row has gone", () => {
    const stale: ViewNav = {
      ...IDLE_VIEW_NAV,
      confirmKey: "session:gone",
      index: 1,
    };

    const y = press(stale, { ch: "y" });

    expect(y.effect).toBeNull();
    expect(y.nav.confirmKey).toBeNull();

    const empty = press(stale, { ch: "q" }, []);

    expect(empty.nav).toEqual({ ...IDLE_VIEW_NAV, index: 0 });
    // Like a live confirmation, a stale one eats Esc: the next Esc closes.
    expect(press(stale, { escape: true }, []).effect).toBeNull();
    expect(press(at(0), { escape: true }, []).effect).toEqual({
      type: "close",
    });
  });

  it("a stale confirmation eats ⏎ instead of attaching to the row under the cursor", () => {
    const r = press({ confirmKey: "agent:gone", index: 1 }, { return: true });

    expect(r.effect).toBeNull();
    expect(r.nav.confirmKey).toBeNull();
    expect(press(r.nav, { return: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
  });
});

describe("selectedIndex", () => {
  it("follows the selected key when rows are inserted above it or reordered", () => {
    const moved = [vrow("new"), ROWS[2]!, ROWS[0]!, ROWS[1]!];

    expect(selectedIndex(moved, "session:s1", 1)).toBe(3);
    expect(selectedIndex(moved, "agent:a1", 2)).toBe(1);
  });

  it("falls back to the old position, clamped, when the key is gone or unset", () => {
    expect(selectedIndex(ROWS, "session:gone", 2)).toBe(2);
    expect(selectedIndex(ROWS, null, 9)).toBe(4);
    expect(selectedIndex([], "session:s1", 3)).toBe(0);
  });
});

describe("visibleWindow", () => {
  it("shows everything when it fits", () => {
    expect(visibleWindow(3, 2, 5)).toEqual({ end: 3, start: 0 });
    expect(visibleWindow(0, 0, 5)).toEqual({ end: 0, start: 0 });
  });

  it("scrolls just far enough to reveal the selection", () => {
    expect(visibleWindow(10, 2, 4)).toEqual({ end: 4, start: 0 });
    expect(visibleWindow(10, 5, 4)).toEqual({ end: 6, start: 2 });
    expect(visibleWindow(10, 9, 4)).toEqual({ end: 10, start: 6 });
  });

  it("keeps the previous window while the selection is inside it", () => {
    expect(visibleWindow(10, 5, 4, 4)).toEqual({ end: 8, start: 4 });
    expect(visibleWindow(10, 3, 4, 4)).toEqual({ end: 7, start: 3 });
    expect(visibleWindow(10, 8, 4, 4)).toEqual({ end: 9, start: 5 });
  });

  it("clamps out-of-range input", () => {
    expect(visibleWindow(10, 50, 4)).toEqual({ end: 10, start: 6 });
    expect(visibleWindow(10, -3, 4, 3)).toEqual({ end: 4, start: 0 });
    expect(visibleWindow(10, 2, 4, 99)).toEqual({ end: 6, start: 2 });
    expect(visibleWindow(5, 4, 0)).toEqual({ end: 5, start: 4 });
  });
});
