import { describe, expect, it } from "vitest";

import {
  $collapsedGroups,
  buildViewRows,
  headerKey,
  IDLE_VIEW_NAV,
  type PastSessionRow,
  reduceViewKey,
  resetCollapsedGroups,
  selectedIndex,
  toggleGroup,
  type ViewGroup,
  type ViewItem,
  viewItems,
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
    const items = viewItems(rows, new Set());
    const idx = items.findIndex((it) => it.key === cur.key);

    expect(
      reduceViewKey(
        { confirmKey: null, index: idx },
        items,
        { return: true },
        5,
      ).effect,
    ).toEqual({ type: "close" });
    expect(
      reduceViewKey({ confirmKey: null, index: idx }, items, { ch: "x" }, 5).nav
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

/**
 * The cursor list of ROWS with nothing folded:
 * 0 Needs input (0) · 1 Working (4) · 2 cur · 3 s1 · 4 a1 · 5 s2 · 6 Completed (1) · 7 p1
 */
const ITEMS = viewItems(ROWS, new Set());
const CUR = 2;
const S1 = 3;
const A1 = 4;
const DONE = 6;
const P1 = 7;

const press = (nav: ViewNav, key: ViewKey, items = ITEMS, page = 2) =>
  reduceViewKey(nav, items, key, page);
const at = (index: number): ViewNav => ({ confirmKey: null, index });

describe("viewItems", () => {
  it("lists every group header, empty ones included, with the rows of the open groups", () => {
    expect(
      ITEMS.map((it) =>
        it.type === "header"
          ? `${it.group}(${it.count})${it.collapsed ? "-" : "+"}`
          : it.key,
      ),
    ).toEqual([
      "input(0)+",
      "working(4)+",
      "session:cur",
      "session:s1",
      "agent:a1",
      "session:s2",
      "completed(1)+",
      "past:p1",
    ]);
  });

  it("keeps the header, with its count, and drops the rows of a collapsed group", () => {
    const folded = viewItems(ROWS, new Set<ViewGroup>(["working"]));

    expect(folded.map((it) => it.key)).toEqual([
      headerKey("input"),
      headerKey("working"),
      headerKey("completed"),
      "past:p1",
    ]);
    expect(folded[1]).toMatchObject({ collapsed: true, count: 4 });
  });

  it("toggleGroup folds and unfolds in the shared store, which survives until reset", () => {
    resetCollapsedGroups();
    toggleGroup("completed");
    expect([...$collapsedGroups.get()]).toEqual(["completed"]);
    toggleGroup("input");
    expect([...$collapsedGroups.get()].sort()).toEqual(["completed", "input"]);
    toggleGroup("completed");
    expect([...$collapsedGroups.get()]).toEqual(["input"]);
    resetCollapsedGroups();
    expect($collapsedGroups.get().size).toBe(0);
  });
});

describe("reduceViewKey", () => {
  it("moves with ↑/↓ over headers and rows alike, clamped at both ends without wrapping", () => {
    expect(press(at(0), { up: true }).nav).toEqual(at(0));
    expect(press(at(0), { down: true }).nav).toEqual(at(1));
    expect(press(at(1), { down: true }).nav).toEqual(at(CUR));
    expect(press(at(P1), { down: true }).nav).toEqual(at(P1));
    expect(press(at(S1), { up: true }).nav).toEqual(at(CUR));
    expect(press(at(P1), { up: true }).nav).toEqual(at(DONE));
  });

  it("skips the rows of a collapsed group: ↓ from its header lands on the next header", () => {
    const folded = viewItems(ROWS, new Set<ViewGroup>(["working"]));

    // 0 input · 1 working (folded) · 2 completed · 3 p1
    expect(press(at(1), { down: true }, folded).nav).toEqual(at(2));
    expect(press(at(2), { up: true }, folded).nav).toEqual(at(1));
    expect(press(at(3), { up: true }, folded).nav).toEqual(at(2));
    expect(press(at(0), { end: true }, folded).nav.index).toBe(3);
  });

  it("⏎, → and space on a header toggle its group; ← there still closes the view", () => {
    for (const key of [{ return: true }, { right: true }, { ch: " " }]) {
      expect(press(at(1), key).effect).toEqual({
        group: "working",
        type: "toggle",
      });
      expect(press(at(0), key).effect).toEqual({
        group: "input",
        type: "toggle",
      });
    }

    expect(press(at(1), { left: true }).effect).toEqual({ type: "close" });
    expect(press(at(1), { ch: "x" })).toEqual({
      consumed: true,
      effect: null,
      nav: at(1),
    });
  });

  it("jumps with home/end and pages by pageSize, clamped", () => {
    expect(press(at(S1), { home: true }).nav.index).toBe(0);
    expect(press(at(1), { end: true }).nav.index).toBe(P1);
    expect(press(at(0), { pageDown: true }).nav.index).toBe(2);
    expect(press(at(DONE), { pageDown: true }).nav.index).toBe(P1);
    expect(press(at(S1), { pageUp: true }).nav.index).toBe(1);
    expect(press(at(1), { pageUp: true }).nav.index).toBe(0);
    expect(press(at(0), { pageDown: true }, ITEMS, 0).nav.index).toBe(1);
  });

  it("⏎ attaches to the selected row, and closes on the current one", () => {
    expect(press(at(S1), { return: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
    expect(press(at(P1), { return: true }).effect).toEqual({
      row: ROWS[3],
      type: "activate",
    });
    expect(press(at(CUR), { return: true }).effect).toEqual({ type: "close" });
  });

  it("→ attaches to the selected row like ⏎, and closes on the current one", () => {
    expect(press(at(S1), { right: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
    expect(press(at(A1), { right: true }).effect).toEqual({
      row: ROWS[2],
      type: "activate",
    });
    expect(press(at(P1), { right: true }).effect).toEqual({
      row: ROWS[3],
      type: "activate",
    });
    expect(press(at(CUR), { right: true })).toEqual({
      consumed: true,
      effect: { type: "close" },
      nav: at(CUR),
    });
    expect(press(at(1), { right: true }, [])).toEqual({
      consumed: true,
      effect: null,
      nav: at(0),
    });
  });

  it("→ cancels a pending stop confirmation instead of stopping or attaching", () => {
    const ask = press(at(S1), { ch: "x" }).nav;
    const r = press(ask, { right: true });

    expect(r.effect).toBeNull();
    expect(r.nav).toEqual(at(S1));
    // the next → attaches as usual
    expect(press(r.nav, { right: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
  });

  it("x asks first; y or ⏎ stops", () => {
    const ask = press(at(S1), { ch: "x" });

    expect(ask.effect).toBeNull();
    expect(ask.nav).toEqual({ confirmKey: "session:s1", index: S1 });

    for (const key of [{ ch: "y" }, { ch: "Y" }, { return: true }]) {
      const r = press(ask.nav, key);

      expect(r.effect).toEqual({ row: ROWS[1], type: "stop" });
      expect(r.nav.confirmKey).toBeNull();
    }

    expect(press(at(A1), { ch: "x" }).nav.confirmKey).toBe("agent:a1");
  });

  it("any other key cancels the confirmation and does nothing else", () => {
    const ask = press(at(S1), { ch: "x" }).nav;

    for (const key of [
      { ch: "n" },
      { escape: true },
      { left: true },
      { down: true },
      { ch: "q" },
    ]) {
      const r = press(ask, key);

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(S1));
    }
  });

  it("ignores x on past rows and on the current session", () => {
    for (const i of [CUR, P1]) {
      const r = press(at(i), { ch: "x" });

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(i));
    }
  });

  it("n starts a new session; esc and ← close", () => {
    expect(press(at(A1), { ch: "n" }).effect).toEqual({ type: "new" });
    expect(press(at(1), { ch: "n" }).effect).toEqual({ type: "new" });
    expect(press(at(A1), { escape: true }).effect).toEqual({ type: "close" });
    expect(press(at(A1), { left: true }).effect).toEqual({ type: "close" });
  });

  it("ignores n, x and y chords with Ctrl or Meta; arrows, ⏎ and esc still work", () => {
    for (const mod of [{ ctrl: true }, { meta: true }]) {
      for (const ch of ["n", "x", "y", "N"]) {
        const r = press(at(S1), { ch, ...mod });

        expect(r.effect).toBeNull();
        expect(r.nav).toEqual(at(S1));
      }

      const ask = press(at(S1), { ch: "x" }).nav;

      expect(press(ask, { ch: "y", ...mod }).effect).toBeNull();
      expect(press(at(S1), { down: true, ...mod }).nav).toEqual(at(A1));
      expect(press(at(S1), { return: true, ...mod }).effect).toEqual({
        row: ROWS[1],
        type: "activate",
      });
      expect(press(at(S1), { escape: true, ...mod }).effect).toEqual({
        type: "close",
      });
    }
  });

  it("swallows every other key without an effect", () => {
    for (const key of [{ ch: "q" }, { ch: "y" }, {}]) {
      const r = press(at(A1), key);

      expect(r.consumed).toBe(true);
      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(A1));
    }
  });

  it("with no items only n, esc and ← do anything", () => {
    const none: ViewItem[] = [];

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

    expect(press(IDLE_VIEW_NAV, { ch: "n" }, none).effect).toEqual({
      type: "new",
    });
    expect(press(IDLE_VIEW_NAV, { escape: true }, none).effect).toEqual({
      type: "close",
    });
    expect(press(IDLE_VIEW_NAV, { left: true }, none).effect).toEqual({
      type: "close",
    });
  });

  it("clamps the index when the items shrink under it", () => {
    const short = ITEMS.slice(0, S1 + 1);

    expect(press(at(P1), { ch: "q" }, short).nav.index).toBe(S1);
    expect(press(at(P1), { up: true }, short).nav.index).toBe(CUR);
    expect(press(at(P1), { return: true }, short).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
  });

  it("drops a confirmation whose row has gone", () => {
    const stale: ViewNav = { confirmKey: "session:gone", index: S1 };

    const y = press(stale, { ch: "y" });

    expect(y.effect).toBeNull();
    expect(y.nav.confirmKey).toBeNull();

    const empty = press(stale, { ch: "q" }, []);

    expect(empty.nav).toEqual(at(0));
    // Like a live confirmation, a stale one eats Esc: the next Esc closes.
    expect(press(stale, { escape: true }, []).effect).toBeNull();
    expect(press(at(0), { escape: true }, []).effect).toEqual({
      type: "close",
    });
  });

  it("a stale confirmation eats ⏎ instead of attaching to the row under the cursor", () => {
    const r = press({ confirmKey: "agent:gone", index: S1 }, { return: true });

    expect(r.effect).toBeNull();
    expect(r.nav.confirmKey).toBeNull();
    expect(press(r.nav, { return: true }).effect).toEqual({
      row: ROWS[1],
      type: "activate",
    });
  });

  it("a confirmation does not fire on a row hidden by folding its group", () => {
    const ask = press(at(S1), { ch: "x" }).nav;
    const folded = viewItems(ROWS, new Set<ViewGroup>(["working"]));

    expect(press(ask, { ch: "y" }, folded).effect).toBeNull();
  });
});

describe("selectedIndex", () => {
  it("follows the selected key when rows are inserted above it or reordered", () => {
    const moved = viewItems(
      [vrow("new"), ROWS[2]!, ROWS[0]!, ROWS[1]!],
      new Set(),
    );

    // 0 input · 1 working · 2 new · 3 a1 · 4 cur · 5 s1 · 6 completed
    expect(selectedIndex(moved, "session:s1", 1)).toBe(5);
    expect(selectedIndex(moved, "agent:a1", 2)).toBe(3);
    expect(selectedIndex(moved, headerKey("completed"), 0)).toBe(6);
  });

  it("falls back to the old position, clamped, when the key is gone or unset", () => {
    expect(selectedIndex(ITEMS, "session:gone", 2)).toBe(2);
    expect(selectedIndex(ITEMS, null, 9)).toBe(P1);
    expect(selectedIndex([], "session:s1", 3)).toBe(0);
  });

  it("falls back to the position when the selected row's group is folded", () => {
    const folded = viewItems(ROWS, new Set<ViewGroup>(["working"]));

    expect(selectedIndex(folded, "session:s1", S1)).toBe(3);
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
