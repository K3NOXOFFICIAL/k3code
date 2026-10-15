import React from "react";
import { describe, expect, it } from "vitest";

import { renderToScreen } from "../../packages/hermes-ink/src/ink/render-to-screen.js";
import { cellAtIndex } from "../../packages/hermes-ink/src/ink/screen.js";
import { SessionPanel } from "../components/branding.js";
import { DEFAULT_THEME } from "../theme.js";

// Regression (#170): in the wide welcome box (90+ columns) the model row had no
// wrap mode, so a long model name was broken mid-word onto a second line
// ("claude-sonnet-4-5-20250929-thinki" / "ng-extended-context · k3code"). The
// narrow layout already cut it at the end with an ellipsis; the wide one must
// too, keeping the row on one line.

const MODEL =
  "openrouter/anthropic/claude-sonnet-4-5-20250929-thinking-extended-context";

const screenLines = (width: number): string[] => {
  const { height, screen } = renderToScreen(
    <SessionPanel
      info={{
        cwd: "/home/someone/projects/app",
        model: MODEL,
        skills: {},
        tools: { files: ["read", "write"], shell: ["bash"] },
      }}
      maxWidth={width}
      sid="0123456789abcdef"
      t={DEFAULT_THEME}
    />,
    width,
  );
  const lines: string[] = [];

  for (let row = 0; row < height; row++) {
    let line = "";

    for (let col = 0; col < screen.width; col++) {
      line += cellAtIndex(screen, row * screen.width + col).char || "";
    }

    lines.push(line.trimEnd());
  }

  return lines;
};

describe("welcome box model row", () => {
  for (const width of [60, 100, 120, 160]) {
    it(`keeps a long model name on one line, cut with an ellipsis, at ${width} columns`, () => {
      const lines = screenLines(width);
      const rows = lines.filter((l) => l.includes("claude-sonnet-4-5"));

      expect(rows).toHaveLength(1);
      expect(rows[0]).toMatch(/claude-sonnet-4-5-\S*…/);
      // nothing of the cut-off tail spills onto the next line
      expect(lines.some((l) => l.includes("extended-context"))).toBe(false);
    });
  }
});
