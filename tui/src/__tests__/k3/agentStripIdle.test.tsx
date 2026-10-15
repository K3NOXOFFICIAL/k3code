import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyAgentSnapshot, mergeAgentRoster } from "../../app/agentRoster.js";
import { resetTurnState } from "../../app/turnStore.js";
import { patchUiState, resetUiState } from "../../app/uiStore.js";
import type { SubagentListResponse } from "../../gatewayTypes.js";
import { AgentStrip, buildStripRows } from "../../k3/agentStrip.js";

// #170 idle repaint: once a turn ends its own sub-agent events are cleared, and
// the strip knows last turn's children only from the gateway's `subagent.list`
// snapshot. A finished child there had no end time, so the "drop a finished
// child a minute after it ends" rule never applied: the row stayed for good,
// its elapsed time kept counting, and the strip's 1 s clock repainted an idle
// TUI every second.

const NOW = 1_800_000_000_000;

const snapshot = (
  row: Partial<SubagentListResponse["subagents"][number]>,
): SubagentListResponse => ({
  delegations: [],
  subagents: [
    {
      goal: "earlier helper",
      started_at: (NOW - 10 * 60_000) / 1000,
      status: "completed",
      subagent_id: "old",
      ...row,
    },
  ],
});

describe("finished sub-agents known only from the snapshot", () => {
  it("leave the strip a minute after the end the gateway reports", () => {
    const roster = mergeAgentRoster([], snapshot({ duration_seconds: 30 }));

    expect(roster[0]?.durationSeconds).toBe(30);
    // ended 9.5 minutes ago: past the linger
    expect(buildStripRows(roster, [], NOW)).toEqual([]);
    // ended 30 s ago: still lingering, timed by its own duration
    const recent = mergeAgentRoster(
      [],
      snapshot({ duration_seconds: 9 * 60 + 30 }),
    );
    const [row] = buildStripRows(recent, [], NOW);

    expect(row?.state).toBe("done");
    expect(row?.elapsedSeconds).toBe(570);
  });

  it("show no running clock when no end time is known (older gateway)", () => {
    for (const status of ["completed", "failed", "interrupted"]) {
      const roster = mergeAgentRoster([], snapshot({ status }));
      const [row] = buildStripRows(roster, [], NOW);

      expect(row?.state).not.toBe("working");
      expect(row?.elapsedSeconds).toBeNull();
    }

    // a running child stays, timed from its start
    const running = mergeAgentRoster([], snapshot({ status: "running" }));

    expect(buildStripRows(running, [], NOW)[0]?.elapsedSeconds).toBe(600);
  });
});

describe("idle strip", () => {
  const mounted: Array<() => void> = [];

  beforeEach(() => {
    vi.useFakeTimers({ now: NOW });
    resetUiState();
    resetTurnState();
    patchUiState({ sid: "s1" });
  });

  afterEach(() => {
    for (const unmount of mounted.splice(0)) {
      unmount();
    }

    applyAgentSnapshot(null);
    resetUiState();
    vi.useRealTimers();
  });

  const mount = () => {
    const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 24 });
    const out = { text: "" };

    stdout.on("data", (chunk) => {
      out.text += chunk.toString();
    });

    const view = renderSync(<AgentStrip cols={78} />, {
      patchConsole: false,
      stdin: new PassThrough() as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    });

    mounted.push(() => {
      view.unmount();
      view.cleanup();
    });

    return out;
  };

  it("writes no frame for a finished child whose end is unknown", async () => {
    applyAgentSnapshot("s1", snapshot({}));
    const out = mount();

    await vi.advanceTimersByTimeAsync(50);
    expect(out.text).toContain("earlier helper");
    out.text = "";

    // the clock that used to repaint the row every second
    await vi.advanceTimersByTimeAsync(5_000);
    expect(out.text).toBe("");
  });

  it("shows nothing for a child that ended long ago, and writes no frame", async () => {
    applyAgentSnapshot("s1", snapshot({ duration_seconds: 30 }));
    const out = mount();

    await vi.advanceTimersByTimeAsync(50);
    const first = out.text;
    out.text = "";

    await vi.advanceTimersByTimeAsync(5_000);
    expect(out.text).toBe("");
    expect(first).not.toContain("earlier helper");
  });
});
