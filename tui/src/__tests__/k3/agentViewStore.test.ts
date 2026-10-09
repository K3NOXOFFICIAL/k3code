import { describe, expect, it } from "vitest";

import {
  buildViewRows,
  IDLE_VIEW_NAV,
  type PastSessionRow,
  reduceViewKey,
  selectedIndex,
  type ViewKey,
  type ViewNav,
  type ViewRow,
  visibleWindow,
} from "../../k3/agentViewStore.js";
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

  it("orders groups input, working, finished, past and keeps the strip order inside a group", () => {
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
      ["f1", "finished"],
      ["d1", "finished"],
      ["a2", "finished"],
      ["p1", "past"],
    ]);
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
      group: "past",
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
  vrow("p1", { group: "past", key: "past:p1", kind: "past", state: "done" }),
  vrow("s2"),
];

const press = (nav: ViewNav, key: ViewKey, rows = ROWS, page = 2) =>
  reduceViewKey(nav, rows, key, page);
const at = (index: number): ViewNav => ({ confirmKey: null, index });

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

  it("x asks first; y or ⏎ stops", () => {
    const ask = press(at(1), { ch: "x" });

    expect(ask.effect).toBeNull();
    expect(ask.nav).toEqual({ confirmKey: "session:s1", index: 1 });

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
      expect(r.nav).toEqual(at(1));
    }
  });

  it("ignores x on past rows and on the current session", () => {
    for (const i of [0, 3]) {
      const r = press(at(i), { ch: "x" });

      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(i));
    }
  });

  it("n starts a new session; esc and ← close", () => {
    expect(press(at(2), { ch: "n" }).effect).toEqual({ type: "new" });
    expect(press(at(2), { escape: true }).effect).toEqual({ type: "close" });
    expect(press(at(2), { left: true }).effect).toEqual({ type: "close" });
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
    for (const key of [{ ch: "q" }, { ch: "y" }, {}]) {
      const r = press(at(2), key);

      expect(r.consumed).toBe(true);
      expect(r.effect).toBeNull();
      expect(r.nav).toEqual(at(2));
    }
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
    const stale: ViewNav = { confirmKey: "session:gone", index: 1 };

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
