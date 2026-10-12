import { EventEmitter } from "events";

import React from "react";
import { describe, expect, it } from "vitest";

import { AlternateScreen } from "./components/AlternateScreen.js";
import Text from "./components/Text.js";
import Ink from "./ink.js";
import { ERASE_SCROLLBACK } from "./termio/csi.js";
import { ENTER_ALT_SCREEN } from "./termio/dec.js";

class FakeTty extends EventEmitter {
  chunks: string[] = [];
  columns = 40;
  rows = 8;
  isTTY = true;

  write(
    chunk: string | Uint8Array,
    cb?: (err?: Error | null) => void,
  ): boolean {
    this.chunks.push(
      typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"),
    );
    cb?.();

    return true;
  }
}

// Entering the alt screen used to write ESC[3J with it. Terminals (Windows
// Terminal, iTerm, kitty, xterm) honor that against the primary buffer's
// scrollback, so every launch destroyed the user's shell history (#151). The
// alt screen starts blank; nothing needs erasing.
describe("AlternateScreen", () => {
  it("enters the alternate screen without erasing the user's scrollback", () => {
    const stdout = new FakeTty();
    const stdin = new FakeTty();
    const stderr = new FakeTty();

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    });

    ink.render(
      React.createElement(
        AlternateScreen,
        null,
        React.createElement(Text, null, "hi"),
      ),
    );
    ink.onRender();

    const written = stdout.chunks.join("");

    expect(written).toContain(ENTER_ALT_SCREEN);
    expect(written).not.toContain(ERASE_SCROLLBACK);
    ink.unmount();
  });
});
