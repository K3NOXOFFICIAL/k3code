import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { describe, expect, it, vi } from "vitest";

import { createServerRequestHandler } from "../app/createServerRequestHandler.js";
import { getOverlayState, resetOverlayState } from "../app/overlayStore.js";
import { ApprovalPrompt } from "../components/prompts.js";
import { DEFAULT_THEME } from "../theme.js";
import type { ApprovalReq } from "../types.js";

import { waitFor } from "./waitFor.js";

function mount(req: ApprovalReq) {
  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 40 });
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true,
    ref: () => {},
    setRawMode: () => {},
    unref: () => {},
  });
  const onChoice = vi.fn();
  let output = "";

  stdout.on("data", (chunk) => (output += String(chunk)));
  const view = renderSync(
    <ApprovalPrompt onChoice={onChoice} req={req} t={DEFAULT_THEME} />,
    {
      patchConsole: false,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );
  // a non-TTY stdout gets every frame whole, each starting at the prompt's top border
  const frame = () => stripAnsi(output).split("╔").at(-1) ?? "";

  return {
    /** The rows inside the diff's single-line border; null when no such box is drawn. */
    diffBox: () => {
      const rows = frame().split("\n");
      const open = rows.findIndex((row) => row.startsWith("║ ┌"));
      const close = rows.findIndex((row) => row.startsWith("║ └"));

      return open < 0 || close < open
        ? null
        : rows
            .slice(open + 1, close)
            .map((row) => row.replace(/^║ │ /, "").replace(/ *│ ║$/, ""));
    },
    frame,
    onChoice,
    press: (key: string) => stdin.write(key),
    stop: () => {
      view.unmount();
      view.cleanup();
    },
  };
}

const lines = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, i) => `+line ${from + i}`);

describe("edit/write approval preview (issue #131)", () => {
  it("carries the gateway's preview onto the approval prompt", () => {
    const preview = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2";

    resetOverlayState();
    createServerRequestHandler({ ringPromptBell: vi.fn(), setStatus: vi.fn() })(
      {
        fail: vi.fn(),
        id: "approval-1",
        method: "approval",
        params: { command: "edit app.py", description: "app.py", preview },
        respond: vi.fn(),
      },
    );

    expect(getOverlayState().approval?.preview).toBe(preview);
    resetOverlayState();
  });

  it("draws a long preview in a bordered box, 16 rows at a time, and pages it with [ and ]", async () => {
    const view = mount({
      command: "write big.txt",
      description: "big.txt",
      preview: lines(0, 49).join("\n"),
      requestId: "approval-1",
    });
    const page = async (key: string, from: number, hint: string) => {
      view.press(key);
      await waitFor(() =>
        expect(view.diffBox()).toEqual([...lines(from, from + 15), hint]),
      );
    };

    try {
      await waitFor(() =>
        expect(view.diffBox()).toEqual([
          ...lines(0, 15),
          "lines 1–16 of 50 · [ / ] scroll",
        ]),
      );
      // useInput subscribes in a passive effect, after the first frame: let React run it before a key
      await new Promise((resolve) => setTimeout(resolve, 20));
      await page("]", 15, "lines 16–31 of 50 · [ / ] scroll");
      await page("]", 30, "lines 31–46 of 50 · [ / ] scroll");
      await page("]", 34, "lines 35–50 of 50 · [ / ] scroll"); // clamped at the end
      await page("[", 19, "lines 20–35 of 50 · [ / ] scroll");
      expect(view.onChoice).not.toHaveBeenCalled();
    } finally {
      view.stop();
    }
  });

  it("shows a short preview whole without a scroll hint, and no box without one", async () => {
    const preview = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2";
    const short = mount({
      command: "edit app.py",
      description: "app.py",
      preview,
      requestId: "approval-1",
    });

    try {
      await waitFor(() => expect(short.diffBox()).toEqual(preview.split("\n")));
    } finally {
      short.stop();
    }

    const plain = mount({
      command: "bash ls",
      description: "ls",
      requestId: "approval-2",
    });

    try {
      await waitFor(() => expect(plain.frame()).toContain("1. Allow once"));
      expect(plain.diffBox()).toBeNull();
    } finally {
      plain.stop();
    }
  });
});
