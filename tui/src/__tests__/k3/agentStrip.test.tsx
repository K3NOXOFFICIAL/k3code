import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import { AgentStripView, buildStripRows } from "../../k3/agentStrip.js";
import { type StripRow, STRIP_MAX_ROWS } from "../../k3/agentStripStore.js";
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

const dump = (rows: StripRow[]) => {
  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 24 });
  const frames: string[] = [];

  stdout.on("data", (c) => frames.push(c.toString()));

  const view = renderSync(
    <AgentStripView cols={80} rows={rows} t={DEFAULT_THEME} />,
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

  it("heads the list with a rule naming it and pointing at the agent view", () => {
    const out = dump([row(1), row(2)]);

    expect(out.split("\n")[0]).toMatch(/^── agents \(2\) · ← agent view ─+$/);
  });

  it("is a read-only list: no selection marker and no stop prompt on any row", () => {
    const lines = dump([row(1), row(2, "input"), row(3, "done")]).split("\n");

    expect(lines.slice(1)).toHaveLength(3);

    for (const line of lines.slice(1)) {
      expect(line).toMatch(/^ {2}[◐●✓✗○] task \d/);
    }

    expect(lines.join("\n")).not.toContain("›");
    expect(lines.join("\n")).not.toContain("stop?");
  });

  it("drops finished sub-agents a minute after they end; running ones stay", () => {
    const agent = (
      status: SubagentProgress["status"],
      startedAt: number,
      durationSeconds?: number,
    ): SubagentProgress => ({
      depth: 0,
      durationSeconds,
      goal: "helper",
      id: `a-${status}-${startedAt}`,
      index: 0,
      notes: [],
      parentId: null,
      startedAt,
      status,
      taskCount: 1,
      thinking: [],
      toolCount: 0,
      tools: [],
    });
    const now = 1_000_000_000;
    const rows = buildStripRows(
      [
        agent("running", now - 5 * 60_000),
        agent("completed", now - 3 * 60_000, 30),
        agent("completed", now - 40_000, 30),
        agent("failed", now - 10 * 60_000, 5),
      ],
      [],
      now,
    );

    expect(rows.map((r) => r.state)).toEqual(["working", "done"]);
    expect(rows[0]?.elapsedSeconds).toBe(300);
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
