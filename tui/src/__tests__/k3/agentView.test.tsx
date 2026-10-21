import { PassThrough } from "node:stream";

import { AlternateScreen, renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it, vi } from "vitest";

import { patchUiState, resetUiState } from "../../app/uiStore.js";
import type { GatewayClient } from "../../gatewayClient.js";
import {
  AGENT_VIEW_ARCHIVE_HINT,
  AGENT_VIEW_HINT,
  AGENT_VIEW_INPUT_HINT,
  AGENT_VIEW_RESTORE_HINT,
  AgentViewPane,
  AgentViewView,
} from "../../k3/agentView.js";
import { $stripSessions } from "../../k3/agentStripStore.js";
import {
  buildViewRows,
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
  nav: ViewNav = { ...IDLE_VIEW_NAV, collapsedGroups: [] },
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
    const out = dump(mixed(), {}, { ...IDLE_VIEW_NAV, index: 1 });

    const lines = out.split("\n");

    expect(lines[0]).toContain("Agents");
    expect(lines[0]).toContain("1 need input · 2 working · 3 completed");
    expect(lines.filter((l) => l.includes("Completed"))).toHaveLength(1);
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
    expect(out).toContain(
      "↑↓ move · ⏎/→ open · space/c collapse · x stop · tab/type compose · ←/esc back",
    );
    // The pane keeps one column free, so the full hint must fit the 79 a default 80-column terminal leaves.
    expect(AGENT_VIEW_HINT.length).toBeLessThanOrEqual(79);
    // a permanent composer row sits between the list and the hint; while the
    // list has focus the composer only shows its placeholder
    expect(out).toContain("❯ ");
    expect(out).not.toContain(AGENT_VIEW_INPUT_HINT);
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
    const out = dump(
      rows,
      {},
      { ...IDLE_VIEW_NAV, confirmKey: rows[1]!.key, index: 1 },
    );

    expect(out).toMatch(/task 2.*stop\? y\/n/);
    expect(out.match(/stop\? y\/n/g)).toHaveLength(1);
  });

  it("has an empty state", () => {
    const out = dump([]);

    expect(out).toContain("No sessions yet - type a prompt below to start one");
    expect(out).toContain(AGENT_VIEW_HINT);
  });

  it("shows loading and errors", () => {
    expect(dump([], { loading: true })).toContain("loading…");
    expect(dump([row(1)], { error: "could not load" })).toContain(
      "could not load",
    );
  });

  it("shows Archived collapsed by default: ▸, its count and no rows; opened, its rows follow the header", () => {
    const archivedRow = (n: number, title: string) =>
      row(n, {
        elapsedSeconds: null,
        group: "archived",
        key: `past:id${n}`,
        kind: "past",
        state: "done",
        title,
      });
    const rows = [
      ...mixed(),
      archivedRow(7, "put away"),
      archivedRow(8, "old"),
    ];

    const shut = dump(rows, {}, { ...IDLE_VIEW_NAV, index: 1 });

    expect(shut.split("\n")[0]).toContain(
      "1 need input · 2 working · 3 completed · 2 archived",
    );
    expect(shut).toMatch(/▸ Archived {2}2/);
    expect(shut).not.toContain("put away");
    expect(shut.indexOf("· older work")).toBeLessThan(shut.indexOf("Archived"));

    const opened = dump(rows, {}, { ...IDLE_VIEW_NAV, collapsedGroups: [] });

    expect(opened).toMatch(/▾ Archived {2}2/);
    expect(opened.indexOf("Archived")).toBeLessThan(
      opened.indexOf("· put away"),
    );
  });

  it("names the row action that applies in the hint, each inside 80 columns", () => {
    const rows = [
      row(1),
      row(2, { group: "completed", key: "past:id2", kind: "past" }),
      row(3, { group: "archived", key: "past:id3", kind: "past" }),
    ];
    const hint = (index: number) =>
      dump(rows, { cols: 79 }, { ...IDLE_VIEW_NAV, collapsedGroups: [], index })
        .split("\n")
        .at(-1);

    expect(hint(0)).toBe(AGENT_VIEW_HINT);
    expect(hint(1)).toBe(AGENT_VIEW_ARCHIVE_HINT);
    expect(hint(2)).toBe(AGENT_VIEW_RESTORE_HINT);
    expect(AGENT_VIEW_ARCHIVE_HINT).toContain("a archive");
    expect(AGENT_VIEW_RESTORE_HINT).toContain("u restore");

    for (const h of [
      AGENT_VIEW_HINT,
      AGENT_VIEW_ARCHIVE_HINT,
      AGENT_VIEW_RESTORE_HINT,
    ]) {
      expect(h.length).toBeLessThanOrEqual(79);
    }
  });

  it("scrolls long lists inside `height`, keeping the selection visible", () => {
    const rows = Array.from({ length: 30 }, (_, i) => row(i));
    const height = 8;
    const listLines = (out: string) =>
      out
        .split("\n")
        .filter(
          (l) =>
            l.trim() &&
            !/(↑|↓) \d+ more/.test(l) &&
            !l.includes("❯") &&
            !l.includes("↑↓ move") &&
            !l.includes("Agents"),
        );

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

  it("gives the list its whole `height`, so the composer and the hint end the frame however few rows there are", () => {
    for (const rows of [[row(1)], []]) {
      const lines = dump(rows, { height: 12 }).split("\n");

      // header, spacer, the 12 list lines, spacer, composer, hint
      expect(lines).toHaveLength(1 + 1 + 12 + 1 + 1 + 1);
      expect(lines.at(-2)).toContain("❯");
      expect(lines.at(-1)).toBe(AGENT_VIEW_HINT);
    }
  });

  it("keeps its own height when not pinned (inline mode): the footer follows the list", () => {
    const lines = dump([row(1)], { height: 12, pin: false }).split("\n");

    // header, spacer, Working header, the row, spacer, composer, hint
    expect(lines).toHaveLength(7);
    expect(lines.at(-2)).toContain("❯");
    expect(lines.at(-1)).toBe(AGENT_VIEW_HINT);
  });

  it("highlights nothing as a row while a header holds the selection", () => {
    const out = dump(
      [row(1), row(2)],
      {},
      { ...IDLE_VIEW_NAV, header: "working", index: 0 },
    );

    expect(out).toContain("▾ Working");
    expect(out).not.toMatch(/›/);
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
      // The composer has the keyboard on open; ↑ hands it to the list without moving the selection.
      expect(output).toContain(AGENT_VIEW_INPUT_HINT);
      output = "";
      stdin.write("\x1b[A");
      await waitFor(() => expect(output).toContain(AGENT_VIEW_HINT));
      expect(output).toContain("› ◐ this one (this session)");

      stdin.write("\x1b[C"); // → on this session
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(onActivate).not.toHaveBeenCalled();

      output = "";
      stdin.write("\x1b[A"); // ↑ stops on the Working header first (a header is a stop of its own)
      await new Promise((r) => setTimeout(r, 50));
      expect(output).not.toContain("› ● alpha");
      stdin.write("\x1b[A"); // ↑ onto alpha, the last row of the group above
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
      stdin.write("\x1b[A"); // the list takes the keyboard from the composer
      await waitFor(() => expect(output).toContain(AGENT_VIEW_HINT));
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
        expect(lastFrame()).toContain("⏎ start");
        expect(lastFrame()).toContain("bravo");
      });

      const out = lastFrame();
      const lines = out.split("\n");

      // header, the three list rows that fit, composer, hint: no spacers, no ↑/↓
      // indicators — the needs-input group's header is what got scrolled away
      expect(lines).toHaveLength(6);
      expect(lines[0]).toContain("Agents");
      expect(lines[1]).toContain("bravo");
      expect(lines[2]).toContain("Working");
      expect(lines[3]).toContain("(this session)");
      expect(out).not.toMatch(/more/);
      expect(lines.at(-2)).toContain("❯");
      expect(lines.at(-1)).toContain("⏎ start");

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

  // Mounts the connected pane in a `cols`x`rows` terminal. The pane sizes itself from useStdout(), which is always
  // process.stdout, so that gets the size too (restored by `done`).
  // `paneRows` is the height process.stdout reports (what the pane believes); `rows` is the real, rendered terminal,
  // which only differs while a resize has not reached the pane. `altScreen` wraps the pane in the full-screen shell the
  // app puts it in, which pins the frame to the terminal's height.
  const mountPane = (
    cols: number,
    rows: number,
    over: Partial<React.ComponentProps<typeof AgentViewPane>> = {},
    { altScreen = false, paneRows = rows } = {},
  ) => {
    const stdout = Object.assign(new PassThrough(), {
      columns: cols,
      isTTY: false,
      rows,
    });
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    const frames: string[] = [];
    const saved = ["columns", "rows"].map(
      (k) => [k, Object.getOwnPropertyDescriptor(process.stdout, k)] as const,
    );

    Object.defineProperty(process.stdout, "columns", {
      configurable: true,
      value: cols,
    });
    Object.defineProperty(process.stdout, "rows", {
      configurable: true,
      value: paneRows,
    });
    stdout.on("data", (c) => frames.push(stripAnsi(c.toString())));

    const pane = (
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={() => {}}
        onClose={() => {}}
        onCommand={() => {}}
        onNewPrompt={() => {}}
        onStop={() => {}}
        {...over}
      />
    );
    const view = renderSync(
      altScreen ? <AlternateScreen>{pane}</AlternateScreen> : pane,
      {
        patchConsole: false,
        stderr: new PassThrough() as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream,
      },
    );

    return {
      done: () => {
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
      },
      // Frames only: the renderer also writes terminal queries, which strip down to stray text such as "0q".
      lastFrame: () =>
        (frames.filter((f) => f.includes("Agents")).at(-1) ?? "").trimEnd(),
      press: (keys: string) => stdin.write(keys),
      /**
       * The terminal changes height as the pane reads it: process.stdout.rows, then 'resize' on process.stdout. The
       * rendering stdout is left alone so only the pane's own listener can refresh it (Ink's would re-render it too).
       */
      resize: (toRows: number) => {
        Object.defineProperty(process.stdout, "rows", {
          configurable: true,
          value: toRows,
        });
        process.stdout.emit("resize");
      },
    };
  };

  it("pins the composer and the hint to the last two rows of the terminal, for a short, an exactly fitting and a long list", async () => {
    // At 20 rows the list area is 15 lines: 14 sessions plus the Working header fill it exactly, which used to push the
    // frame one line past the terminal.
    for (const count of [2, 14, 40]) {
      resetUiState();
      patchUiState({ sid: "cur" });
      $stripSessions.set(
        Array.from({ length: count }, (_, i) => ({
          id: `s${i}`,
          status: "working" as const,
          title: `task ${i}`,
        })),
      );

      const pane = mountPane(90, 20);

      try {
        await waitFor(() => {
          expect(pane.lastFrame()).toContain("task 0");
          expect(pane.lastFrame()).toContain("⏎ start");
        });

        const lines = pane.lastFrame().split("\n");

        // The frame is exactly as tall as the terminal: nothing scrolls off the top, and the composer is not left
        // hanging under the list.
        expect(lines).toHaveLength(20);
        expect(lines[0]).toContain("Agents");
        expect(lines.at(-2)).toContain("❯");
        expect(lines.at(-1)).toContain("⏎ start");
      } finally {
        pane.done();
      }
    }
  });

  it("inline mode is not pinned and stays a row short of the terminal, so the frame never scrolls", async () => {
    // At 20 rows the inline list area is 14 lines: 13 sessions plus the Working header fill it exactly, and 14 are one
    // too many for it (a list area sized for a pinned frame would make 20 lines here).
    // A short list keeps its own height (8 lines), not the screen's.
    for (const [count, maxLines] of [
      [2, 10],
      [13, 19],
      [14, 19],
      [40, 19],
    ] as const) {
      resetUiState();
      patchUiState({ sid: "cur" });
      $stripSessions.set(
        Array.from({ length: count }, (_, i) => ({
          id: `s${i}`,
          status: "waiting" as const,
          title: `task ${i}`,
        })),
      );

      const pane = mountPane(90, 20, { inline: true });

      try {
        await waitFor(() => {
          expect(pane.lastFrame()).toContain("task 0");
          expect(pane.lastFrame()).toContain("⏎ start");
        });

        const lines = pane.lastFrame().split("\n");

        // Never as tall as the terminal (that scrolls a row into scrollback), and the footer still ends it.
        expect(lines.length).toBeLessThanOrEqual(maxLines);
        expect(lines.at(-2)).toContain("❯");
        expect(lines.at(-1)).toContain("⏎ start");
      } finally {
        pane.done();
      }
    }
  });

  it("a header whose group disappears does not take the selection back when the group returns", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });

    const alpha = { id: "a", status: "waiting" as const, title: "alpha" };
    const mine = {
      current: true,
      id: "cur",
      status: "waiting" as const,
      title: "this one",
    };

    $stripSessions.set([alpha, mine]);

    const pane = mountPane(90, 30);
    const settle = () => new Promise((r) => setTimeout(r, 80));

    try {
      await waitFor(() => expect(pane.lastFrame()).toContain("› ● this one"));
      pane.press("\x1b[A"); // the list takes the keyboard from the composer
      await waitFor(() => expect(pane.lastFrame()).toContain(AGENT_VIEW_HINT));
      pane.press("\x1b[A"); // onto the Needs input header
      await settle();
      expect(pane.lastFrame()).not.toContain("›");

      // Both sessions leave that group (answered elsewhere): the header's rows are gone and a row is highlighted.
      $stripSessions.set([
        { current: true, id: "cur", status: "working", title: "this one" },
      ]);
      await waitFor(() => {
        expect(pane.lastFrame()).not.toContain("alpha");
        expect(pane.lastFrame()).toContain("› ◐ this one");
      });
      await settle();

      // A session needs input again: the group returns, the highlighted row stays highlighted.
      $stripSessions.set([
        alpha,
        { current: true, id: "cur", status: "working", title: "this one" },
      ]);
      await waitFor(() => expect(pane.lastFrame()).toContain("alpha"));
      await settle();
      expect(pane.lastFrame()).toMatch(/^\s*› /m);
    } finally {
      pane.done();
    }
  });

  it("⏎ on an empty composer opens the selected row but does not fold a header the list last stopped on", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { id: "a", status: "waiting", title: "alpha" },
      { current: true, id: "cur", status: "working", title: "this one" },
    ]);

    const onActivate = vi.fn();
    const onClose = vi.fn();
    const pane = mountPane(90, 30, { onActivate, onClose });
    const settle = () => new Promise((r) => setTimeout(r, 80));

    try {
      await waitFor(() =>
        expect(pane.lastFrame()).toContain("› ◐ this one (this session)"),
      );
      pane.press("\x1b[A"); // composer → list
      await waitFor(() => expect(pane.lastFrame()).toContain(AGENT_VIEW_HINT));
      pane.press("\x1b[A"); // onto the Working header
      await settle();
      pane.press("\t"); // back to the composer, empty
      await waitFor(() =>
        expect(pane.lastFrame()).toContain(AGENT_VIEW_INPUT_HINT),
      );
      await settle();

      pane.press("\r");
      await settle();
      await settle();

      expect(pane.lastFrame()).toContain("▾ Working");
      expect(pane.lastFrame()).toContain("this one (this session)");
      expect(onActivate).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      pane.done();
    }
  });

  it("keeps the composer and the hint when the terminal is shorter than the pane assumed", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set(
      Array.from({ length: 6 }, (_, i) => ({
        id: `s${i}`,
        status: "waiting" as const,
        title: `task ${i}`,
      })),
    );

    // The pane still believes in 24 rows (a resize it has not seen) while the screen has 12.
    const pane = mountPane(90, 12, {}, { altScreen: true, paneRows: 24 });

    try {
      await waitFor(() => expect(pane.lastFrame()).toContain("task 0"));

      const lines = pane.lastFrame().split("\n");

      // The list area gives way, clipped, instead of the composer being squeezed out of the frame.
      expect(lines).toHaveLength(12);
      expect(lines[0]).toContain("Agents");
      expect(lines.at(-2)).toContain("❯");
      expect(lines.at(-1)).toContain("⏎ start");
    } finally {
      pane.done();
    }
  });

  it("moves the composer and the hint to the new last rows when the terminal is resized", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    // Waiting sessions only: nothing ticks, so the resize is the only thing that can re-render the pane.
    $stripSessions.set([
      { id: "a", status: "waiting", title: "alpha" },
      { id: "b", status: "waiting", title: "bravo" },
    ]);

    // No full-screen shell here: a bare mount repaints whole frames, which the test can read.
    const pane = mountPane(90, 12);

    try {
      // Loaded and settled: the session poll's own re-render must not be what picks up the new height.
      await waitFor(() => {
        expect(pane.lastFrame()).toContain("alpha");
        expect(pane.lastFrame()).not.toContain("loading…");
        expect(pane.lastFrame().split("\n")).toHaveLength(12);
      });
      await new Promise((r) => setTimeout(r, 100));

      pane.resize(20);
      await waitFor(() => {
        const lines = pane.lastFrame().split("\n");

        expect(lines).toHaveLength(20);
        expect(lines.at(-2)).toContain("❯");
        expect(lines.at(-1)).toContain("⏎ start");
      });
    } finally {
      pane.done();
    }
  });

  it("hides the parked hardware cursor while the list has the keyboard and shows it again for typed text", async () => {
    const HIDE = "\x1b[?25l";
    const SHOW = "\x1b[?25h";

    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([{ id: "a", status: "waiting", title: "alpha" }]);

    // The cursor escapes go to process.stdout only on a TTY; the pane's frames go to its own stream.
    const isTTY = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
    const writes: string[] = [];
    const spy = vi
      .spyOn(process.stdout, "write")
      .mockImplementation((chunk: unknown) => {
        writes.push(String(chunk));

        return true;
      });

    Object.defineProperty(process.stdout, "isTTY", {
      configurable: true,
      value: true,
    });

    const pane = mountPane(90, 20);
    const lastCursor = () =>
      writes.filter((w) => w === HIDE || w === SHOW).at(-1);

    try {
      await waitFor(() => expect(pane.lastFrame()).toContain("alpha"));
      writes.length = 0;
      pane.press("\x1b[A"); // composer → list
      await waitFor(() => expect(pane.lastFrame()).toContain(AGENT_VIEW_HINT));
      // The renderer parks the cursor on the hint's first cell, so it stays hidden (the composer's own cleanup shows it).
      expect(lastCursor()).toBe(HIDE);

      pane.press("\t"); // list → composer
      await waitFor(() =>
        expect(pane.lastFrame()).toContain(AGENT_VIEW_INPUT_HINT),
      );
      // give React a frame to (re)subscribe the now-focused TextInput before typing
      await new Promise((r) => setTimeout(r, 50));
      writes.length = 0;
      pane.press("h");
      await waitFor(() => expect(lastCursor()).toBe(SHOW));
    } finally {
      pane.done();
      spy.mockRestore();

      if (isTTY) {
        Object.defineProperty(process.stdout, "isTTY", isTTY);
      } else {
        delete (process.stdout as unknown as Record<string, unknown>).isTTY;
      }
    }
  });

  it("space, ⏎ and c on a header collapse and expand its group; ⏎ there attaches nothing", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { id: "a", status: "waiting", title: "alpha" },
      { current: true, id: "cur", status: "working", title: "this one" },
    ]);

    const onActivate = vi.fn();
    const onClose = vi.fn();
    const pane = mountPane(90, 30, { onActivate, onClose });
    const settle = () => new Promise((r) => setTimeout(r, 80));

    try {
      await waitFor(() =>
        expect(pane.lastFrame()).toContain("› ◐ this one (this session)"),
      );
      pane.press("\x1b[A"); // the list takes the keyboard from the composer
      await waitFor(() => expect(pane.lastFrame()).toContain(AGENT_VIEW_HINT));
      pane.press("\x1b[A"); // onto the Working header (open: it is a stop all the same)
      await settle();
      expect(pane.lastFrame()).toContain("▾ Working");

      pane.press(" ");
      await waitFor(() => {
        expect(pane.lastFrame()).toContain("▸ Working");
        expect(pane.lastFrame()).not.toContain("this one");
      });
      // The other group is untouched, and nothing was typed into the composer.
      expect(pane.lastFrame()).toContain("▾ Needs input");
      expect(pane.lastFrame()).toContain("alpha");
      expect(pane.lastFrame()).toContain(AGENT_VIEW_HINT);

      await settle();
      pane.press(" ");
      await waitFor(() => {
        expect(pane.lastFrame()).toContain("▾ Working");
        expect(pane.lastFrame()).toContain("this one (this session)");
      });

      for (const key of ["\r", "c"]) {
        await settle();
        pane.press(key);
        await waitFor(() => expect(pane.lastFrame()).toContain("▸ Working"));
        await settle();
        pane.press(key);
        await waitFor(() => expect(pane.lastFrame()).toContain("▾ Working"));
      }

      expect(onActivate).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    } finally {
      pane.done();
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

    const onNewPrompt = vi.fn();
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
        onNewPrompt={onNewPrompt}
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
      // The chords are list keys: the list takes the keyboard from the composer first.
      stdin.write("\x1b[A");
      await waitFor(() => expect(output).toContain(AGENT_VIEW_HINT));

      stdin.write("\x0e"); // Ctrl+N
      stdin.write("\x1bn"); // Alt+N
      stdin.write("\x18"); // Ctrl+X
      // With no stop armed, y is plain text: it goes to the composer (Esc clears it, ↑ is the list again).
      fresh = "";
      stdin.write("y");
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_INPUT_HINT));
      stdin.write("\x1b");
      await new Promise((r) => setTimeout(r, 100));
      fresh = "";
      stdin.write("\x1b[A");
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_HINT));

      // Sentinel: ↓ then ↑, each waited for on output captured after it was sent.
      fresh = "";
      stdin.write("\x1b[B");
      await waitFor(() => expect(fresh).toContain("› ◐ bravo"));
      fresh = "";
      stdin.write("\x1b[A");
      await waitFor(() => expect(fresh).toContain("› ◐ alpha"));

      expect(onNewPrompt).not.toHaveBeenCalled();
      expect(onStop).not.toHaveBeenCalled();
      expect(output).not.toContain("stop? y/n");

      // The bare letters still are view commands.
      stdin.write("x");
      await waitFor(() => expect(output).toContain("stop? y/n"));
      stdin.write("y");
      await waitFor(() => expect(onStop).toHaveBeenCalled());
      expect(onStop.mock.calls[0]![0]).toMatchObject({ id: "a" });

      // "n" focuses the composer; type text and press Enter to start the session.
      stdin.write("n");
      await waitFor(() => expect(output).toContain("start a new session"));
      // give React a frame to (re)subscribe the now-focused TextInput before typing
      await new Promise((r) => setTimeout(r, 30));
      for (const ch of "test session") {
        stdin.write(ch);
        await new Promise((r) => setTimeout(r, 10));
      }
      stdin.write("\n");
      await new Promise((r) => setTimeout(r, 800));
      await waitFor(
        () => expect(onNewPrompt).toHaveBeenCalledWith("test session"),
        { timeout: 10000 },
      );
    } finally {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  it("composer: ⏎ empties it after starting a session, esc clears a draft without closing the view", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([{ id: "a", status: "working", title: "alpha" }]);

    const onNewPrompt = vi.fn();
    const onClose = vi.fn();
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    let fresh = "";
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });

    stdout.on("data", (c) => {
      fresh += stripAnsi(c.toString());
    });

    const typeSlowly = async (text: string) => {
      for (const ch of text) {
        stdin.write(ch);
        await new Promise((r) => setTimeout(r, 10));
      }
      await new Promise((r) => setTimeout(r, 100));
    };
    const view = renderSync(
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={() => {}}
        onClose={onClose}
        onNewPrompt={onNewPrompt}
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
      await waitFor(() => expect(fresh).toContain("◐ alpha"));
      // The composer has the keyboard as soon as the view opens.
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_INPUT_HINT));
      await new Promise((r) => setTimeout(r, 30));

      await typeSlowly("first task");
      stdin.write("\r");
      await waitFor(() =>
        expect(onNewPrompt).toHaveBeenCalledWith("first task"),
      );
      await new Promise((r) => setTimeout(r, 200));
      expect(view.lastFrame?.() ?? fresh.split("❯").at(-1)).not.toContain(
        "first task",
      );

      await typeSlowly("draft");
      fresh = "";
      stdin.write("\x1b");
      await new Promise((r) => setTimeout(r, 300));
      expect(onClose).not.toHaveBeenCalled();
      expect(fresh.split("❯").at(-1)).toContain("start a new session");

      stdin.write("\x1b");
      await waitFor(() => expect(onClose).toHaveBeenCalled());
    } finally {
      view.unmount();
      view.cleanup();
      $stripSessions.set([]);
      resetUiState();
    }
  });

  it("focus: the composer types on open, ⏎ on it empty opens the row, ↑ reaches the list, typing comes back", async () => {
    resetUiState();
    patchUiState({ sid: "cur" });
    $stripSessions.set([
      { id: "a", status: "working", title: "alpha" },
      { id: "b", status: "working", title: "bravo" },
    ]);

    const onActivate = vi.fn();
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      ref: () => {},
      setRawMode: () => {},
      unref: () => {},
    });
    let fresh = "";
    const stdout = Object.assign(new PassThrough(), {
      columns: 90,
      isTTY: false,
      rows: 30,
    });

    stdout.on("data", (c) => {
      fresh += stripAnsi(c.toString());
    });

    const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));
    const composer = () => fresh.split("❯").at(-1) ?? "";
    const view = renderSync(
      <AgentViewPane
        gw={
          {
            request: () => Promise.resolve({ sessions: [] }),
          } as unknown as GatewayClient
        }
        onActivate={onActivate}
        onClose={() => {}}
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
      await waitFor(() => expect(fresh).toContain("› ◐ alpha"));
      expect(fresh).toContain(AGENT_VIEW_INPUT_HINT);
      await pause(30);

      // ⏎ on the empty composer opens the selected row, as ⏎ on the list does.
      stdin.write("\r");
      await waitFor(() => expect(onActivate).toHaveBeenCalled());
      expect(onActivate.mock.calls[0]![0]).toMatchObject({ id: "a" });

      // ↑ hands the keyboard to the list; there c collapses the group onto its header.
      fresh = "";
      stdin.write("\x1b[A");
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_HINT));
      stdin.write("c");
      await waitFor(() => expect(fresh).toContain("▸ Working"));

      // A typed letter goes back to the composer, and there c is just a letter: the group stays collapsed.
      fresh = "";
      stdin.write("h");
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_INPUT_HINT));
      await pause(30);
      stdin.write("c");
      await waitFor(() => expect(composer()).toContain("hc"));
      expect(fresh).not.toContain("▾ Working");

      // Tab is the way back to the list, where c expands the group again.
      fresh = "";
      stdin.write("\t");
      await waitFor(() => expect(fresh).toContain(AGENT_VIEW_HINT));
      stdin.write("c");
      await waitFor(() => expect(fresh).toContain("▾ Working"));
      expect(onActivate).toHaveBeenCalledTimes(1);
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

  describe("archiving", () => {
    const CWD = "/work/proj";
    const nowS = Date.now() / 1000;
    const STORED = [
      { id: "p1", started_at: nowS - DAY, title: "first work" },
      { id: "p2", started_at: nowS - 2 * DAY, title: "second work" },
      { id: "x1", started_at: nowS - 3 * DAY, title: "put away" },
    ].map((s) => ({ ...s, cwd: CWD, message_count: 3 }));

    /** A gateway that keeps the archived set, answering `session.list` and `session.archive` by the contract. */
    const archiveGateway = ({ failArchived = false } = {}) => {
      const archivedIds = new Set(["x1"]);

      return vi.fn((method: string, params: Record<string, unknown> = {}) => {
        if (method === "session.list") {
          if (params.archived && failArchived) {
            return Promise.reject(new Error("unknown param: archived"));
          }

          return Promise.resolve({
            sessions: STORED.filter(
              (s) => archivedIds.has(s.id) === !!params.archived,
            ).map((s) => ({
              ...s,
              archived_at: archivedIds.has(s.id) ? nowS : null,
            })),
          });
        }

        if (method === "session.archive") {
          for (const id of params.session_ids as string[]) {
            if (params.archived === false) {
              archivedIds.delete(id);
            } else {
              archivedIds.add(id);
            }
          }

          return Promise.resolve({
            archived: (params.session_ids as string[]).length,
            skipped: [],
          });
        }

        return Promise.resolve(null);
      });
    };

    const mountPane = (request: ReturnType<typeof archiveGateway>) => {
      resetUiState();
      patchUiState({
        info: { cwd: CWD, model: "test", skills: {}, tools: {} },
        sid: "cur",
      });

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
          gw={{ request } as unknown as GatewayClient}
          onActivate={() => {}}
          onClose={() => {}}
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

      return {
        close: () => {
          view.unmount();
          view.cleanup();
          resetUiState();
        },
        // A whole frame starts with the view's header; other writes are terminal control (cursor shape and the like).
        lastFrame: () =>
          frames.filter((f) => f.includes("Agents")).at(-1) ?? "",
        stdin,
      };
    };

    const listCalls = (request: ReturnType<typeof archiveGateway>) =>
      request.mock.calls.filter(([m]) => m === "session.list");

    it("a archives the selected earlier session, refetches both lists and the row moves under Archived; u restores it", async () => {
      const request = archiveGateway();
      const pane = mountPane(request);

      try {
        await waitFor(() => {
          expect(pane.lastFrame()).toContain("› · first work");
          expect(pane.lastFrame()).toMatch(/▸ Archived {2}1/);
        });
        expect(pane.lastFrame()).not.toContain("put away");
        expect(listCalls(request).map(([, p]) => p)).toEqual([
          { cwd: CWD, limit: 50 },
          { archived: true, cwd: CWD, limit: 50 },
        ]);

        pane.stdin.write("\x1b[A"); // the list takes the keyboard
        await waitFor(() =>
          expect(pane.lastFrame()).toContain(AGENT_VIEW_ARCHIVE_HINT),
        );
        pane.stdin.write("a");
        await waitFor(() =>
          expect(pane.lastFrame()).toMatch(/▸ Archived {2}2/),
        );
        expect(request).toHaveBeenCalledWith("session.archive", {
          archived: true,
          session_ids: ["p1"],
        });
        expect(listCalls(request)).toHaveLength(4);
        expect(
          listCalls(request)
            .slice(2)
            .map(([, p]) => p),
        ).toEqual([
          { cwd: CWD, limit: 50 },
          { archived: true, cwd: CWD, limit: 50 },
        ]);
        // The cursor stayed in view, on the row that was next to the archived one.
        expect(pane.lastFrame()).not.toContain("first work");
        expect(pane.lastFrame()).toContain("› · second work");

        // ↓ onto the Archived header, c opens it (the header keeps the selection), ↓ enters its first (newest) row.
        pane.stdin.write("\x1b[B");
        await waitFor(() =>
          expect(pane.lastFrame()).not.toContain("› · second work"),
        );
        pane.stdin.write("c");
        await waitFor(() =>
          expect(pane.lastFrame()).toMatch(/▾ Archived {2}2/),
        );
        pane.stdin.write("\x1b[B");
        await waitFor(() =>
          expect(pane.lastFrame()).toContain("› · first work"),
        );

        const opened = pane.lastFrame();

        expect(opened).toContain("› · first work");
        expect(opened.indexOf("Archived")).toBeLessThan(
          opened.indexOf("first work"),
        );
        expect(opened.indexOf("first work")).toBeLessThan(
          opened.indexOf("put away"),
        );
        expect(opened).toContain(AGENT_VIEW_RESTORE_HINT);

        pane.stdin.write("u");
        await waitFor(() =>
          expect(pane.lastFrame()).toMatch(/▾ Archived {2}1/),
        );
        expect(request).toHaveBeenCalledWith("session.archive", {
          archived: false,
          session_ids: ["p1"],
        });
        expect(listCalls(request)).toHaveLength(6);

        const restored = pane.lastFrame();

        expect(restored.indexOf("first work")).toBeLessThan(
          restored.indexOf("Archived"),
        );
        expect(restored).toContain("› · put away");
      } finally {
        pane.close();
      }
    });

    it("still lists the earlier sessions when the archived request fails (an older core)", async () => {
      const request = archiveGateway({ failArchived: true });
      const pane = mountPane(request);

      try {
        await waitFor(() => {
          expect(pane.lastFrame()).toContain("first work");
          expect(pane.lastFrame()).toContain("second work");
        });
        expect(listCalls(request)).toHaveLength(2);

        const out = pane.lastFrame();

        expect(out).not.toContain("Archived");
        expect(out).not.toContain("unknown param");
        expect(out).not.toContain("loading");
      } finally {
        pane.close();
      }
    });

    describe("the archive proposal banner", () => {
      const ITEMS = [
        { id: "o1", idle_days: 21, message_count: 4, title: "foo" },
        { id: "o2", idle_days: 18, message_count: 4, title: "bar" },
        { id: "o3", idle_days: 15, message_count: 4, title: "baz" },
        { id: "o4", idle_days: 15, message_count: 4, title: "qux" },
      ];
      const proposal = (items = ITEMS, days = 14) => ({
        days,
        items,
        total: items.length,
      });

      it("asks with the count, the first titles and their ages; singular for one; none without items", () => {
        const out = dump(mixed(), { suggestions: proposal() });

        expect(out).toContain(
          "Archive 4 sessions unused for 14+ days?  alt+y archive · alt+n keep",
        );
        expect(out).toContain("foo (21d) · bar (18d) · baz (15d) +1 more");
        // Between the counts line and the list.
        expect(out.indexOf("Archive 4")).toBeLessThan(
          out.indexOf("Needs input"),
        );

        expect(
          dump(mixed(), { suggestions: proposal(ITEMS.slice(0, 1)) }),
        ).toContain("Archive 1 session unused for 14+ days?");
        expect(dump(mixed(), { suggestions: proposal([]) })).not.toContain(
          "Archive",
        );
        expect(dump(mixed(), { suggestions: proposal([], 0) })).not.toContain(
          "Archive",
        );
      });

      it("sanitises titles and stays inside a narrow pane", () => {
        const items = [
          { ...ITEMS[0]!, title: "evil\x1b[2Jtitle\x07 that goes on and on" },
          ...ITEMS.slice(1),
        ];
        const out = dump([], { cols: 30, suggestions: proposal(items) });

        expect(out).not.toContain("\x1b");
        expect(out).not.toContain("\x07");
        expect(out).toContain("Archive 4 idle sessions?");
        for (const line of out.split("\n")) {
          expect(line.length).toBeLessThanOrEqual(30);
        }
      });

      /** The archive gateway, plus `session.archive_suggestions` (proposing ITEMS once) and `session.archive_decline`. */
      const proposingGateway = ({ fail = false } = {}) => {
        const base = archiveGateway();
        let proposed = false;

        return vi.fn((method: string, params: Record<string, unknown> = {}) => {
          if (method === "session.archive_suggestions") {
            if (fail) {
              return Promise.reject(new Error("unknown method"));
            }

            const first = !proposed;

            proposed = true;

            return Promise.resolve(proposal(first ? ITEMS : []));
          }

          if (method === "session.archive_decline") {
            return Promise.resolve({
              declined: (params.session_ids as string[]).length,
            });
          }

          return base(method, params);
        });
      };

      it("alt+y in the composer archives exactly the listed sessions, refetches and the banner goes", async () => {
        const request = proposingGateway();
        const pane = mountPane(request as ReturnType<typeof archiveGateway>);

        try {
          await waitFor(() =>
            expect(pane.lastFrame()).toContain("Archive 4 sessions"),
          );
          expect(request).toHaveBeenCalledWith("session.archive_suggestions", {
            cwd: CWD,
          });
          expect(pane.lastFrame()).toContain(AGENT_VIEW_INPUT_HINT);

          pane.stdin.write("\x1by");
          await waitFor(() =>
            expect(pane.lastFrame()).not.toContain("Archive 4"),
          );
          expect(request).toHaveBeenCalledWith("session.archive", {
            archived: true,
            session_ids: ["o1", "o2", "o3", "o4"],
          });
          expect(
            request.mock.calls.filter(
              ([m]) => m === "session.archive_suggestions",
            ),
          ).toHaveLength(2);
          // The chord was the view's: nothing was typed into the composer.
          expect(pane.lastFrame()).not.toMatch(/❯ y/);
          expect(pane.lastFrame()).toContain(AGENT_VIEW_INPUT_HINT);
        } finally {
          pane.close();
        }
      });

      it("alt+n on the list keeps them: session.archive_decline with those ids, and the banner goes", async () => {
        const request = proposingGateway();
        const pane = mountPane(request as ReturnType<typeof archiveGateway>);

        try {
          await waitFor(() =>
            expect(pane.lastFrame()).toContain("Archive 4 sessions"),
          );
          pane.stdin.write("\x1b[A");
          await waitFor(() =>
            expect(pane.lastFrame()).toContain(AGENT_VIEW_ARCHIVE_HINT),
          );
          pane.stdin.write("\x1bn");
          await waitFor(() =>
            expect(pane.lastFrame()).not.toContain("Archive 4"),
          );
          expect(request).toHaveBeenCalledWith("session.archive_decline", {
            session_ids: ["o1", "o2", "o3", "o4"],
          });
          expect(request).not.toHaveBeenCalledWith(
            "session.archive",
            expect.anything(),
          );
        } finally {
          pane.close();
        }
      });

      it("shows no banner when the request fails (an older core); the lists still load", async () => {
        const request = proposingGateway({ fail: true });
        const pane = mountPane(request as ReturnType<typeof archiveGateway>);

        try {
          await waitFor(() => expect(pane.lastFrame()).toContain("first work"));
          expect(pane.lastFrame()).not.toContain("alt+y");
          expect(pane.lastFrame()).not.toContain("unknown method");
        } finally {
          pane.close();
        }
      });
    });

    it("counts no row as archived without an archived_at (an older core that ignores the param)", async () => {
      const request = vi.fn(() =>
        Promise.resolve({ sessions: STORED.slice(0, 1) }),
      );
      const pane = mountPane(request as ReturnType<typeof archiveGateway>);

      try {
        await waitFor(() => expect(pane.lastFrame()).toContain("first work"));
        expect(pane.lastFrame()).not.toContain("Archived");
      } finally {
        pane.close();
      }
    });
  });
});
