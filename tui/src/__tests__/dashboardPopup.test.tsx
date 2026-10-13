import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { patchOverlayState, resetOverlayState } from "../app/overlayStore.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import { Dashboard } from "../components/dashboard.js";
import type {
  DashboardChange,
  DashboardTabId,
  DashboardUi,
} from "../domain/dashboard.js";
import { DEFAULT_THEME } from "../theme.js";

import { waitFor } from "./waitFor.js";

class FakeInput extends EventEmitter {
  chunks: string[] = [];
  isRaw = false;
  isTTY = true;
  readableLength = 0;

  read() {
    const next = this.chunks.shift() ?? null;
    this.readableLength = this.chunks.length;

    return next;
  }

  ref = vi.fn();

  send(...chunks: string[]) {
    this.chunks.push(...chunks);
    this.readableLength = this.chunks.length;
    this.emit("readable");
  }

  setEncoding = vi.fn();

  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled;
  });

  unref = vi.fn();
}

const settle = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));

const DOWN = "\u001b[B";
const RIGHT = "\u001b[C";
const LEFT = "\u001b[D";
const ENTER = "\r";
const ESC = "\u001b";
const TAB = "\t";

function mount(tab: DashboardTabId = "look") {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 });
  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const onSave =
    vi.fn<(changes: DashboardChange[], patch: Partial<DashboardUi>) => void>();
  const onCancel = vi.fn();

  const instance = renderSync(
    <Dashboard
      initialTab={tab}
      onCancel={onCancel}
      onSave={onSave}
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  return {
    frame: () => stripAnsi(output),
    onCancel,
    onSave,
    stdin,
    stop: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}

beforeEach(() => {
  resetUiState();
  resetOverlayState();
});

afterEach(() => {
  resetUiState();
  resetOverlayState();
});

describe("Dashboard popup", () => {
  it("paints the tabs, the rows and the picked chips from the ui it found", async () => {
    patchUiState({ indicatorStyle: "emoji", statusBar: "bottom" });

    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Status bar"));
    expect(t.frame()).toContain("[Look]");
    expect(t.frame()).toContain("[bottom]");
    expect(t.frame()).toContain("[emoji]");
    t.stop();
  });

  it("paints a change live and Esc puts the ui back", async () => {
    const t = mount();

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    t.stdin.send(ESC);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(getUiState().statusBar).toBe("top");
    expect(t.onSave).not.toHaveBeenCalled();
    t.stop();
  });

  it("closing without saving (Ctrl+C unmounts it) restores the ui too", async () => {
    const t = mount();

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    t.stop();

    expect(getUiState().statusBar).toBe("top");
  });

  it("Enter hands the changes and the whole draft to onSave and leaves the ui as painted", async () => {
    const t = mount("input");

    await settle();
    t.stdin.send(`${RIGHT}${DOWN}${RIGHT}${DOWN}${RIGHT}${ENTER}`);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "mouse", value: "buttons" },
      { key: "busy", value: "steer" },
      { key: "focus", value: "on" },
    ]);
    expect(t.onSave.mock.calls[0]![1]).toMatchObject({
      busyInputMode: "steer",
      focusView: true,
      mouseTracking: "buttons",
    });
    t.stop();
    // saved: the unmount must not undo what the user saved
    expect(getUiState().mouseTracking).toBe("buttons");
    expect(getUiState().busyInputMode).toBe("steer");
  });

  it("the Context tab hands its change over as the /autocompact command", async () => {
    const t = mount("context");

    await waitFor(() => expect(t.frame()).toContain("Autocompact"));
    t.stdin.send(`${RIGHT}${RIGHT}${ENTER}`);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { command: "/autocompact 60%", key: "autocompact", value: "60%" },
    ]);
    t.stop();
  });

  it("a field that waits for Enter does not touch the ui before it", async () => {
    const t = mount("input");

    await settle();
    t.stdin.send(`${DOWN}${DOWN}${RIGHT}`);
    await settle();
    expect(getUiState().focusView).toBe(false);
    t.stop();
  });

  it("Tab changes tab and the details rows show", async () => {
    const t = mount();

    await settle();
    t.stdin.send(TAB);
    await waitFor(() => expect(t.frame()).toContain("Tool calls"));
    expect(t.frame()).toContain("[Details]");
    t.stop();
  });

  it("ignores keys while a question is open", async () => {
    patchOverlayState({
      approval: { command: "ls", description: "", requestId: "a" },
    });

    const t = mount();

    await settle();
    t.stdin.send(`${RIGHT}${ENTER}${ESC}`);
    await settle(120);

    expect(getUiState().statusBar).toBe("top");
    expect(t.onSave).not.toHaveBeenCalled();
    expect(t.onCancel).not.toHaveBeenCalled();
    expect(t.frame()).toContain("Answer the question above first");
    t.stop();
  });

  it("two keys that land before a re-render are both counted", async () => {
    const t = mount();

    await settle();
    t.stdin.send(`${DOWN}${RIGHT}${LEFT}${RIGHT}`);
    await settle(120);
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "indicator", value: "unicode" },
    ]);
    t.stop();
  });
});
