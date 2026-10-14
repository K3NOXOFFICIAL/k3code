import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it, vi } from "vitest";

import { patchUiState, resetUiState } from "../../app/uiStore.js";
import type { GatewayClient } from "../../gatewayClient.js";
import {
  AGENT_VIEW_HINT,
  AGENT_VIEW_HINT_WIDE,
  AgentViewPane,
  AgentViewView,
} from "../../k3/agentView.js";
import { $stripSessions } from "../../k3/agentStripStore.js";
import type { SessionActiveItem } from "../../gatewayTypes.js";
import {
  buildViewRows,
  getAgentViewDraft,
  IDLE_VIEW_NAV,
  setAgentViewDraft,
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
  const stdout = Object.assign(new PassThrough(), {
    columns: Math.max(90, extra.cols ?? 0),
    rows: 40,
  });
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
    const out = dump(mixed(), {}, { confirmKey: null, index: 1 });

    console.log(`\n--- agent view dump ---\n${out}\n-----------------------`);

    const lines = out.split("\n");

    expect(lines[0]).toContain("Agents");
    expect(lines[0]).toContain("1 need input · 2 working · 3 completed");
    expect(lines.filter((l) => l === "Completed")).toHaveLength(1);
    expect(out).not.toContain("Earlier sessions");
    expect(out).not.toContain("Idle");

    const order = [
      "Needs input",
      "● asks a question",
      "Working",
      "◐ this one (this session)",
      "◐ helper agent",
      "Completed",
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
    expect(out).toContain("↑↓ select · ^N new · ^X stop · esc back");
    expect(out).not.toContain("/rename");
    // A terminal wide enough for it also lists the view's commands.
    expect(dump(mixed(), { cols: 140 }).split("\n").at(-1)).toBe(
      AGENT_VIEW_HINT_WIDE,
    );
  });

  it("keeps the hint inside 80 columns", () => {
    expect(AGENT_VIEW_HINT.length).toBeLessThanOrEqual(79);
    expect(
      dump([row(1)], { cols: 79 })
        .split("\n")
        .at(-1),
    ).toBe(AGENT_VIEW_HINT);
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
      "Working",
      "◐ busy",
      "Completed",
      "○ Session",
      "✓ shipped",
    ].map((s) => out.indexOf(s));

    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });

  it("omits headers of empty groups", () => {
    const out = dump([row(1)]);

    expect(out).toContain("Working");
    expect(out).not.toContain("Needs input");
    expect(out).not.toContain("Completed");
  });

  it("shows the stop confirmation on its row", () => {
    const rows = [row(1), row(2)];
    const out = dump(rows, {}, { confirmKey: rows[1]!.key, index: 1 });

    expect(out).toMatch(/task 2.*stop\? y\/n/);
    expect(out.match(/stop\? y\/n/g)).toHaveLength(1);
  });

  it("has an empty state", () => {
    const out = dump([]);

    expect(out).toContain("No sessions yet - press Ctrl+N to start one");
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

/** A mounted AgentViewPane on alpha and bravo (no current row listed), fed through a fake TTY. */
const mountPane = (
  props: Partial<React.ComponentProps<typeof AgentViewPane>> = {},
  sessions: SessionActiveItem[] = [
    { id: "a", status: "working", title: "alpha" },
    { id: "b", status: "working", title: "bravo" },
  ],
) => {
  resetUiState();
  setAgentViewDraft("");
  patchUiState({ sid: "cur" });
  $stripSessions.set(sessions);

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
  const frames: string[] = [];

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
      {...props}
    />,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  return {
    done: () => {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      setAgentViewDraft("");
      resetUiState();
    },
    last: () => (frames.filter((f) => f.trim()).at(-1) ?? "").trimEnd(),
    output: () => frames.join(""),
    stdin,
  };
};

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
      const lastFrame = () =>
        (frames.filter((f) => f.trim()).at(-1) ?? "").trimEnd();

      // A positive marker: the list and the composer are painted (the loading text never shows at this width).
      await waitFor(() => {
        expect(lastFrame()).toContain("message this session");
        expect(lastFrame()).toContain("bravo");
      });

      const out = lastFrame();
      const lines = out.split("\n");

      // header, four list lines, composer: no spacers, no ↑/↓ indicators, no hint
      expect(lines).toHaveLength(6);
      expect(lines[0]).toContain("Agents");
      expect(lines[1]).toBe("Needs input");
      expect(lines[2]).toContain("bravo");
      expect(out).not.toMatch(/more/);
      expect(lines.at(-1)).toMatch(/^› message this session/);
      expect(out).not.toContain("↑↓ select");

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

  it("Ctrl+N starts a new session and Ctrl+X arms the stop; bare n, x and y type into the composer", async () => {
    const onNew = vi.fn();
    const onStop = vi.fn();
    const p = mountPane({ onNew, onStop });

    try {
      await waitFor(() => expect(p.output()).toContain("› ◐ alpha"));

      p.stdin.write("nxy");
      await waitFor(() => expect(getAgentViewDraft()).toBe("nxy"));
      await waitFor(() => expect(p.last()).toContain("› nxy"));
      expect(onNew).not.toHaveBeenCalled();
      expect(onStop).not.toHaveBeenCalled();
      expect(p.output()).not.toContain("stop? y/n");

      p.stdin.write("\x15"); // Ctrl+U empties the composer
      await waitFor(() => expect(getAgentViewDraft()).toBe(""));

      p.stdin.write("\x18"); // Ctrl+X
      await waitFor(() => expect(p.last()).toContain("stop? y/n"));
      p.stdin.write("y");
      await waitFor(() => expect(onStop).toHaveBeenCalled());
      expect(onStop.mock.calls[0]![0]).toMatchObject({ id: "a" });
      // The composer was unfocused while the confirmation was up: y was not typed.
      await waitFor(() => expect(p.last()).not.toContain("stop? y/n"));
      expect(getAgentViewDraft()).toBe("");
      expect(p.last()).not.toMatch(/^› y/m);

      p.stdin.write("\x0e"); // Ctrl+N
      await waitFor(() => expect(onNew).toHaveBeenCalled());
    } finally {
      p.done();
    }
  });

  it("types into the composer without moving the selection; ↓/↑ still move, onto a headerless + New agent", async () => {
    const p = mountPane({}, [
      { current: true, id: "cur", status: "working", title: "this one" },
      { id: "a", status: "working", title: "alpha" },
    ]);

    try {
      await waitFor(() => expect(p.output()).toContain("› ◐ this one"));
      p.stdin.write("hello");
      await waitFor(() => expect(p.last()).toContain("› hello"));
      expect(p.last()).toContain("› ◐ this one");
      expect(getAgentViewDraft()).toBe("hello");

      p.stdin.write("\x1b[B"); // ↓ with a draft
      await waitFor(() => expect(p.last()).toContain("› ◐ alpha"));
      expect(getAgentViewDraft()).toBe("hello");

      // One key per render: the view reads its selection from the last render.
      p.stdin.write("\x1b[A");
      await waitFor(() => expect(p.last()).toContain("› ◐ this one"));
      p.stdin.write("\x1b[A"); // onto + New agent
      await waitFor(() => expect(p.last()).toContain("› + New agent"));
      p.stdin.write("\x1b[A"); // already at the top
      await waitFor(() => expect(p.last()).toContain("› + New agent"));
      p.stdin.write("\x1b[B");
      await waitFor(() => expect(p.last()).toContain("› ◐ this one"));

      const lines = p.last().split("\n");
      const newLine = lines.findIndex((l) => l.includes("+ New agent"));

      // No header of its own: the line above it is the spacer under the title line.
      expect(newLine).toBe(2);
      expect(lines[1]!.trim()).toBe("");
      expect(lines[3]).toBe("Working");
      expect(getAgentViewDraft()).toBe("hello");
    } finally {
      p.done();
    }
  });

  it("sends text on + New agent as a background agent, shows the notice and clears it on the next key", async () => {
    const onDispatch = vi.fn(() =>
      Promise.resolve({
        text: "started abcd1234 in the background",
        tone: "info" as const,
      }),
    );
    const p = mountPane({ onDispatch });

    try {
      await waitFor(() => expect(p.output()).toContain("› ◐ alpha"));
      p.stdin.write("\x1b[A"); // onto + New agent
      await waitFor(() => expect(p.last()).toContain("› + New agent"));
      p.stdin.write("ship it");
      await waitFor(() => expect(getAgentViewDraft()).toBe("ship it"));
      p.stdin.write("\r");
      await waitFor(() =>
        expect(p.last()).toContain("started abcd1234 in the background"),
      );
      expect(onDispatch).toHaveBeenCalledWith("ship it");
      expect(getAgentViewDraft()).toBe("");

      p.stdin.write("\x1b[B");
      await waitFor(() => expect(p.last()).toContain("› ◐ alpha"));
      expect(p.last()).not.toContain("started abcd1234");
    } finally {
      p.done();
    }
  });

  it("/delete asks first; only y deletes", async () => {
    const onDelete = vi.fn(() =>
      Promise.resolve({ text: "deleted alpha", tone: "info" as const }),
    );
    const p = mountPane({ onDelete });

    try {
      await waitFor(() => expect(p.output()).toContain("› ◐ alpha"));
      p.stdin.write("/delete");
      await waitFor(() => expect(getAgentViewDraft()).toBe("/delete"));
      p.stdin.write("\r");
      await waitFor(() => expect(p.last()).toContain("delete? y/n"));
      expect(onDelete).not.toHaveBeenCalled();
      p.stdin.write("y");
      await waitFor(() => expect(onDelete).toHaveBeenCalled());
      expect(onDelete.mock.calls[0]![0]).toMatchObject({ id: "a" });
    } finally {
      p.done();
    }
  });

  it("← closes with an empty draft, but a letter and ← in one burst edit the text instead", async () => {
    const onClose = vi.fn();
    const p = mountPane({ onClose });

    try {
      await waitFor(() => expect(p.output()).toContain("› ◐ alpha"));
      p.stdin.write("\x1b[D");
      await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

      // One chunk: the composer applies "a" and flushes the draft before the view decides about ←.
      p.stdin.write("a\x1b[D");
      p.stdin.write("b"); // lands before "a" only if ← moved the text cursor
      await waitFor(() => expect(getAgentViewDraft()).toBe("ba"));
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      p.done();
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
