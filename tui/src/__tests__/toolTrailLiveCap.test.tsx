import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import { stripAnsi } from "@k3code/shared/ansi";
import React from "react";
import { describe, expect, it } from "vitest";

import { MessageLine } from "../components/messageLine.js";
import { LIVE_TOOL_ROWS_MAX } from "../config/limits.js";
import { buildToolTrailLine } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";

const COUNT = LIVE_TOOL_ROWS_MAX * 4;
const trail = Array.from({ length: COUNT }, (_, i) =>
  buildToolTrailLine("terminal", `echo marker${i}x`, false, "", 0.1),
);

const paint = (liveDetails: boolean) => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 1000 });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });
  stdout.on("data", (chunk) => {
    output += chunk.toString();
  });

  const instance = renderSync(
    React.createElement(MessageLine, {
      cols: 100,
      liveDetails,
      msg: { kind: "trail", role: "system", text: "", tools: trail },
      t: DEFAULT_THEME,
    }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    },
  );

  instance.unmount();
  instance.cleanup();

  return stripAnsi(output);
};

describe("tool trail row cap in the live area", () => {
  it("paints only the newest rows while the turn is live, and says how many it folded", () => {
    const rendered = paint(true);
    const hidden = COUNT - LIVE_TOOL_ROWS_MAX;

    expect(rendered).toContain(`marker${COUNT - 1}x`);
    expect(rendered).toContain(`marker${hidden}x`);
    expect(rendered).not.toContain(`marker${hidden - 1}x`);
    expect(rendered).not.toContain("marker0x");
    expect(rendered).toContain(`${hidden} earlier tool calls`);
    expect(rendered).toContain(`Tool calls (${COUNT})`);
  });

  it("keeps every row once the message is settled in the transcript", () => {
    const rendered = paint(false);

    expect(rendered).toContain("marker0x");
    expect(rendered).toContain(`marker${COUNT - 1}x`);
    expect(rendered).not.toContain("earlier tool calls");
  });
});
