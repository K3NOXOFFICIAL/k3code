import { beforeEach, describe, expect, it, vi } from "vitest";

import * as ClipboardModule from "../lib/clipboard.js";
import { copyText } from "../lib/copyText.js";
import * as Osc52Module from "../lib/osc52.js";
import * as TerminalSetupModule from "../lib/terminalSetup.js";

describe("copyText", () => {
  beforeEach(() => vi.restoreAllMocks());

  const spies = (remote: boolean, native: boolean) => {
    vi.spyOn(TerminalSetupModule, "isRemoteShellSession").mockReturnValue(
      remote,
    );

    return {
      native: vi
        .spyOn(ClipboardModule, "writeClipboardText")
        .mockResolvedValue(native),
      osc: vi.spyOn(Osc52Module, "writeOsc52Clipboard").mockReturnValue(true),
    };
  };

  it("uses the native clipboard locally", async () => {
    const { native, osc } = spies(false, true);

    await expect(copyText("hi")).resolves.toBe("native");
    expect(native).toHaveBeenCalledWith("hi");
    expect(osc).not.toHaveBeenCalled();
  });

  it("falls back to OSC52 when native fails", async () => {
    const { osc } = spies(false, false);

    await expect(copyText("hi")).resolves.toBe("osc52");
    expect(osc).toHaveBeenCalledWith("hi");
  });

  it("uses OSC52 only in a remote shell", async () => {
    const { native, osc } = spies(true, true);

    await expect(copyText("hi")).resolves.toBe("osc52");
    expect(osc).toHaveBeenCalledWith("hi");
    expect(native).not.toHaveBeenCalled();
  });

  it("rejects when the native write throws", async () => {
    const { native } = spies(false, true);

    native.mockRejectedValue(new Error("nope"));
    await expect(copyText("hi")).rejects.toThrow("nope");
  });
});
