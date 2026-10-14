import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import { describe, expect, it } from "vitest";

import { SessionPanel } from "../components/branding.js";
import { DEFAULT_THEME } from "../theme.js";
import type { SessionInfo } from "../types.js";

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const TOOLS = [
  "bash",
  "bash_kill",
  "bash_output",
  "skill",
  "task",
  "task_result",
  "web_browse",
  "web_fetch",
  "web_search",
  "read_file",
  "write_file",
  "edit_file",
  "glob",
  "grep",
  "todo_write",
  "notebook_edit",
];

const info: SessionInfo = {
  model: "test-model",
  skills: { core: ["a"] },
  tools: { agent: TOOLS, file: ["read_file", "write_file"] },
};

async function renderPanel(
  columns: number,
  tools: string[] = TOOLS,
): Promise<string[]> {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();

  Object.assign(stdout, { columns, isTTY: false, rows: 40 });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });

  let captured = "";
  stdout.on("data", (chunk) => {
    captured += chunk.toString();
  });

  const instance = renderSync(
    React.createElement(SessionPanel, {
      info: { ...info, tools: { agent: tools, file: ["read_file"] } },
      maxWidth: columns - 2,
      sid: "test",
      t: DEFAULT_THEME,
    }),
    {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  try {
    await delay(20);

    // eslint-disable-next-line no-control-regex
    const plain = captured.replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "");
    const frames = plain.split("\r").join("").split("\n");

    return frames;
  } finally {
    instance.unmount();
    instance.cleanup();
  }
}

// Regression: truncLine checked the fit before appending ", …+N", so a row
// that landed near the cell edge was clipped by the Text's own truncation into
// "…+…" or "……" (a second ellipsis, and the "+N" count lost).
describe("session panel Available Tools row", () => {
  for (const columns of [60, 80, 120]) {
    it(`ends in one ellipsis with the +N suffix intact at ${columns} columns`, async () => {
      // Shift the name lengths so the row lands on every column near the edge.
      for (let pad = 0; pad < 12; pad++) {
        const tools = TOOLS.map((n, i) =>
          i % 3 === 0 ? n + "x".repeat(pad) : n,
        );
        const lines = await renderPanel(columns, tools);
        const row = lines.filter((l) => l.includes("agent:")).pop() ?? "";

        expect(row, `${columns} cols, pad ${pad}`).toMatch(
          /^│.*agent: .*, …\+\d+\s*│$/,
        );
        expect(row.match(/…/g)).toHaveLength(1);
      }
    });
  }

  it("shows every tool, no ellipsis, when the row fits", async () => {
    const lines = await renderPanel(120, ["bash", "glob"]);
    const row = lines.filter((l) => l.includes("agent:")).pop() ?? "";

    expect(row).toContain("agent: bash, glob");
    expect(row).not.toContain("…");
  });
});
