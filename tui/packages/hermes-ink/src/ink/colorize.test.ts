import { afterEach, describe, expect, it, vi } from "vitest";

import {
  richEightBitColorNumber,
  shouldBoostChalkLevelForTruecolorTerminal,
  shouldUseRichEightBitDowngradeForLegacyAppleTerminal,
} from "./colorize.js";

const env = (vars: Record<string, string>) => vars as NodeJS.ProcessEnv;

describe("shouldBoostChalkLevelForTruecolorTerminal", () => {
  it("boosts xterm.js terminals that chalk reads as 256-color", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ TERM_PROGRAM: "vscode" }),
        2,
      ),
    ).toBe(true);
  });

  it("boosts Windows Terminal, which chalk reads as 256-color under WSL", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ TERM: "xterm-256color", WT_SESSION: "abc-123" }),
        2,
      ),
    ).toBe(true);
  });

  it("boosts any terminal hosting a WSL distro", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ TERM: "xterm-256color", WSL_DISTRO_NAME: "Ubuntu" }),
        2,
      ),
    ).toBe(true);
  });

  it("leaves a plain 256-color terminal alone", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ TERM: "xterm-256color" }),
        2,
      ),
    ).toBe(false);
  });

  it("only raises level 2: no color, 16-color and truecolor stay as chalk found them", () => {
    const wsl = env({ WT_SESSION: "abc-123" });

    expect(shouldBoostChalkLevelForTruecolorTerminal(wsl, 0)).toBe(false);
    expect(shouldBoostChalkLevelForTruecolorTerminal(wsl, 1)).toBe(false);
    expect(shouldBoostChalkLevelForTruecolorTerminal(wsl, 3)).toBe(false);
  });

  it("does not guess when COLORTERM already says something", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ COLORTERM: "gnome-terminal", WT_SESSION: "abc-123" }),
        2,
      ),
    ).toBe(false);
  });

  it("does not override an explicit FORCE_COLOR", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ FORCE_COLOR: "2", WT_SESSION: "abc-123" }),
        2,
      ),
    ).toBe(false);
  });

  it("does not boost inside tmux, which re-emits only 256 colors", () => {
    expect(
      shouldBoostChalkLevelForTruecolorTerminal(
        env({ TMUX: "/tmp/tmux-1000/default,1,0", WT_SESSION: "abc-123" }),
        2,
      ),
    ).toBe(false);
  });
});

describe("chalk level at load", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  async function loadColorize(vars: Record<string, string>) {
    vi.resetModules();

    for (const key of [
      "COLORTERM",
      "FORCE_COLOR",
      "NO_COLOR",
      "TERM_PROGRAM",
      "TMUX",
      "WSL_DISTRO_NAME",
      "WT_SESSION",
    ]) {
      vi.stubEnv(key, undefined);
    }

    for (const [key, value] of Object.entries(vars)) {
      vi.stubEnv(key, value);
    }

    const { default: chalk } = await import("chalk");

    chalk.level = 2;

    const mod = await import("./colorize.js");

    return { chalk, ...mod };
  }

  it("renders a theme color as 24-bit RGB under Windows Terminal + WSL", async () => {
    const { chalk, colorize } = await loadColorize({
      TERM: "xterm-256color",
      WSL_DISTRO_NAME: "Ubuntu",
      WT_SESSION: "abc-123",
    });

    expect(chalk.level).toBe(3);
    expect(colorize("x", "rgb(255,105,180)", "foreground")).toBe(
      "\u001b[38;2;255;105;180mx\u001b[39m",
    );
  });

  it("keeps the 256-color cube inside tmux", async () => {
    const { chalk } = await loadColorize({
      TMUX: "/tmp/tmux-1000/default,1,0",
      WT_SESSION: "abc-123",
    });

    expect(chalk.level).toBe(2);
  });
});

describe("shouldUseRichEightBitDowngradeForLegacyAppleTerminal", () => {
  it("uses Rich-compatible 256-color downgrade on legacy Apple Terminal", () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        { TERM_PROGRAM: "Apple_Terminal" } as NodeJS.ProcessEnv,
        2,
      ),
    ).toBe(true);
  });

  it("normalizes Apple Terminal names before matching", () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        { TERM_PROGRAM: " Apple_Terminal " } as NodeJS.ProcessEnv,
        2,
      ),
    ).toBe(true);
  });

  it("does not rewrite when Apple Terminal advertises truecolor", () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        {
          COLORTERM: "truecolor",
          TERM_PROGRAM: "Apple_Terminal",
        } as NodeJS.ProcessEnv,
        3,
      ),
    ).toBe(false);
  });

  it("does not override explicit color environment choices", () => {
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        {
          FORCE_COLOR: "2",
          TERM_PROGRAM: "Apple_Terminal",
        } as NodeJS.ProcessEnv,
        2,
      ),
    ).toBe(false);
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        {
          HERMES_TUI_TRUECOLOR: "1",
          TERM_PROGRAM: "Apple_Terminal",
        } as NodeJS.ProcessEnv,
        3,
      ),
    ).toBe(false);
    expect(
      shouldUseRichEightBitDowngradeForLegacyAppleTerminal(
        {
          K3CODE_TUI_TRUECOLOR: "1",
          TERM_PROGRAM: "Apple_Terminal",
        } as NodeJS.ProcessEnv,
        2,
      ),
    ).toBe(false);
  });
});

describe("richEightBitColorNumber", () => {
  it("matches Rich downgrade output for default Hermes skin colors", () => {
    expect(richEightBitColorNumber(0xff, 0xd7, 0x00)).toBe(220);
    expect(richEightBitColorNumber(0xff, 0xbf, 0x00)).toBe(214);
    expect(richEightBitColorNumber(0xcd, 0x7f, 0x32)).toBe(173);
    expect(richEightBitColorNumber(0xb8, 0x86, 0x0b)).toBe(136);
    expect(richEightBitColorNumber(0xff, 0xf8, 0xdc)).toBe(230);
  });
});
