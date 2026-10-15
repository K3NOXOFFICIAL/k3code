import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import { describe, expect, it } from "vitest";

import { SessionPanel } from "../components/branding.js";
import { DEFAULT_THEME } from "../theme.js";
import type { SessionInfo } from "../types.js";

// Regression: the welcome box cwd row was cut from the right (`truncate-end`),
// so a long path kept its head ("/mnt/c/Users/me/.claude…") and lost the
// project directory. It must be cut from the left so the tail stays visible.

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const CWD = "/mnt/c/Users/someone/scratch-dir/k3audit/win-e2e/s3/work";

async function render(columns: number): Promise<string> {
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

  const info: SessionInfo = {
    cwd: CWD,
    model: "test-model",
    skills: {},
    tools: {},
  };

  const instance = renderSync(
    React.createElement(SessionPanel, { info, sid: "test", t: DEFAULT_THEME }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    },
  );

  try {
    await delay(20);

    // eslint-disable-next-line no-control-regex
    return captured.replace(/\u001b\[[0-9;]*m/g, "");
  } finally {
    instance.unmount();
    instance.cleanup();
  }
}

describe("welcome box cwd", () => {
  for (const columns of [80, 100, 120]) {
    it(`keeps the last path segments when cut at ${columns} columns`, async () => {
      const frame = await render(columns);

      expect(frame).toContain("win-e2e/s3/work");
    });
  }

  it("cuts from the left with an ellipsis at 120 columns", async () => {
    const frame = await render(120);

    expect(frame).toMatch(/…\S*\/work/);
    expect(frame).not.toContain(CWD);
  });
});
