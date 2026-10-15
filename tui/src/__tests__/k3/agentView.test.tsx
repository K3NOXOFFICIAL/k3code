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
  NEW_SESSION_PLACEHOLDER,
} from "../../k3/agentView.js";
import { $stripSessions } from "../../k3/agentStripStore.js";
import {
  $collapsedGroups,
  $pastSessionsCache,
  buildViewRows,
  IDLE_VIEW_NAV,
  resetCollapsedGroups,
  toggleGroup,
  type ViewGroup,
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
  row(4, { group: "completed", state: "done", title: "shipped" }),
  row(5, { group: "completed", state: "failed", title: "broke" }),
  row(6, {
    elapsedSeconds: null,
    group: "completed",
    key: "past:id6",
    kind: "past",
    lastActive: Date.now() / 1000 - 3 * DAY,
    state: "done",
    title: "older work",
  }),
];

describe("AgentViewView", () => {
  it("groups rows under the three headers with the strip's glyphs and counts, past rows last", () => {
    // index 3: input header, "asks a question", working header, this one
    const out = dump(mixed(), {}, { confirmKey: null, index: 3 });

    console.log(`\n--- agent view dump ---\n${out}\n-----------------------`);

    const lines = out.split("\n");

    expect(lines[0]).toContain("Agents");
    expect(lines[0]).toContain("2 working · 1 needs input · 3 completed");
    expect(lines.filter((l) => l.includes("Completed (3)"))).toHaveLength(1);
    expect(out).not.toContain("Earlier sessions");
    expect(out).not.toContain("Idle");

    const order = [
      "▾ Needs input (1)",
      "● asks a question",
      "▾ Working (2)",
      "◐ this one (this session)",
      "◐ helper agent",
      "▾ Completed (3)",
      "✓ shipped",
      "✗ broke",
      "· older work",
    ].map((s) => out.indexOf(s));

    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(out).toContain("needs input 1m 6s");
    expect(out).toContain("3d ago");
    expect(out).toContain("model-a");
    expect(out).toMatch(/› ◐ this one/);
    expect(lines.at(-1)).toBe(AGENT_VIEW_HINT);
    expect(out).toContain(
      "↑↓ select · →/⏎ attach · ⏎/space fold group · x stop · n new · ←/esc back",
    );
  });

  it("shows a session with no turn yet as idle (○) inside Completed, not as completed (✓)", () => {
    const out = dump([
      row(1, { title: "busy" }),
      row(2, {
        current: true,
        elapsedSeconds: null,
        group: "completed",
        state: "idle",
        title: "Session",
      }),
      row(3, { group: "completed", state: "done", title: "shipped" }),
    ]);
    const lines = out.split("\n");

    expect(lines[0]).toContain("1 working · 2 completed");
    expect(out).toMatch(/○ Session \(this session\)\s+idle\s*$/m);
    expect(out).not.toMatch(/Session \(this session\)\s+completed/);

    const order = [
      "Working (1)",
      "◐ busy",
      "Completed (2)",
      "○ Session",
      "✓ shipped",
    ].map((s) => out.indexOf(s));

    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  it("lists every group header, an empty one with (0)", () => {
    const out = dump([row(1)]);

    expect(out).toContain("▾ Working (1)");
    expect(out).toContain("▾ Needs input (0)");
    expect(out).toContain("▾ Completed (0)");
  });

  it("a collapsed group shows only its header, marked ▸, and keeps its count", () => {
    const out = dump(mixed(), {
      collapsed: new Set<ViewGroup>(["completed"]),
    });

    expect(out).toContain("▸ Completed (3)");
    expect(out).not.toContain("shipped");
    expect(out).not.toContain("older work");
    expect(out).toContain("▾ Working (2)");
    expect(out).toContain("helper agent");
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
    expect(out).toContain("Working (0)");
    expect(out).toContain(AGENT_VIEW_HINT);
  });

  it("shows loading and errors", () => {
    const loading = dump([], { loading: true });

    expect(loading).toContain("loading…");
    expect(loading).not.toContain("No sessions yet");
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

    // 33 lines: the three headers and 30 rows; the window shows 6 of them plus the two indicators.
    const top = dump(rows, { height });

    expect(top).toContain("↓ 27 more");
    expect(top).not.toMatch(/↑ \d+ more/);
    expect(listLines(top).length).toBeLessThanOrEqual(height);

    // item 17: input header, working header, then task 0..14 → task 15
    const mid = dump(rows, { height }, { confirmKey: null, index: 17 });

    expect(mid).toMatch(/› ◐ task 15/);
    expect(mid).toMatch(/↑ \d+ more/);
    expect(mid).toMatch(/↓ \d+ more/);
    expect(listLines(mid).length).toBeLessThanOrEqual(height);

    const bottom = dump(rows, { height }, { confirmKey: null, index: 32 });

    expect(bottom).toMatch(/› ▾ Completed \(0\)/);
    expect(bottom).toMatch(/◐ task 29/);
    expect(bottom).toContain("↑ 27 more");
    expect(bottom).not.toMatch(/↓ \d+ more/);
    expect(listLines(bottom).length).toBeLessThanOrEqual(height);
  });
});

describe("buildViewRows", () => {
  it("times this session's working turn from turn_started_at, not from the session's creation", () => {
    // The reported bug: a session created ~23h ago read "working 1370m 20s" five minutes into the app.
    const nowMs = Date.now();
    const rows = buildViewRows({
      currentSid: "cur",
      nowMs,
      past: [],
      sessions: [
        {
          current: true,
          id: "cur",
          started_at: nowMs / 1000 - 1370 * 60,
          status: "working",
          title: "Session",
          turn_started_at: nowMs / 1000 - 20,
        },
      ],
      subagents: [],
    });
    const out = dump(rows);

    expect(out).toMatch(/◐ Session \(this session\)\s+working 20s/);
    expect(out).not.toContain("1370m");
  });
});

describe("AgentViewPane", () => {
  it("opens on this session's row; → closes there and attaches elsewhere", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    // alpha (needs input) is listed above this session, so the first row is not the current one.
    $stripSessions.set([
      { id: "a", status: "waiting", title: "alpha" },
      { current: true, id: "cur", status: "working", title: "this one" },
    ]);

    const onActivate = vi.fn();
    const onClose = vi.fn();
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
        onClose={onClose}
        onNew={() => {}}
        onNewPrompt={() => {}}
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
      await waitFor(() =>
        expect(output).toContain("› ◐ this one (this session)"),
      );
      expect(output).not.toContain("› ● alpha");
      expect(output).toContain(AGENT_VIEW_HINT);

      stdin.write("\x1b[C"); // → on this session
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(onActivate).not.toHaveBeenCalled();

      output = "";
      stdin.write("\x1b[A"); // ↑ onto the Working header
      await waitFor(() => expect(output).toContain("› ▾ Working (1)"));
      stdin.write("\x1b[A"); // ↑ onto alpha
      await waitFor(() => expect(output).toContain("› ● alpha"));
      stdin.write("\x1b[C");
      await waitFor(() => expect(onActivate).toHaveBeenCalled());
      expect(onActivate.mock.calls[0]![0]).toMatchObject({ id: "a" });
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

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
        onNewPrompt={() => {}}
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
        onNewPrompt={() => {}}
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
      const lastFrame = () =>
        (frames.filter((f) => f.trim()).at(-1) ?? "").trimEnd();

      // A positive marker: the list and the footer are painted (the loading text never shows at this width).
      await waitFor(() => {
        expect(lastFrame()).toContain("↑↓ select");
        expect(lastFrame()).toContain("bravo");
      });

      const out = lastFrame();
      const lines = out.split("\n");

      // header, four list lines, footer: no spacers, no ↑/↓ indicators
      expect(lines).toHaveLength(6);
      expect(lines[0]).toContain("Agents");
      expect(lines[1]).toBe("  ▾ Needs input (1)");
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
    // bravo is the sentinel: moving onto it and back proves the chords before it were dispatched.
    $stripSessions.set([
      { id: "a", status: "working", title: "alpha" },
      { id: "b", status: "working", title: "bravo" },
    ]);

    const onNew = vi.fn();
    const onStop = vi.fn();
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    // `output` keeps every frame (the absence check reads all of them); `fresh` only what came after a reset.
    let output = "";
    let fresh = "";
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });

    stdout.on("data", (c) => {
      const text = stripAnsi(c.toString());

      output += text;
      fresh += text;
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
        onNewPrompt={() => {}}
        onStop={onStop}
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

      stdin.write("\x0e"); // Ctrl+N
      stdin.write("\x1bn"); // Alt+N
      stdin.write("\x18"); // Ctrl+X
      stdin.write("y");

      // Sentinel: ↓ then ↑, each waited for on output captured after it was sent.
      fresh = "";
      stdin.write("\x1b[B");
      await waitFor(() => expect(fresh).toContain("› ◐ bravo"));
      fresh = "";
      stdin.write("\x1b[A");
      await waitFor(() => expect(fresh).toContain("› ◐ alpha"));

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
      await waitFor(() => expect(output).toContain(NEW_SESSION_PLACEHOLDER));
      stdin.write("\r"); // an empty draft: a plain new session
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
        onNewPrompt={() => {}}
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

  /** Mount the pane against `gw`, with a 90x30 terminal; returns the captured output and the stdin to type into. */
  const mount = (
    request: (method: string, params: unknown) => Promise<unknown>,
    handlers: Partial<React.ComponentProps<typeof AgentViewPane>> = {},
  ) => {
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });
    const out = { text: "" };
    const frames: string[] = [];

    stdout.on("data", (c) => {
      const text = stripAnsi(c.toString());
      out.text += text;
      frames.push(text);
    });

    const view = renderSync(
      <AgentViewPane
        gw={{ request } as unknown as GatewayClient}
        onActivate={() => {}}
        onClose={() => {}}
        onNew={() => {}}
        onNewPrompt={() => {}}
        onStop={() => {}}
        {...handlers}
      />,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );

    return {
      /** The last full frame: Ink repaints the whole pane, so this is what the terminal shows. */
      last: () => frames.filter((f) => f.trim()).at(-1) ?? "",
      out,
      stdin,
      unmount: () => {
        view.unmount();
        view.cleanup();
      },
    };
  };

  it("renders the live rows and the cached history before session.list answers", async () => {
    resetUiState();
    patchUiState({
      info: { cwd: "/work/proj", model: "test", skills: {}, tools: {} },
      sid: "cur",
    });
    $stripSessions.set([
      { current: true, id: "cur", status: "working", title: "this one" },
    ]);
    $pastSessionsCache.set({
      cwd: "/work/proj",
      rows: [
        {
          cwd: "/work/proj",
          id: "old1",
          message_count: 3,
          started_at: 1,
          title: "cached earlier work",
        },
      ],
    });

    // The gateway never answers: everything shown comes from what the TUI already knew.
    const pane = mount(() => new Promise(() => {}));

    try {
      await waitFor(() => {
        expect(pane.out.text).toContain("› ◐ this one (this session)");
        expect(pane.out.text).toContain("cached earlier work");
      });
      expect(pane.out.text).toContain("loading…");
      expect(pane.out.text).not.toContain("Loading sessions");
    } finally {
      pane.unmount();
      $stripSessions.set([]);
      $pastSessionsCache.set(null);
      resetUiState();
    }
  });

  it("replaces the cached history with the fetched one and keeps it for the next open", async () => {
    resetUiState();
    patchUiState({
      info: { cwd: "/work/proj", model: "test", skills: {}, tools: {} },
      sid: "cur",
    });
    $pastSessionsCache.set({
      cwd: "/other/proj",
      rows: [
        { cwd: "/other/proj", id: "x", message_count: 1, title: "elsewhere" },
      ],
    });
    const fetched = {
      sessions: [
        {
          cwd: "/work/proj",
          id: "f1",
          message_count: 2,
          started_at: 5,
          title: "fetched",
        },
      ],
    };
    const pane = mount(() => Promise.resolve(fetched));

    try {
      await waitFor(() => expect(pane.out.text).toContain("fetched"));
      // Another project's cache is not shown here.
      expect(pane.out.text).not.toContain("elsewhere");
      expect($pastSessionsCache.get()).toEqual({
        cwd: "/work/proj",
        rows: fetched.sessions,
      });
    } finally {
      pane.unmount();
      $pastSessionsCache.set(null);
      resetUiState();
    }
  });

  it("n opens the prompt; Enter starts a session with the text, Esc cancels", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { current: true, id: "cur", status: "working", title: "this one" },
    ]);
    const onNew = vi.fn();
    const onNewPrompt = vi.fn();
    const onClose = vi.fn();
    const pane = mount(() => Promise.resolve({ sessions: [] }), {
      onClose,
      onNew,
      onNewPrompt,
    });

    try {
      await waitFor(() => expect(pane.out.text).toContain("› ◐ this one"));

      pane.stdin.write("n");
      await waitFor(() =>
        expect(pane.out.text).toContain(NEW_SESSION_PLACEHOLDER),
      );
      pane.stdin.write("fix the build");
      await waitFor(() =>
        expect(pane.out.text).toContain("new › fix the build"),
      );
      // While typing, the letters are text, not view commands: no stop prompt, no close.
      expect(pane.out.text).not.toContain("stop? y/n");
      expect(onClose).not.toHaveBeenCalled();

      pane.stdin.write("\r");
      await waitFor(() =>
        expect(onNewPrompt).toHaveBeenCalledWith("fix the build"),
      );
      expect(onNew).not.toHaveBeenCalled();

      // The draft closes with Enter; a second n opens a fresh one and Esc only cancels it.
      pane.out.text = "";
      pane.stdin.write("n");
      await waitFor(() =>
        expect(pane.out.text).toContain(NEW_SESSION_PLACEHOLDER),
      );
      pane.stdin.write("abc");
      await waitFor(() => expect(pane.out.text).toContain("new › abc"));
      pane.out.text = "";
      pane.stdin.write("\x1b");
      await waitFor(() => expect(pane.out.text).toContain(AGENT_VIEW_HINT));
      expect(onClose).not.toHaveBeenCalled();
      expect(onNewPrompt).toHaveBeenCalledTimes(1);

      // Esc on the list, with no draft, closes the view.
      pane.stdin.write("\x1b");
      await waitFor(() => expect(onClose).toHaveBeenCalled());
    } finally {
      pane.unmount();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  const foldFixture = () => {
    resetCollapsedGroups();
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { current: true, id: "cur", status: "working", title: "this one" },
      { id: "a", status: "working", title: "alpha" },
      { id: "d", status: "failed", title: "delta" },
    ]);
  };

  it("space on a header folds the group and ↑↓ skip its rows", async () => {
    foldFixture();
    const onActivate = vi.fn();
    const pane = mount(() => Promise.resolve({ sessions: [] }), {
      onActivate,
    });

    try {
      await waitFor(() => expect(pane.last()).toContain("› ◐ this one"));
      pane.stdin.write("\x1b[A"); // ↑ onto the Working header
      await waitFor(() => expect(pane.last()).toContain("› ▾ Working (2)"));
      pane.stdin.write(" ");
      await waitFor(() => expect(pane.last()).toContain("› ▸ Working (2)"));
      expect(pane.last()).not.toContain("alpha");
      expect(pane.last()).toContain("▾ Needs input (0)");

      // ↓ from the folded header lands on the Completed header, not on a hidden row.
      pane.stdin.write("\x1b[B");
      await waitFor(() => expect(pane.last()).toContain("› ▾ Completed (1)"));
      pane.stdin.write("\x1b[B");
      await waitFor(() => expect(pane.last()).toContain("› ✗ delta"));
      pane.stdin.write("\r");
      await waitFor(() => expect(onActivate).toHaveBeenCalled());
      expect(onActivate.mock.calls[0]![0]).toMatchObject({ id: "d" });
    } finally {
      pane.unmount();
      $stripSessions.set([]);
      resetCollapsedGroups();
      resetUiState();
    }
  });

  it("keeps a fold across opens; Enter on the header unfolds it", async () => {
    foldFixture();
    toggleGroup("working");
    const pane = mount(() => Promise.resolve({ sessions: [] }));

    try {
      // The current row is hidden, so the cursor rests on the first visible row.
      await waitFor(() => expect(pane.last()).toContain("› ✗ delta"));
      expect(pane.last()).toContain("▸ Working (2)");
      expect(pane.last()).not.toContain("alpha");
      pane.stdin.write("\x1b[A");
      await waitFor(() => expect(pane.last()).toContain("› ▾ Completed (1)"));
      pane.stdin.write("\x1b[A");
      await waitFor(() => expect(pane.last()).toContain("› ▸ Working (2)"));
      pane.stdin.write("\r");
      await waitFor(() => expect(pane.last()).toContain("› ▾ Working (2)"));
      expect(pane.last()).toContain("alpha");
      expect($collapsedGroups.get().size).toBe(0);
    } finally {
      pane.unmount();
      $stripSessions.set([]);
      resetCollapsedGroups();
      resetUiState();
    }
  });
});
