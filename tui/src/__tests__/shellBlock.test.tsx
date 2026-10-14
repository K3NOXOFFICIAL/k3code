import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, describe, expect, it } from "vitest";

import {
  $shellRuns,
  appendShellOutput,
  resetShellRunsForTests,
  startShellRun,
  toggleShellExpanded,
} from "../app/shellRunStore.js";
import { MessageLine } from "../components/messageLine.js";
import { ShellBlock } from "../components/shellBlock.js";
import { shellBlockModel, shellOutputLines } from "../lib/shellBlock.js";
import { estimatedMsgHeight } from "../lib/virtualHeights.js";
import { DEFAULT_THEME } from "../theme.js";
import type { Msg, ShellRun } from "../types.js";

const COLS = 60;

/** The last painted frame, escapes removed, as rows. `rows` is far above the tallest block so ink paints it whole. */
const paint = (element: React.ReactElement): string[] => {
  const stdout = Object.assign(new PassThrough(), { columns: COLS, rows: 300 });
  const frames: string[] = [];
  stdout.on("data", (chunk) => frames.push(stripAnsi(chunk.toString())));

  const view = renderSync(element, {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: new PassThrough() as unknown as NodeJS.ReadStream,
  });

  view.unmount();
  view.cleanup();

  return (frames.filter((f) => f.trim()).at(-1) ?? "").trimEnd().split("\n");
};

const lines = (n: number, tag = "line") =>
  Array.from({ length: n }, (_, i) => `${tag} ${i + 1}`).join("\n") + "\n";

const run = (patch: Partial<ShellRun> = {}): ShellRun => ({
  command: "ls -la",
  execId: "x1",
  running: false,
  ...patch,
});

const block = (shell: ShellRun) =>
  paint(<ShellBlock shell={shell} t={DEFAULT_THEME} />);

afterEach(resetShellRunsForTests);

describe("shell block", () => {
  it("shows the command, its output and a dim exit code when it finished short", () => {
    const out = block(run({ code: 0, stdout: "a.txt\nb.txt\n" }));

    expect(out.map((l) => l.trimEnd())).toEqual([
      "$ ls -la",
      "  a.txt",
      "  b.txt",
      "  exit 0",
    ]);
  });

  it("collapses output over 40 lines to the first 5 and last 25, and the toggle shows it all", () => {
    expect(block(run({ code: 0, stdout: lines(40) }))).toHaveLength(1 + 40 + 1);
    expect(block(run({ code: 0, stdout: lines(40) }))[0]).toBe("$ ls -la");

    const collapsed = block(run({ code: 0, stdout: lines(41) }));

    expect(collapsed).toHaveLength(1 + 5 + 1 + 25 + 1);
    expect(collapsed[0]).toBe("▸ $ ls -la");
    expect(collapsed[5]?.trim()).toBe("line 5");
    expect(collapsed[6]?.trim()).toBe("… 11 lines hidden");
    expect(collapsed[7]?.trim()).toBe("line 17");
    expect(collapsed.at(-2)?.trim()).toBe("line 41");

    toggleShellExpanded("x1");
    const expanded = block(run({ code: 0, stdout: lines(41) }));

    expect(expanded).toHaveLength(1 + 41 + 1);
    expect(expanded[0]).toBe("▾ $ ls -la");
    expect(expanded.join("\n")).not.toContain("hidden");

    toggleShellExpanded("x1");
    expect(block(run({ code: 0, stdout: lines(41) }))).toHaveLength(33);
  });

  it("puts stderr after stdout and shows a denied run's reason with its exit code", () => {
    expect(
      block(run({ code: 1, stderr: "boom\n", stdout: "partial\n" })).map((l) =>
        l.trim(),
      ),
    ).toEqual(["$ ls -la", "partial", "boom", "exit 1"]);
    expect(
      block(run({ code: 126, stderr: "denied: rm -rf / is blocked\n" })).map(
        (l) => l.trim(),
      ),
    ).toEqual(["$ ls -la", "denied: rm -rf / is blocked", "exit 126"]);
  });

  it("tags a cancelled and a timed-out run", () => {
    expect(
      block(run({ cancelled: true, code: 130 }))
        .at(-1)
        ?.trim(),
    ).toBe("exit 130 · cancelled");
    expect(
      block(run({ code: 124, timedOut: true }))
        .at(-1)
        ?.trim(),
    ).toBe("exit 124 · timed out");
    expect(
      block(run({ cancelled: true }))
        .at(-1)
        ?.trim(),
    ).toBe("cancelled");
  });

  it("shows running… and the live tail (last 25 lines) from the run store while it runs", () => {
    startShellRun("x1");
    appendShellOutput({ exec_id: "x1", text: lines(30, "live") });

    const out = block(run({ running: true }));

    expect(out[0]).toBe("$ ls -la");
    expect(out[1]?.trim()).toBe("live 6");
    expect(out.at(-2)?.trim()).toBe("live 30");
    expect(out.at(-1)?.trim()).toBe("running…");
    expect(out).toHaveLength(1 + 25 + 1);
    expect(block(run({ execId: "other", running: true }))).toHaveLength(2);
  });

  it("strips ANSI colour, an OSC title, an OSC 8 link and cursor moves from output", () => {
    const raw =
      "\x1b]0;my title\x07\x1b[31mred\x1b[0m \x1b[2Aup\x1b[1Gcol \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\\n";

    expect(shellOutputLines(raw)).toEqual(["red upcol link"]);
    expect(
      shellBlockModel(run({ code: 0, stderr: raw }), undefined, false).rows,
    ).toEqual([{ err: true, text: "red upcol link" }]);
    expect(
      shellBlockModel(run({ running: true }), [raw.trimEnd()], false).rows,
    ).toEqual([{ err: false, text: "red upcol link" }]);
  });

  it("keeps what a terminal shows after a carriage return", () => {
    expect(shellOutputLines("10%\r50%\r100%\r\nok\r\n")).toEqual([
      "100%",
      "ok",
    ]);
    expect(shellOutputLines("")).toEqual([]);
    expect(shellOutputLines("\n")).toEqual([""]);
  });
});

describe("shell block height estimate", () => {
  const shellMsg = (shell: ShellRun): Msg => ({
    kind: "shell",
    role: "system",
    shell,
    text: "",
  });

  const pair = (shell: ShellRun, expanded = false) => {
    if (expanded) {
      toggleShellExpanded(shell.execId);
    }

    const msg = shellMsg(shell);
    const rendered = paint(
      <MessageLine cols={COLS} msg={msg} t={DEFAULT_THEME} />,
    ).length;
    const estimated = estimatedMsgHeight(msg, COLS, {
      compact: false,
      details: false,
      shellExpanded: expanded,
      shellLiveLines: $shellRuns.get()[shell.execId]?.lines,
    });

    return { estimated, rendered };
  };

  it("equals the rendered height while running", () => {
    startShellRun("x1");
    appendShellOutput({ exec_id: "x1", text: lines(30, "live") });

    expect(pair(run({ running: true }))).toEqual({
      estimated: 27,
      rendered: 27,
    });
  });

  it("equals the rendered height for a short finished run", () => {
    expect(pair(run({ code: 0, stdout: "a\nb\n" }))).toEqual({
      estimated: 4,
      rendered: 4,
    });
  });

  it("equals the rendered height for 100 lines collapsed and expanded", () => {
    expect(pair(run({ code: 0, stdout: lines(100) }))).toEqual({
      estimated: 33,
      rendered: 33,
    });
    expect(pair(run({ code: 0, stdout: lines(100) }), true)).toEqual({
      estimated: 102,
      rendered: 102,
    });
  });

  it("equals the rendered height with stderr and a line wider than the screen", () => {
    expect(
      pair(
        run({
          code: 2,
          stderr: `${"x".repeat(200)}\nerr 2\n`,
          stdout: "out\n",
          timedOut: true,
        }),
      ),
    ).toEqual({ estimated: 5, rendered: 5 });
  });

  it("re-keys the row when the reply patches the run", async () => {
    const { messageHeightKey } = await import("../lib/virtualHeights.js");
    const running = shellMsg(run({ running: true }));

    expect(messageHeightKey(running)).not.toBe(
      messageHeightKey(shellMsg(run({ code: 0, stdout: "a\n" }))),
    );
    expect(
      messageHeightKey(shellMsg(run({ code: 0, stdout: "a\n" }))),
    ).not.toBe(messageHeightKey(shellMsg(run({ code: 0, stdout: "a\nb\n" }))));
  });
});
