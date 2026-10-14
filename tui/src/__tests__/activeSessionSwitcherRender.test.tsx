import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it } from "vitest";

import { ActiveSessionSwitcher } from "../components/activeSessionSwitcher.js";
import { FloatBox } from "../components/appChrome.js";
import type { GatewayClient } from "../gatewayClient.js";
import { DEFAULT_THEME } from "../theme.js";

const rowsOf = (n: number, prefix: string) =>
  Array.from({ length: n }, (_, i) => ({
    id: `${prefix}${i}`,
    message_count: 3,
    preview: `preview ${i}`,
    started_at: 1,
    title: `${prefix} title ${i}`,
  }));

const fakeGw = (history: unknown[]) =>
  ({
    request: async (method: string) =>
      method === "session.active_list"
        ? {
            sessions: [
              {
                current: true,
                id: "live-1",
                model: "default",
                status: "completed",
                title: "Session",
              },
            ],
          }
        : { sessions: history },
  }) as unknown as GatewayClient;

// `useStdout` hands components `process.stdout`, so the terminal size under
// test has to be set there as well as on the stream Ink paints to.
const withTerminalSize = async <T,>(
  columns: number,
  rows: number,
  run: () => Promise<T>,
) => {
  const saved = { columns: process.stdout.columns, rows: process.stdout.rows };
  Object.assign(process.stdout, { columns, rows });

  try {
    return await run();
  } finally {
    Object.assign(process.stdout, saved);
  }
};

const render = async (columns: number, rows: number, history: unknown[]) => {
  const stdout = Object.assign(new PassThrough(), {
    columns,
    isTTY: false,
    rows,
  });
  const frames: string[] = [];
  stdout.on("data", (chunk) => frames.push(chunk.toString()));

  const view = renderSync(
    <FloatBox color={DEFAULT_THEME.color.border}>
      <ActiveSessionSwitcher
        currentSessionId="live-1"
        gw={fakeGw(history)}
        onCancel={() => {}}
        onClose={async () => null}
        onNew={() => {}}
        onNewPrompt={() => {}}
        onResume={() => {}}
        onSelect={() => {}}
        t={DEFAULT_THEME}
      />
    </FloatBox>,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: Object.assign(new EventEmitter(), {
        isTTY: true,
        read: () => null,
        readableLength: 0,
        ref: () => {},
        setEncoding: () => {},
        setRawMode: () => {},
        unref: () => {},
      }) as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  await new Promise((resolve) => setTimeout(resolve, 500));
  view.unmount();
  view.cleanup();

  return stripAnsi(
    frames.filter((frame) => stripAnsi(frame).includes("Sessions")).at(-1) ??
      "",
  );
};

describe("session switcher render", () => {
  it.each([
    [120, 40],
    [60, 15],
  ])(
    "keeps status and model apart and draws the footer at %ix%i",
    async (columns, rows) => {
      const out = await withTerminalSize(columns, rows, () =>
        render(columns, rows, rowsOf(30, "old")),
      );
      const lines = out.split("\n");

      expect(out).not.toContain("completeddefault");
      expect(out).toMatch(/completed\s+default/);
      expect(out).toContain("Esc close");
      expect(out).toContain("Select +new");
      expect(lines.length).toBeLessThanOrEqual(rows);
      // bottom border of the double-line box (its right corner sits past column 60)
      expect(out).toMatch(/╚═{10,}/);
    },
  );
});
