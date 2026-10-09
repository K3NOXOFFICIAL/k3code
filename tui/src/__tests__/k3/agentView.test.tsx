import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it, vi } from "vitest";

import { patchUiState, resetUiState } from "../../app/uiStore.js";
import type { GatewayClient } from "../../gatewayClient.js";
import {
  AGENT_VIEW_HINT,
  AgentViewPane,
  AgentViewView,
} from "../../k3/agentView.js";
import { $stripSessions } from "../../k3/agentStripStore.js";
import {
  IDLE_VIEW_NAV,
  type ViewNav,
  type ViewRow,
} from "../../k3/agentViewStore.js";
import { DEFAULT_THEME } from "../../theme.js";
import { waitFor } from "../waitFor.js";

const row = (n: number, over: Partial<ViewRow> = {}): ViewRow => ({
  activity: "",
  current: false,
  elapsedSeconds: 65 + n,
  group: "working",
  id: `id${n}`,
  key: `session:id${n}`,
  kind: "session",
  state: "working",
  title: `task ${n}`,
  ...over,
});

const DAY = 86_400;

const dump = (
  rows: ViewRow[],
  extra: Partial<React.ComponentProps<typeof AgentViewView>> = {},
  nav: ViewNav = IDLE_VIEW_NAV,
) => {
  const stdout = Object.assign(new PassThrough(), { columns: 90, rows: 40 });
  const frames: string[] = [];

  stdout.on("data", (c) => frames.push(c.toString()));

  const view = renderSync(
    <AgentViewView
      cols={90}
      height={20}
      nav={nav}
      rows={rows}
      t={DEFAULT_THEME}
      {...extra}
    />,
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

const mixed = (): ViewRow[] => [
  row(1, { group: "input", state: "input", title: "asks a question" }),
  row(2, { current: true, model: "model-a", title: "this one" }),
  row(3, { key: "agent:id3", kind: "agent", title: "helper agent" }),
  row(4, { group: "finished", state: "done", title: "shipped" }),
  row(5, { group: "finished", state: "failed", title: "broke" }),
  row(6, {
    elapsedSeconds: null,
    group: "past",
    key: "past:id6",
    kind: "past",
    lastActive: Date.now() / 1000 - 3 * DAY,
    state: "done",
    title: "older work",
  }),
];

describe("AgentViewView", () => {
  it("groups rows under headers with the strip's glyphs and counts", () => {
    const out = dump(mixed(), {}, { confirmKey: null, index: 1 });

    console.log(`\n--- agent view dump ---\n${out}\n-----------------------`);

    const lines = out.split("\n");

    expect(lines[0]).toContain("Agents");
    expect(lines[0]).toContain(
      "1 need input · 2 working · 2 finished · 1 earlier",
    );

    const order = [
      "Needs input",
      "● asks a question",
      "Working",
      "◐ this one (this session)",
      "◐ helper agent",
      "Completed / failed",
      "✓ shipped",
      "✗ broke",
      "Earlier sessions",
      "· older work",
    ].map((s) => out.indexOf(s));

    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(out).toContain("needs input 1m 6s");
    expect(out).toContain("3d ago");
    expect(out).toContain("model-a");
    expect(out).toMatch(/› ◐ this one/);
    expect(lines.at(-1)).toBe(AGENT_VIEW_HINT);
    expect(out).toContain("↑↓ select · ⏎ attach · x stop · n new · ← back");
  });

  it("omits headers of empty groups", () => {
    const out = dump([row(1)]);

    expect(out).toContain("Working");
    expect(out).not.toContain("Needs input");
    expect(out).not.toContain("Earlier sessions");
  });

  it("shows the stop confirmation on its row", () => {
    const rows = [row(1), row(2)];
    const out = dump(rows, {}, { confirmKey: rows[1]!.key, index: 1 });

    expect(out).toMatch(/task 2.*stop\? y\/n/);
    expect(out.match(/stop\? y\/n/g)).toHaveLength(1);
  });

  it("has an empty state", () => {
    const out = dump([]);

    expect(out).toContain("No sessions yet - press n to start one");
    expect(out).toContain(AGENT_VIEW_HINT);
  });

  it("shows loading and errors", () => {
    expect(dump([], { loading: true })).toContain("loading…");
    expect(dump([row(1)], { error: "could not load" })).toContain(
      "could not load",
    );
  });

  it("scrolls long lists inside `height`, keeping the selection visible", () => {
    const rows = Array.from({ length: 30 }, (_, i) => row(i));
    const height = 8;
    const listLines = (out: string) =>
      out
        .split("\n")
        .slice(2, -2)
        .filter((l) => l.trim());

    const top = dump(rows, { height });

    expect(top).toContain("↓ 25 more");
    expect(top).not.toMatch(/↑ \d+ more/);
    expect(listLines(top).length).toBeLessThanOrEqual(height);

    const mid = dump(rows, { height }, { confirmKey: null, index: 15 });

    expect(mid).toMatch(/› ◐ task 15/);
    expect(mid).toMatch(/↑ \d+ more/);
    expect(mid).toMatch(/↓ \d+ more/);
    expect(listLines(mid).length).toBeLessThanOrEqual(height);

    const bottom = dump(rows, { height }, { confirmKey: null, index: 29 });

    expect(bottom).toMatch(/› ◐ task 29/);
    expect(bottom).toContain("↑ 24 more");
    expect(bottom).not.toMatch(/↓ \d+ more/);
    expect(listLines(bottom).length).toBeLessThanOrEqual(height);
  });
});

describe("AgentViewPane", () => {
  it("keeps the selection on its row when a poll inserts or reorders rows above it", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { id: "a", status: "working", title: "alpha" },
      { id: "b", status: "working", title: "bravo" },
    ]);

    const onActivate = vi.fn();
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    let output = "";

    stdout.on("data", (c) => {
      output += stripAnsi(c.toString());
    });

    const view = renderSync(
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={onActivate}
        onClose={() => {}}
        onNew={() => {}}
        onStop={() => {}}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );

    try {
      await waitFor(() => expect(output).toContain("› ◐ alpha"));
      output = "";
      stdin.write("\x1b[B"); // ↓ onto bravo
      await waitFor(() => expect(output).toContain("› ◐ bravo"));

      $stripSessions.set([
        { id: "c", status: "working", title: "charlie" },
        { id: "a", status: "working", title: "alpha" },
        { id: "b", status: "working", title: "bravo" },
      ]);
      await waitFor(() => expect(output).toContain("charlie"));
      stdin.write("\r");
      await waitFor(() => expect(onActivate).toHaveBeenCalled());
      expect(onActivate.mock.calls[0]![0]).toMatchObject({ id: "b" });
    } finally {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  it("fits a 30x6 terminal: nothing wraps and the list shrinks to what is left", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      {
        current: true,
        id: "cur",
        status: "working",
        title: "the session I came from",
      },
      { id: "a", status: "working", title: "alpha with a rather long title" },
      { id: "b", status: "waiting", title: "bravo needs an answer" },
      { id: "c", status: "failed", title: "charlie broke" },
    ]);

    const stdout = Object.assign(new PassThrough(), {
      columns: 30,
      isTTY: false,
      rows: 6,
    });
    const frames: string[] = [];
    // The pane sizes itself from useStdout(), which is always process.stdout.
    const saved = ["columns", "rows"].map(
      (k) => [k, Object.getOwnPropertyDescriptor(process.stdout, k)] as const,
    );

    Object.defineProperty(process.stdout, "columns", {
      configurable: true,
      value: 30,
    });
    Object.defineProperty(process.stdout, "rows", {
      configurable: true,
      value: 6,
    });

    stdout.on("data", (c) => frames.push(stripAnsi(c.toString())));

    const view = renderSync(
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={() => {}}
        onClose={() => {}}
        onNew={() => {}}
        onStop={() => {}}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: Object.assign(new PassThrough(), {
          isTTY: true,
          ref: () => {},
          setRawMode: () => {},
          unref: () => {},
        }) as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );

    try {
      await waitFor(() => expect(frames.join("")).not.toContain("loading"));

      const out = (frames.filter((f) => f.trim()).at(-1) ?? "").trimEnd();
      const lines = out.split("\n");

      // header, four list lines, footer: no spacers, no ↑/↓ indicators
      expect(lines).toHaveLength(6);
      expect(lines[0]).toContain("Agents");
      expect(lines[1]).toBe("Needs input");
      expect(lines[2]).toContain("bravo");
      expect(out).not.toMatch(/more/);
      expect(lines.at(-1)).toContain("↑↓ select");

      for (const line of lines) {
        expect(line.length).toBeLessThanOrEqual(29);
      }
    } finally {
      for (const [k, d] of saved) {
        if (d) {
          Object.defineProperty(process.stdout, k, d);
        } else {
          delete (process.stdout as unknown as Record<string, unknown>)[k];
        }
      }

      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  it("treats Ctrl/Alt+letter as chords: Ctrl+N and Alt+N start nothing, Ctrl+X arms no stop", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([{ id: "a", status: "working", title: "alpha" }]);

    const onNew = vi.fn();
    const onStop = vi.fn();
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    let output = "";
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });

    stdout.on("data", (c) => {
      output += stripAnsi(c.toString());
    });

    const view = renderSync(
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={() => {}}
        onClose={() => {}}
        onNew={onNew}
        onStop={onStop}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );
    // Long enough for Ink's 50 ms escape-sequence flush and the re-render after it.
    const settle = () => new Promise((resolve) => setTimeout(resolve, 120));

    try {
      await waitFor(() => expect(output).toContain("› ◐ alpha"));

      stdin.write("\x0e"); // Ctrl+N
      await settle();
      stdin.write("\x1bn"); // Alt+N
      await settle();
      stdin.write("\x18"); // Ctrl+X
      await settle();
      stdin.write("y");
      await settle();

      expect(onNew).not.toHaveBeenCalled();
      expect(onStop).not.toHaveBeenCalled();
      expect(output).not.toContain("stop? y/n");

      // The bare letters still are view commands.
      stdin.write("x");
      await waitFor(() => expect(output).toContain("stop? y/n"));
      stdin.write("y");
      await waitFor(() => expect(onStop).toHaveBeenCalled());
      expect(onStop.mock.calls[0]![0]).toMatchObject({ id: "a" });

      stdin.write("n");
      await waitFor(() => expect(onNew).toHaveBeenCalled());
    } finally {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  it("asks the gateway for the current project's earlier sessions only", async () => {
    resetUiState();
    patchUiState({
      info: { cwd: "/work/proj", model: "test", skills: {}, tools: {} },
      sid: "cur",
    });

    const request = vi.fn(() => Promise.resolve({ sessions: [] }));
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    const view = renderSync(
      <AgentViewPane
        gw={{ request } as unknown as GatewayClient}
        onActivate={() => {}}
        onClose={() => {}}
        onNew={() => {}}
        onStop={() => {}}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: Object.assign(new PassThrough(), {
          columns: 90,
          isTTY: false,
          rows: 30,
        }) as unknown as NodeJS.WriteStream,
      },
    );

    try {
      await waitFor(() => expect(request).toHaveBeenCalled());
      expect(request).toHaveBeenCalledWith("session.list", {
        cwd: "/work/proj",
        limit: 50,
      });
    } finally {
      view.unmount();
      view.cleanup();
      resetUiState();
    }
  });
});
