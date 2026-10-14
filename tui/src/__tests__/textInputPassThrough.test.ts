import { describe, expect, it } from "vitest";

import {
  shouldPassThroughToGlobalHandler,
  shouldPreserveCtrlJNewline,
} from "../components/textInput.js";

const key = (overrides: Record<string, unknown> = {}) =>
  ({ ctrl: false, meta: false, ...overrides }) as any;

describe("shouldPreserveCtrlJNewline", () => {
  it("preserves Ctrl+J as newline in Ghostty even when tmux masks TERM/TERM_PROGRAM", () => {
    expect(
      shouldPreserveCtrlJNewline({
        GHOSTTY_RESOURCES_DIR: "/usr/share/ghostty",
        TERM: "tmux-256color",
        TERM_PROGRAM: "tmux",
      }),
    ).toBe(true);
  });

  it("keeps bare local POSIX LF-compatible prompts submitting on Ctrl+J", () => {
    expect(
      shouldPreserveCtrlJNewline({ TERM: "xterm-256color" }, () => ""),
    ).toBe(false);
  });

  it("detects WSL through WSL_INTEROP, whatever WSL_DISTRO_NAME holds", () => {
    expect(
      shouldPreserveCtrlJNewline(
        { WSL_DISTRO_NAME: "Ubuntu", WSL_INTEROP: "/run/WSL/8_interop" },
        () => "",
      ),
    ).toBe(true);
  });

  it("detects WSL through /proc/version containing 'microsoft' in any case", () => {
    expect(
      shouldPreserveCtrlJNewline(
        { WSL_DISTRO_NAME: "Ubuntu" },
        () => "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@x)",
      ),
    ).toBe(true);
    expect(
      shouldPreserveCtrlJNewline({}, () => "Linux version 4.4.0-Microsoft"),
    ).toBe(true);
  });

  it("does not treat a plain Linux kernel or a distro name as WSL", () => {
    expect(
      shouldPreserveCtrlJNewline(
        { WSL_DISTRO_NAME: "Ubuntu" },
        () => "Linux version 6.8.0-generic (buildd@lcy02)",
      ),
    ).toBe(false);
    expect(shouldPreserveCtrlJNewline({}, () => "")).toBe(false);
  });
});

describe("shouldPassThroughToGlobalHandler", () => {
  it("does not swallow ordinary typing keys", () => {
    expect(shouldPassThroughToGlobalHandler("h", key())).toBe(false);
    expect(shouldPassThroughToGlobalHandler("o", key())).toBe(false);
  });

  it("always passes through global control keys", () => {
    expect(shouldPassThroughToGlobalHandler("c", key({ ctrl: true }))).toBe(
      true,
    );
    expect(shouldPassThroughToGlobalHandler("x", key({ ctrl: true }))).toBe(
      true,
    );
    expect(shouldPassThroughToGlobalHandler("o", key({ ctrl: true }))).toBe(
      true,
    );
    expect(shouldPassThroughToGlobalHandler("", key({ escape: true }))).toBe(
      true,
    );
    expect(shouldPassThroughToGlobalHandler("", key({ tab: true }))).toBe(true);
    expect(shouldPassThroughToGlobalHandler("", key({ pageUp: true }))).toBe(
      true,
    );
    expect(shouldPassThroughToGlobalHandler("", key({ pageDown: true }))).toBe(
      true,
    );
  });
});
