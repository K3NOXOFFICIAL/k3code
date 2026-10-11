import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import { stripAnsi } from "@k3code/shared/ansi";
import React from "react";
import { describe, expect, it } from "vitest";

import { ToolTrail, toolNamesSummary } from "../components/thinking.js";
import { ROLE } from "../domain/roles.js";
import { verboseToolTrailLine } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";

const settle = async () => {
  for (let i = 0; i < 10; i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};

const renderTrail = async (
  trail: string[],
  sections?: Record<string, string>,
) => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });
  stdout.on("data", (chunk) => {
    output += chunk.toString();
  });

  const instance = renderSync(
    <ToolTrail sections={sections} t={DEFAULT_THEME} trail={trail} />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    },
  );

  await settle();
  instance.unmount();
  instance.cleanup();

  return stripAnsi(output);
};

const glob = verboseToolTrailLine(
  'Glob("**/*.ts")',
  false,
  276.9,
  "pattern: **/*.ts",
  Array.from({ length: 40 }, (_, i) => `file-${i}.ts`).join("\n"),
);
const read = verboseToolTrailLine('Read("a.ts")', false, 0.1, "", "body");

describe("ToolTrail — tool calls collapse by default", () => {
  it("shows only the header with a name summary, no Args/Result bodies", async () => {
    const out = await renderTrail([glob, read]);

    expect(out).toContain("Tool calls");
    expect(out).toContain("Glob, Read");
    expect(out).not.toContain("Result:");
    expect(out).not.toContain("file-39.ts");
  });

  it("renders bodies when tools are expanded explicitly", async () => {
    const out = await renderTrail([glob], { tools: "expanded" });

    expect(out).toContain("Result:");
  });

  it("dedupes an identical finished call reaching the trail twice", async () => {
    const out = await renderTrail([glob, glob], { tools: "expanded" });
    const lastFrame = out.slice(out.lastIndexOf("Tool calls"));

    expect(lastFrame.split("Result:").length - 1).toBe(1);
  });
});

describe("toolNamesSummary", () => {
  it("counts repeats and trims long lists", () => {
    expect(
      toolNamesSummary([
        { label: "Glob(x)" },
        { label: "Glob(y)" },
        { label: "Read(z)" },
      ]),
    ).toBe("Glob ×2, Read");
    expect(
      toolNamesSummary(
        ["A", "B", "C", "D", "E", "F"].map((label) => ({ label })),
      ),
    ).toBe("A, B, C, D, +2");
  });

  it("strips a leading emoji from gateway-phrased labels", () => {
    expect(toolNamesSummary([{ label: "🔎 Search  foo" }])).toBe("Search");
  });
});

describe("ROLE styling", () => {
  it("fills user prompts and marks assistant replies with the accent", () => {
    const user = ROLE.user(DEFAULT_THEME);
    const assistant = ROLE.assistant(DEFAULT_THEME);

    expect(user.fill).toBe(DEFAULT_THEME.color.userBg);
    expect(assistant.fill).toBeUndefined();
    expect(assistant.prefix).toBe(DEFAULT_THEME.color.accent);
    expect(ROLE.tool(DEFAULT_THEME).body).toBe(DEFAULT_THEME.color.toolDim);
  });
});
