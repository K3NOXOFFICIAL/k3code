import { describe, expect, it } from "vitest";

import {
  boundedLiveRenderText,
  buildToolTrailLine,
  buildVerboseToolTrailLine,
  edgePreview,
  estimateRows,
  formatToolCall,
  isToolTrailResultLine,
  lastCotTrailIndex,
  parseToolTrailResultLine,
  pasteTokenLabel,
  sameToolTrailGroup,
  splitToolDuration,
  thinkingPreview,
  toolCallSummary,
} from "../lib/text.js";

describe("toolCallSummary", () => {
  it("puts the key argument first, one line per tool, never a dict repr", () => {
    expect(
      toolCallSummary("bash", {
        command: "pwd; ls -la; git rev-parse --show-toplevel\necho done",
      }),
    ).toBe("Bash  pwd; ls -la; git rev-parse --show-toplevel");
    expect(toolCallSummary("read", { path: "src/k3code/README.md" })).toBe(
      "Read  src/k3code/README.md",
    );
    expect(
      toolCallSummary("read", { path: "a.py", offset: 40, limit: 20 }),
    ).toBe("Read  a.py  from 40 (20 lines)");
    expect(
      toolCallSummary("glob", { pattern: "**/k3code*", path: "src/k3code" }),
    ).toBe("Glob  **/k3code*  in src/k3code");
    expect(toolCallSummary("glob", { pattern: "*", path: "." })).toBe(
      "Glob  *",
    );
    expect(
      toolCallSummary("grep", { pattern: "foo", path: "src", include: "*.ts" }),
    ).toBe("Grep  foo  in src");
    expect(
      toolCallSummary("edit", {
        path: "x.ts",
        old_string: "a\nb",
        new_string: "c",
      }),
    ).toBe("Edit  x.ts");
    expect(toolCallSummary("write", { path: "y.md", content: "# hi\n" })).toBe(
      "Write  y.md",
    );
  });

  it("phrases an unknown tool as key=value pairs, compactly", () => {
    expect(
      toolCallSummary("web_fetch", { url: "https://x.example", prompt: "p" }),
    ).toBe("Web Fetch  https://x.example  prompt=p");
    expect(toolCallSummary("todo", { items: [{ id: 1 }], merge: true })).toBe(
      'Todo  items=[{"id":1}], merge=true',
    );
    expect(toolCallSummary("ping", {})).toBe("Ping");
  });

  it("truncates to the width and never keeps a line break", () => {
    const summary = toolCallSummary(
      "bash",
      { command: `${"x".repeat(200)}\nsecond line` },
      40,
    );

    expect(summary).toHaveLength(40);
    expect(summary.endsWith("…")).toBe(true);
    expect(summary).not.toContain("\n");
    expect(
      toolCallSummary("note", { text: "line one\nline two" }),
    ).not.toContain("\n");
  });
});

describe("formatToolCall", () => {
  it("prefers structured args over the free-text context", () => {
    expect(
      formatToolCall("glob", "{'pattern': '*', 'path': '.'}", {
        pattern: "*",
        path: "src",
      }),
    ).toBe("Glob  *  in src");
  });

  it("falls back to the context preview without quotes", () => {
    expect(formatToolCall("glob", "*.ts")).toBe("Glob  *.ts");
    expect(formatToolCall("glob", "")).toBe("Glob");
  });

  it("phrases a context that is itself a dict repr or JSON (older gateways)", () => {
    expect(
      formatToolCall("glob", "{'pattern': '[!.]*/k3code*', 'path': '.'}"),
    ).toBe("Glob  [!.]*/k3code*");
    expect(formatToolCall("read", '{"path": "src/k3code/README.md"}')).toBe(
      "Read  src/k3code/README.md",
    );
    expect(
      formatToolCall(
        "bash",
        "{'command': 'pwd; ls -la; git rev-parse --show-toplevel 2>/dev/…",
      ),
    ).toBe("Bash  pwd; ls -la; git rev-parse --show-toplevel 2>/dev/…");
  });
});

describe("isToolTrailResultLine", () => {
  it("detects completion markers", () => {
    expect(isToolTrailResultLine("foo ✓")).toBe(true);
    expect(isToolTrailResultLine("foo ✗")).toBe(true);
    expect(isToolTrailResultLine("drafting x…")).toBe(false);
  });
});

describe("buildToolTrailLine", () => {
  it("puts completion duration inline before the result marker", () => {
    const line = buildToolTrailLine("read_file", "x", false, "", 0.94);

    expect(line).toBe("Read File  x (0.9s) ✓");
    expect(parseToolTrailResultLine(line)).toEqual({
      call: "Read File  x (0.9s)",
      detail: "",
      mark: "✓",
    });
    expect(splitToolDuration("Read File  x (0.9s)")).toEqual({
      label: "Read File  x",
      duration: " (0.9s)",
    });
  });
});

describe("buildVerboseToolTrailLine", () => {
  it("preserves multiline args and result details", () => {
    const line = buildVerboseToolTrailLine(
      "terminal",
      "npm test",
      false,
      1.25,
      '{\n  "cmd": "npm test"\n}',
      "first line\nsecond :: line",
    );

    expect(line).toContain("Args:\n{");
    expect(line).toContain("Result:\nfirst line\nsecond :: line");
    expect(parseToolTrailResultLine(line)).toEqual({
      call: "Terminal  npm test (1.3s)",
      detail:
        'Args:\n{\n  "cmd": "npm test"\n}\nResult:\nfirst line\nsecond :: line',
      mark: "✓",
    });
  });

  it("labels verbose failures as errors", () => {
    const line = buildVerboseToolTrailLine(
      "terminal",
      "npm test",
      true,
      0.5,
      undefined,
      "command failed",
    );

    expect(line).toContain("Error:\ncommand failed");
    expect(line).not.toContain("Result:\ncommand failed");
    expect(parseToolTrailResultLine(line)).toEqual({
      call: "Terminal  npm test (0.5s)",
      detail: "Error:\ncommand failed",
      mark: "✗",
    });
  });

  it("caps a large result to a small persisted preview (#34095)", () => {
    // A 40KB browser-snapshot-sized result must NOT be embedded whole — the
    // persisted, expanded-by-default trail block is what blew up the Ink
    // render tree and silently OOM-killed the TUI. The block stays small.
    const huge = "A".repeat(40_000);
    const line = buildVerboseToolTrailLine(
      "browser_snapshot",
      "https://x.example",
      false,
      2,
      undefined,
      huge,
    );

    expect(line).toContain("Result:\n");
    // Far below the old 16KB live-render budget; the whole line (call + label +
    // omitted marker + preview) must stay on the order of ~1KB, not ~40KB.
    expect(line.length).toBeLessThan(2_000);
    expect(line).toContain("omitted");
    expect(line.endsWith(" ✓")).toBe(true);
  });

  it("does not truncate a result that already fits the preview budget", () => {
    const small = "ok: 3 files changed";
    const line = buildVerboseToolTrailLine(
      "patch",
      "index.html",
      false,
      0.1,
      undefined,
      small,
    );

    expect(line).toContain(`Result:\n${small}`);
    expect(line).not.toContain("omitted");
  });
});

describe("lastCotTrailIndex", () => {
  it("finds last non-result line", () => {
    expect(lastCotTrailIndex(["a ✓", "thinking…"])).toBe(1);
    expect(lastCotTrailIndex(["only result ✓"])).toBe(-1);
  });
});

describe("sameToolTrailGroup", () => {
  it("matches bare check lines", () => {
    expect(sameToolTrailGroup("searching", "searching ✓")).toBe(true);
    expect(sameToolTrailGroup("searching", "searching ✗")).toBe(true);
  });

  it("matches contextual lines", () => {
    expect(sameToolTrailGroup("searching", "searching: * ✓")).toBe(true);
    expect(sameToolTrailGroup("searching", "searching: foo ✓")).toBe(true);
  });

  it("rejects other tools", () => {
    expect(sameToolTrailGroup("searching", "reading ✓")).toBe(false);
    expect(sameToolTrailGroup("searching", "searching extra ✓")).toBe(false);
  });
});

describe("thinkingPreview", () => {
  it("adds paragraph breaks before markdown thinking headings", () => {
    const raw =
      "**Considering user instructions**\nI need to answer.**Planning tool execution**\nI can run tools.**Determining weather search parameters**\nUse SF.";

    expect(thinkingPreview(raw, "full")).toBe(
      "**Considering user instructions**\nI need to answer.\n\n**Planning tool execution**\nI can run tools.\n\n**Determining weather search parameters**\nUse SF.",
    );
  });
});

describe("boundedLiveRenderText", () => {
  it("preserves short live text verbatim", () => {
    expect(
      boundedLiveRenderText("one\ntwo", { maxChars: 100, maxLines: 10 }),
    ).toBe("one\ntwo");
  });

  it("keeps the live tail by character budget", () => {
    const out = boundedLiveRenderText("abcdefghij", {
      maxChars: 4,
      maxLines: 10,
    });

    expect(out).toContain("ghij");
    expect(out).toContain("omitted");
    expect(out).not.toContain("abcdef");
  });

  it("keeps the live tail by line budget", () => {
    const out = boundedLiveRenderText(["a", "b", "c", "d"].join("\n"), {
      maxChars: 100,
      maxLines: 2,
    });

    expect(out).toContain("c\nd");
    expect(out).toContain("omitted 2 lines");
    expect(out).not.toContain("a\nb");
  });
});

describe("edgePreview", () => {
  it("keeps both ends for long text", () => {
    expect(
      edgePreview(
        "Vampire Bondage ropes slipped from her neck, still stained with blood",
        8,
        18,
      ),
    ).toBe("Vampire.. stained with blood");
  });
});

describe("thinkingPreview over-bound tail", () => {
  it("retains the live tail when reasoning exceeds the clean bound", () => {
    const TAIL = "<<<LIVE_TAIL_MARKER>>>";
    // Slightly above the 24k clean-tail bound, so the implementation must trim.
    const reasoning = "A".repeat(25_000) + "\n" + TAIL;
    const result = thinkingPreview(reasoning, "full");
    expect(result).toContain(TAIL);
    // The bounded window is shorter than the 25k prefix, but the tail remains.
    expect(result.length).toBeLessThanOrEqual(25_000);
  });
});
describe("pasteTokenLabel", () => {
  it("builds readable long-paste labels with counts", () => {
    const label = pasteTokenLabel(
      "Vampire Bondage ropes slipped from her neck, still stained with blood",
      250,
    );
    expect(label.startsWith("[[ ")).toBe(true);
    expect(label).toContain("[250 lines]");
    expect(label.endsWith(" ]]")).toBe(true);
  });
});

describe("estimateRows", () => {
  it("handles tilde code fences", () => {
    const md = ["~~~markdown", "# heading", "~~~"].join("\n");

    expect(estimateRows(md, 40)).toBeGreaterThanOrEqual(2);
  });

  it("handles checklist bullets as list rows", () => {
    const md = ["- [x] done", "- [ ] todo"].join("\n");

    expect(estimateRows(md, 40)).toBe(2);
  });

  it("keeps intraword underscores when sizing snake_case identifiers", () => {
    const w = 80;
    const snake = "look at test_case_with_underscores now";
    const plain = "look at test case with underscores now";

    expect(estimateRows(snake, w)).toBe(estimateRows(plain, w));
  });
});
