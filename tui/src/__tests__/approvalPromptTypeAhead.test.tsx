import { EventEmitter } from "events";

import { renderSync } from "@k3code/ink";
import React from "react";
import { describe, expect, it } from "vitest";

import { ApprovalPrompt } from "../components/prompts.js";
import { DARK_THEME } from "../theme.js";

// The approval prompt takes every key while it is open (the composer is
// hidden). Text typed for the composer, or a letter shortcut the prompt does
// not have, must not leave the next Enter free to approve the selected row.

class FakeTty extends EventEmitter {
  chunks: string[] = [];
  columns = 80;
  rows = 24;
  isTTY = true;
  isRaw = false;
  private pendingReads: string[] = [];
  ref(): void {}
  unref(): void {}
  read(): string | null {
    return this.pendingReads.shift() ?? null;
  }
  send(chunk: string): void {
    this.pendingReads.push(chunk);
    this.emit("readable");
  }
  setEncoding(): this {
    return this;
  }
  setRawMode(mode: boolean): this {
    this.isRaw = mode;

    return this;
  }
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

const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

const DOWN = "\x1b[B";
const ENTER = "\r";

async function drive(reads: string[]): Promise<string[]> {
  const stdout = new FakeTty();
  const stdin = new FakeTty();
  const stderr = new FakeTty();
  const choices: string[] = [];

  const instance = renderSync(
    React.createElement(ApprovalPrompt, {
      onChoice: (c: string) => choices.push(c),
      req: { command: "cp a b", description: "copy" },
      t: DARK_THEME,
    }),
    {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  try {
    await tick();

    for (const r of reads) {
      stdin.send(r);
      await tick();
    }

    return choices;
  } finally {
    instance.unmount();
    instance.cleanup();
  }
}

describe("ApprovalPrompt ignores typed text", () => {
  it("a stray letter then Enter approves nothing", async () => {
    // `d` was documented as deny but was never a shortcut.
    expect(await drive(["d", ENTER])).toEqual([]);
    expect(await drive([`d${ENTER}`])).toEqual([]);
    // Kitty keyboard protocol: `d` and Enter are two keys of one read.
    expect(await drive([`\x1b[100u${ENTER}`])).toEqual([]);
  });

  it("text typed for the composer, then Enter, approves nothing", async () => {
    expect(await drive([..."fix the tests", ENTER])).toEqual([]);
    expect(await drive([`fix the tests${ENTER}`])).toEqual([]);
  });

  it("a burst or paste starting with a digit is not a quick pick", async () => {
    expect(await drive(["3 tests failed"])).toEqual([]);
    expect(await drive(["\x1b[200~3\x1b[201~", ENTER])).toEqual([]);
  });

  it("deny still answers once disarmed", async () => {
    expect(await drive(["d", ENTER, "4"])).toEqual(["deny"]);
  });

  it("↑/↓ re-arm Enter", async () => {
    expect(await drive(["d", ENTER, DOWN, ENTER])).toEqual(["session"]);
    expect(await drive([`d${DOWN}${ENTER}`])).toEqual(["session"]);
  });

  it("leaves the documented keys alone", async () => {
    expect(await drive([ENTER])).toEqual(["once"]);
    expect(await drive(["3"])).toEqual(["always"]);
    expect(await drive([DOWN, ENTER])).toEqual(["session"]);
  });
});
