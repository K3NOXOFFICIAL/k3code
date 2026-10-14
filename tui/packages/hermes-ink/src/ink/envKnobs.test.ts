import { afterEach, describe, expect, it } from "vitest";

import { isMouseClicksDisabled } from "../utils/fullscreen.js";
import { logError } from "../utils/log.js";

import { isDashboardHosted } from "./termio/host.js";

const KEYS = [
  "HERMES_INK_DEBUG_ERRORS",
  "HERMES_TUI_DISABLE_MOUSE_CLICKS",
  "K3CODE_INK_DEBUG_ERRORS",
  "K3CODE_TUI_DISABLE_MOUSE_CLICKS",
] as const;

afterEach(() => {
  for (const k of KEYS) {
    delete process.env[k];
  }
});

describe("isDashboardHosted", () => {
  it("reads K3CODE_PTY_HOST and keeps HERMES_PTY_HOST as a fallback", () => {
    expect(isDashboardHosted({} as NodeJS.ProcessEnv)).toBe(false);
    expect(
      isDashboardHosted({ K3CODE_PTY_HOST: "dashboard" } as NodeJS.ProcessEnv),
    ).toBe(true);
    expect(
      isDashboardHosted({ HERMES_PTY_HOST: "dashboard" } as NodeJS.ProcessEnv),
    ).toBe(true);
    expect(
      isDashboardHosted({
        HERMES_PTY_HOST: "dashboard",
        K3CODE_PTY_HOST: "terminal",
      } as NodeJS.ProcessEnv),
    ).toBe(false);
  });
});

describe("isMouseClicksDisabled", () => {
  it("reads K3CODE_TUI_DISABLE_MOUSE_CLICKS and keeps HERMES_ as a fallback", () => {
    expect(isMouseClicksDisabled()).toBe(false);

    process.env.K3CODE_TUI_DISABLE_MOUSE_CLICKS = "1";
    expect(isMouseClicksDisabled()).toBe(true);

    delete process.env.K3CODE_TUI_DISABLE_MOUSE_CLICKS;
    process.env.HERMES_TUI_DISABLE_MOUSE_CLICKS = "1";
    expect(isMouseClicksDisabled()).toBe(true);

    process.env.K3CODE_TUI_DISABLE_MOUSE_CLICKS = "0";
    expect(isMouseClicksDisabled()).toBe(false);
  });
});

describe("logError", () => {
  it("prints only when K3CODE_INK_DEBUG_ERRORS (or the HERMES_ name) is set", () => {
    const seen: unknown[] = [];
    const original = console.error;

    console.error = (e: unknown) => {
      seen.push(e);
    };

    try {
      logError("off");
      process.env.K3CODE_INK_DEBUG_ERRORS = "1";
      logError("k3");
      delete process.env.K3CODE_INK_DEBUG_ERRORS;
      process.env.HERMES_INK_DEBUG_ERRORS = "1";
      logError("hermes");
    } finally {
      console.error = original;
    }

    expect(seen).toEqual(["k3", "hermes"]);
  });
});
