import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import { AgentStripView, buildStripRows } from "../../k3/agentStrip.js";
import {
  IDLE_NAV,
  reduceStripKey,
  shouldEnterStrip,
  type StripNav,
  type StripRow,
  STRIP_MAX_ROWS,
} from "../../k3/agentStripStore.js";
import { DEFAULT_THEME } from "../../theme.js";
import type { SubagentProgress } from "../../types.js";

const row = (n: number, state: StripRow["state"] = "working"): StripRow => ({
  activity: `doing step ${n}`,
  elapsedSeconds: 65 + n,
  id: `id${n}`,
  key: `session:id${n}`,
  kind: "session",
  state,
  title: `task ${n}`,
});

const dump = (
  rows: StripRow[],
  extra: Partial<React.ComponentProps<typeof AgentStripView>> = {},
) => {
  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 24 });
  const frames: string[] = [];

  stdout.on("data", (c) => frames.push(c.toString()));

  const view = renderSync(
    <AgentStripView cols={80} rows={rows} t={DEFAULT_THEME} {...extra} />,
    {
      stdin: new PassThrough() as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  view.unmount();
  view.cleanup();

  return stripAnsi(
    frames.filter((f) => stripAnsi(f).trim()).at(-1) ?? "",
  ).trimEnd();
};

describe("AgentStripView", () => {
  it("renders each state glyph", () => {
    const out = dump([
      row(1, "working"),
      row(2, "input"),
      row(3, "done"),
      row(4, "failed"),
    ]);
    console.log(`\n--- agent strip dump ---\n${out}\n------------------------`);
    expect(out).toContain("◐ task 1");
    expect(out).toContain("● task 2");
    expect(out).toContain("✓ task 3");
    expect(out).toContain("✗ task 4");
    expect(out).toContain("needs input");
    expect(out).toContain("1m 6s");
    expect(out).toContain("doing step 1");
  });

  it("caps at 6 rows and says +N more", () => {
    const out = dump(Array.from({ length: 9 }, (_, i) => row(i)));
    expect(out.split("\n").filter((l) => l.includes("task "))).toHaveLength(
      STRIP_MAX_ROWS,
    );
    expect(out).toContain("+3 more");
  });

  it("is empty when there are no rows", () => {
    expect(dump([])).toBe("");
  });

  it("marks the selected row and the stop confirmation", () => {
    const rows = [row(1), row(2)];
    const out = dump(rows, {
      confirmKey: rows[1]!.key,
      focused: true,
      index: 1,
    });
    expect(out).toMatch(/› .*task 2/);
    expect(out).toContain("stop? y/n");
  });
});

describe("buildStripRows", () => {
  const sub = (status: SubagentProgress["status"]): SubagentProgress => ({
    depth: 0,
    goal: "sub goal",
    id: "s1",
    index: 0,
    notes: ["note"],
    parentId: null,
    startedAt: 1000,
    status,
    taskCount: 1,
    thinking: [],
    toolCount: 0,
    tools: [],
  });

  it("maps statuses, skips the current session and sorts needs-input first", () => {
    const rows = buildStripRows(
      [sub("completed")],
      [
        { current: true, id: "cur", status: "working" },
        { id: "a", status: "working", title: "A" },
        { id: "b", status: "waiting", title: "B" },
      ],
      5000,
    );

    expect(rows.map((r) => [r.title, r.state])).toEqual([
      ["B", "input"],
      ["A", "working"],
      ["sub goal", "done"],
    ]);
  });

  it("maps failed sub-agents", () => {
    expect(buildStripRows([sub("failed")], [], 5000)[0]!.state).toBe("failed");
  });
});

describe("strip keyboard", () => {
  const rows = [row(1), row(2), row(3)];
  const focused: StripNav = { ...IDLE_NAV, focused: true };

  it("↓ enters only from an empty input with no history cycle", () => {
    expect(shouldEnterStrip({ historyIdx: null, input: "", rows: 2 })).toBe(
      true,
    );
    expect(shouldEnterStrip({ historyIdx: null, input: "hi", rows: 2 })).toBe(
      false,
    );
    expect(shouldEnterStrip({ historyIdx: 3, input: "", rows: 2 })).toBe(false);
    expect(shouldEnterStrip({ historyIdx: null, input: "", rows: 0 })).toBe(
      false,
    );
  });

  it("ignores keys when not focused", () => {
    expect(reduceStripKey(IDLE_NAV, rows, { down: true }).consumed).toBe(false);
  });

  it("↑/↓ move between rows and clamp at the bottom", () => {
    let n = reduceStripKey(focused, rows, { down: true }).nav;
    expect(n.index).toBe(1);
    n = reduceStripKey(n, rows, { down: true }).nav;
    n = reduceStripKey(n, rows, { down: true }).nav;
    expect(n.index).toBe(2);
    expect(reduceStripKey(n, rows, { up: true }).nav.index).toBe(1);
  });

  it("↑ past the first row and Esc return to the input", () => {
    expect(reduceStripKey(focused, rows, { up: true }).nav.focused).toBe(false);
    expect(reduceStripKey(focused, rows, { escape: true }).nav.focused).toBe(
      false,
    );
  });

  it("Enter activates the selected row and leaves the strip", () => {
    const r = reduceStripKey({ ...focused, index: 1 }, rows, { return: true });
    expect(r.effect).toEqual({ row: rows[1], type: "activate" });
    expect(r.nav.focused).toBe(false);
  });

  it("x asks for confirmation; y stops, anything else cancels", () => {
    const ask = reduceStripKey({ ...focused, index: 2 }, rows, { ch: "x" });
    expect(ask.effect).toBeNull();
    expect(ask.nav.confirmKey).toBe(rows[2]!.key);
    expect(reduceStripKey(ask.nav, rows, { ch: "y" }).effect).toEqual({
      row: rows[2],
      type: "stop",
    });
    const no = reduceStripKey(ask.nav, rows, { ch: "n" });
    expect(no.effect).toBeNull();
    expect(no.nav.confirmKey).toBeNull();
    expect(no.nav.focused).toBe(true);
  });
});

describe("buildStripRows: sub-agent session rows", () => {
  it("skips the daemon's sub-agent rows (the in-turn roster already shows them) and maps running/queued to working", () => {
    const sessions = [
      { id: "sa-1", origin: "subagent", status: "running", title: "child" },
      { id: "bg-1", origin: "automation", status: "running", title: "cron" },
      { id: "q-1", status: "queued", title: "queued job" },
    ] as unknown as Parameters<typeof buildStripRows>[1];
    const rows = buildStripRows([], sessions, Date.now(), "current");

    expect(rows.map((r) => r.id)).toEqual(["bg-1", "q-1"]);
    expect(rows.every((r) => r.state === "working")).toBe(true);
  });
});
