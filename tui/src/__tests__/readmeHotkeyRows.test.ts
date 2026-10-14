import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const README = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../../README.md"),
  "utf8",
);

const row = (key: string) =>
  README.split("\n").find((line) => line.startsWith(`| \`${key}\``)) ?? "";

describe("tui/README.md hotkeys", () => {
  it("describes Ctrl+L as a repaint, not a new session", () => {
    const ctrlL = row("Ctrl+L");

    expect(ctrlL).toMatch(/repaint/i);
    expect(ctrlL).toContain("/redraw");
    expect(ctrlL).not.toMatch(/new session|\/clear/i);
  });

  it("documents the transcript scroll keys and says Home/End do not scroll", () => {
    expect(README).not.toMatch(/PgUp[^\n]*left to the terminal emulator/);
    expect(README).toMatch(/`PgUp` \/ `PgDn`[^\n]*scroll the transcript/);
    expect(README).toMatch(/`Shift\+Up` \/ `Shift\+Down`[^\n]*scroll/);
    expect(README).toMatch(/`Home` \/ `End`[^\n]*do not scroll the transcript/);
  });
});
