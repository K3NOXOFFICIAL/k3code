import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import { stripAnsi } from "@k3code/shared/ansi";
import React from "react";
import { describe, expect, it } from "vitest";

import { MessageLine } from "../components/messageLine.js";
import { toolNamesSummary } from "../components/thinking.js";
import { toTranscriptMessages } from "../domain/messages.js";
import { toolCallSummary, toolTrailLine } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";
import type { Msg, SectionVisibility } from "../types.js";

// Snapshot-style renders of the transcript's role blocks: the reader must
// tell at a glance what the user typed (`❯ you` + a `┃` bar), what the model
// did (dim tool rows under a collapsed `▸ N tool calls` header) and what it
// answered (`● k3code` header). Rendered at 72 columns.
const COLS = 72;

const paint = (
  msgs: Msg[],
  opts: { liveDetails?: boolean; sections?: SectionVisibility } = {},
) => {
  const stdout = new PassThrough();
  const stdin = new PassThrough();
  const stderr = new PassThrough();
  // Each write is one frame; keep the last one that painted text.
  const frames: string[] = [];

  Object.assign(stdout, { columns: COLS, isTTY: false, rows: 200 });
  Object.assign(stdin, { isTTY: false });
  Object.assign(stderr, { isTTY: false });
  stdout.on("data", (chunk) => {
    frames.push(chunk.toString());
  });

  const instance = renderSync(
    React.createElement(
      React.Fragment,
      null,
      ...msgs.map((msg, i) =>
        React.createElement(MessageLine, {
          cols: COLS - 4,
          key: i,
          liveDetails: opts.liveDetails,
          msg,
          prev: msgs[i - 1],
          sections: opts.sections,
          t: DEFAULT_THEME,
        }),
      ),
    ),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream,
    },
  );

  instance.unmount();
  instance.cleanup();

  const last =
    frames
      .map(stripAnsi)
      .filter((f) => f.trim())
      .at(-1) ?? "";
  const lines = last
    .split("\n")
    .map((line) => line.trimEnd())
    .filter(Boolean);

  return { lines, text: lines.join("\n") };
};

const glob = (pattern: string, path = ".") =>
  toolTrailLine(toolCallSummary("glob", { path, pattern }), false, "", 0.1);

describe("transcript role blocks", () => {
  it("renders a typed prompt as a `❯ you` block with a bar on every line", () => {
    const { lines } = paint([
      {
        role: "user",
        text: "please look at the harness\nand fix the transcript",
      },
    ]);

    expect(lines[0]).toBe("❯ you");
    expect(lines[1]).toBe("┃ please look at the harness");
    expect(lines[2]).toBe("┃ and fix the transcript");
  });

  it("labels a goal kick `goal` and a continuation `system`, not as the user's words", () => {
    const { lines } = paint([
      {
        role: "user",
        text: "[Standing goal]\nGoal: help me improve the harness.\n\nWork toward this goal.",
      },
    ]);

    expect(lines[0]).toBe("goal");
    expect(lines.join("\n")).not.toContain("❯ you");
    expect(lines[1]).toBe("┃ [Standing goal]");

    const cut = paint([
      {
        role: "user",
        text: "Your previous answer was cut off at the output token limit. Continue.",
      },
    ]);

    expect(cut.lines[0]).toBe("system");
  });

  it("heads an answer with `● k3code` and no bare `Response` label", () => {
    const { lines, text } = paint([
      {
        kind: "trail",
        role: "system",
        text: "",
        tools: [glob("*.md", "src/k3code")],
      },
      { role: "assistant", text: "The project is at **~/src/k3code**." },
    ]);

    expect(text).not.toContain("Response");
    expect(lines.some((line) => line === "● k3code")).toBe(true);

    const header = lines.indexOf("● k3code");

    expect(lines[header + 1]).toMatch(
      /^\s+The project is at ~\/src\/k3code\.$/,
    );
  });

  it("collapses the tool trail to a one-line header with the names, running and failed calls still listed", () => {
    const trail = [
      toolTrailLine(
        toolCallSummary("bash", {
          command: "pwd; ls -la; git rev-parse --show-toplevel 2>/dev/null",
        }),
        false,
        "",
        0.3,
      ),
      glob("[!.]*/k3code*"),
      glob("[!.]*/[!.]*/k3code*"),
      toolTrailLine(
        toolCallSummary("read", { path: "src/k3code/README.md" }),
        false,
        "",
        0.1,
      ),
      toolTrailLine(
        toolCallSummary("grep", { path: "src", pattern: "foo" }),
        true,
        "no matches",
        0.2,
      ),
      "drafting edit…",
    ];
    const { lines, text } = paint([
      { kind: "trail", role: "system", text: "", tools: trail },
    ]);

    expect(text).toContain("▸ 6 tool calls");
    expect(text).toContain("(Bash, Glob ×2, Read, Grep, +1)");
    // Finished calls fold away; the failed and the running one stay.
    expect(text).not.toContain("Bash  pwd");
    expect(text).not.toContain("README.md");
    expect(lines.some((line) => /✗ Grep {2}foo {2}in src/.test(line))).toBe(
      true,
    );
    expect(lines.some((line) => /● Edit/.test(line))).toBe(true);
  });

  it("expanded, lists each call as a one-line summary with its status glyph and a result preview", () => {
    const trail = [
      toolTrailLine(
        toolCallSummary("bash", {
          command: "pwd; ls -la; git rev-parse --show-toplevel 2>/dev/null",
        }),
        false,
        "",
        0.3,
      ),
      toolTrailLine(
        toolCallSummary("glob", { path: "src/k3code", pattern: "**/k3code*" }),
        false,
        "3 files",
        0.1,
      ),
      toolTrailLine(
        toolCallSummary("grep", { path: "src", pattern: "foo" }),
        true,
        "no matches",
        0.2,
      ),
    ];
    const { lines, text } = paint(
      [{ kind: "trail", role: "system", text: "", tools: trail }],
      { sections: { tools: "expanded" } },
    );

    expect(text).toContain("▾ 3 tool calls");
    // The long command stays on one row, cut at the width with an ellipsis.
    const bash = lines.find((line) => line.includes("✓ Bash  pwd; ls -la;"));

    expect(bash).toBeDefined();
    expect(bash!.length).toBeLessThanOrEqual(COLS);
    expect(bash).toMatch(/…$/);
    expect(lines.filter((line) => line.includes("rev-parse"))).toHaveLength(1);
    expect(
      lines.some((line) =>
        /✓ Glob {2}\*\*\/k3code\* {2}in src\/k3code/.test(line),
      ),
    ).toBe(true);
    expect(lines.some((line) => /└─ 3 files$/.test(line))).toBe(true);
    expect(lines.some((line) => /✗ Grep {2}foo {2}in src/.test(line))).toBe(
      true,
    );
    expect(lines.some((line) => /└─ no matches$/.test(line))).toBe(true);
    // Never a dict repr, never a summary broken over two rows.
    expect(text).not.toContain("{'");
    expect(text).not.toMatch(/\("\{/);
  });

  it("renders a repeated identical finished call once", () => {
    const line = glob("[!.]*/k3code*");
    const { text } = paint(
      [
        {
          kind: "trail",
          role: "system",
          text: "",
          tools: Array.from({ length: 30 }, () => line),
        },
      ],
      { sections: { tools: "expanded" } },
    );

    expect(text.match(/Glob {2}\[!\.\]\*\/k3code\*/g)).toHaveLength(1);
    expect(text).toContain("▾ 1 tool call");
  });

  it("phrases a resumed session's tool rows from their stored arguments", () => {
    const msgs = toTranscriptMessages([
      { role: "user", text: "find it" },
      {
        args: { path: ".", pattern: "[!.]*/k3code*" },
        context: "{'pattern': '[!.]*/k3code*', 'path': '.'}",
        name: "glob",
        role: "tool",
      },
      {
        context: "{'path': 'src/k3code/README.md'}",
        name: "read",
        role: "tool",
      },
      { role: "assistant", text: "Found it." },
    ]);
    const { text } = paint(msgs, { sections: { tools: "expanded" } });

    expect(text).toContain("Glob  [!.]*/k3code*");
    expect(text).toContain("Read  src/k3code/README.md");
    expect(text).not.toContain("{'");
  });

  it("keeps the thinking panel collapsed once reasoning is over", () => {
    const { text } = paint([
      {
        kind: "trail",
        role: "system",
        text: "",
        thinking: "I should look at the source first.",
      },
    ]);

    expect(text).toContain("▸ thinking");
    expect(text).not.toContain("look at the source");
  });
});

describe("toolNamesSummary", () => {
  it("counts repeated names and caps the list", () => {
    const groups = ["Glob  a", "Glob  b", "Read  x", "Bash  pwd"].map(
      (label) => ({ label }),
    );

    expect(toolNamesSummary(groups)).toBe("Glob ×2, Read, Bash");
    expect(
      toolNamesSummary(
        ["A", "B", "C", "D", "E", "F"].map((label) => ({ label })),
      ),
    ).toBe("A, B, C, D, +2");
  });
});
