import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getOverlayState,
  openTunePicker,
  patchOverlayState,
  resetOverlayState,
  resetFlowOverlays,
  $isBlocked,
  hasFloatingPanel,
} from "../app/overlayStore.js";
import { FloatBox } from "../components/appChrome.js";
import { TunePicker } from "../components/tunePicker.js";
import type { GatewayClient } from "../gatewayClient.js";
import type { TuneSetParams } from "../gatewayTypes.js";
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
const UP = "\u001b[A";
const RIGHT = "\u001b[C";
const ENTER = "\r";
const TAB = "\t";

const snapshot = (over: Record<string, unknown> = {}) => ({
  default_model: "main",
  effort: "high",
  efforts: ["low", "medium", "high", "xhigh", "max"],
  has_session: true,
  model: "main",
  models: [
    { description: "Everyday", effort: true, key: "main", resolved: ["m-4"] },
    { description: "", effort: true, key: "fast", resolved: ["f-4"] },
    { description: "", effort: false, key: "tiny", resolved: ["tiny"] },
  ],
  ultra_mode: "off",
  wake_words: ["ultracode", "ultraplan", "ultraresearch"],
  wake_words_enabled: true,
  ...over,
});

function mount(
  opts: {
    reply?: () => Promise<unknown>;
    sessionId?: null | string;
    snap?: Record<string, unknown>;
  } = {},
  rows = 24,
) {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 80, isTTY: false, rows });
  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const request = vi.fn(
    opts.reply ?? (() => Promise.resolve(snapshot(opts.snap))),
  );

  const onApply = vi.fn<(params: TuneSetParams) => void>();
  const onCancel = vi.fn();

  const instance = renderSync(
    <FloatBox color={DEFAULT_THEME.color.border}>
      <TunePicker
        gw={{ request } as unknown as GatewayClient}
        onApply={onApply}
        onCancel={onCancel}
        sessionId={opts.sessionId === undefined ? "sid-1" : opts.sessionId}
        t={DEFAULT_THEME}
      />
    </FloatBox>,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  return {
    frame: () => stripAnsi(output),
    instance,
    onApply,
    onCancel,
    request,
    stdin,
    stop: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}

afterEach(() => {
  resetOverlayState();
});

describe("TunePicker", () => {
  it("asks the gateway for tune.get with the session id", async () => {
    const t = mount();

    await waitFor(() => expect(t.request).toHaveBeenCalled());
    expect(t.request).toHaveBeenCalledWith("tune.get", { session_id: "sid-1" });
    t.stop();
  });

  it("asks without a session id when there is none", async () => {
    const t = mount({ sessionId: null, snap: { has_session: false } });

    await waitFor(() => expect(t.request).toHaveBeenCalled());
    expect(t.request).toHaveBeenCalledWith("tune.get", {});
    t.stop();
  });

  it("Enter sends the model, effort and ultracode changes in one payload and keeps the popup's own close to the caller", async () => {
    const t = mount();

    await settle();
    t.stdin.send(DOWN);
    await settle();
    t.stdin.send(RIGHT);
    await settle();
    t.stdin.send(TAB);
    await settle();
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onApply).toHaveBeenCalledTimes(1));

    expect(t.onApply).toHaveBeenCalledWith({
      effort: "xhigh",
      model: "fast",
      scope: "default",
      session_id: "sid-1",
      ultra_mode: "ultracode",
    });
    expect(t.onCancel).not.toHaveBeenCalled();
    t.stop();
  });

  it("two keys that land before a re-render are both counted", async () => {
    const t = mount();

    await settle();
    t.stdin.send(`${DOWN}${DOWN}${ENTER}`);
    await waitFor(() => expect(t.onApply).toHaveBeenCalledTimes(1));

    expect(t.onApply.mock.calls[0]![0]).toMatchObject({ model: "tiny" });
    t.stop();
  });

  it("`s` applies for this session only", async () => {
    const t = mount();

    await settle();
    t.stdin.send(UP);
    await settle();
    t.stdin.send("2");
    await settle();
    t.stdin.send("s");
    await waitFor(() => expect(t.onApply).toHaveBeenCalledTimes(1));

    expect(t.onApply).toHaveBeenCalledWith({
      model: "fast",
      scope: "session",
      session_id: "sid-1",
    });
    t.stop();
  });

  it("Esc closes and applies nothing", async () => {
    const t = mount();

    await settle();
    t.stdin.send(DOWN);
    await settle();
    t.stdin.send("\u001b");
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(t.onApply).not.toHaveBeenCalled();
    t.stop();
  });

  it("Enter with nothing changed just closes", async () => {
    const t = mount();

    await settle();
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(t.onApply).not.toHaveBeenCalled();
    t.stop();
  });

  it("without a session: effort and Tab are ignored, the model can still be set (no session id)", async () => {
    const t = mount({
      sessionId: null,
      snap: { effort: null, has_session: false },
    });

    await settle();
    t.stdin.send(RIGHT);
    await settle();
    t.stdin.send(TAB);
    await settle();
    t.stdin.send(DOWN);
    await settle();
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onApply).toHaveBeenCalledTimes(1));

    expect(t.onApply).toHaveBeenCalledWith({
      model: "fast",
      scope: "default",
    });
    t.stop();
  });

  it.each([
    [
      "approval",
      { approval: { command: "ls", description: "", requestId: "a" } },
    ],
    [
      "clarify",
      { clarify: { choices: null, question: "which?", requestId: "c" } },
    ],
    ["confirm", { confirm: { onConfirm: () => {}, title: "sure?" } }],
    [
      "secret",
      { secret: { envVar: "TOKEN", prompt: "token", requestId: "s" } },
    ],
    ["sudo", { sudo: { requestId: "p" } }],
  ])("ignores every key while a %s prompt is open", async (_name, overlay) => {
    const t = mount();

    await settle();
    patchOverlayState(overlay as never);
    await settle();
    t.stdin.send(DOWN);
    await settle();
    t.stdin.send(ENTER);
    await settle();
    t.stdin.send("s");
    await settle();
    t.stdin.send("\u001b");
    await settle(150);

    expect(t.onApply).not.toHaveBeenCalled();
    expect(t.onCancel).not.toHaveBeenCalled();

    // the prompt is answered: the popup is live again, with its state intact
    resetOverlayState();
    await settle();
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));
    t.stop();
  });

  it("shows the error and still closes on Esc when tune.get fails", async () => {
    const t = mount({
      reply: () => Promise.reject(new Error("no such method")),
    });

    await settle();
    t.stdin.send("\u001b");
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));
    t.stop();
    expect(t.frame()).toContain("error:");
  });

  it("rejects a tune.get that is not the shape", async () => {
    const t = mount({ reply: () => Promise.resolve({ nope: true }) });

    await settle();
    t.stop();
    expect(t.frame()).toContain("invalid response: tune.get");
  });

  it("paints the popup: title, cursor row, ladder and both footer lines, inside 80x24", async () => {
    const t = mount({
      snap: {
        model: "m1",
        models: Array.from({ length: 16 }, (_, i) => ({
          key: i === 0 ? "main" : `m${i}`,
          resolved: [`vendor/model-${i}`],
        })),
      },
    });

    await settle(150);
    t.stop();

    // the output is every frame in a row (loading, then the popup): measure the last box only
    const all = t.frame();
    const frame = all.slice(all.lastIndexOf("╔"), all.lastIndexOf("╝") + 1);
    const lines = frame.split("\n").filter((l) => l.trim());

    expect(frame).toContain("Tune");
    expect(frame).toContain("Default (recommended)");
    expect(frame).toContain("❯");
    expect(frame).toContain("▲");
    expect(frame).toMatch(/default\s+low\s+medium\s+high\s+xhigh\s+max/);
    // 80 columns: the key line is the shortened one, Esc still named
    expect(frame).toMatch(/Tab ultracode · Enter default · s session · Esc\s/);
    expect(frame).toContain("ultraresearch");
    // box + margin + the status rule under it must leave the 24 rows enough room
    expect(lines.length + 3).toBeLessThanOrEqual(24);
  });
});

describe("tunePicker overlay state", () => {
  it("is a floating, blocking panel that survives a turn ending", () => {
    expect(openTunePicker()).toBe(true);
    expect(getOverlayState().tunePicker).toBe(true);
    expect($isBlocked.get()).toBe(true);
    expect(hasFloatingPanel(getOverlayState())).toBe(true);

    resetFlowOverlays();
    expect(getOverlayState().tunePicker).toBe(true);
  });

  it.each([
    [
      "approval",
      { approval: { command: "ls", description: "", requestId: "a" } },
    ],
    ["clarify", { clarify: { choices: null, question: "?", requestId: "c" } }],
    ["confirm", { confirm: { onConfirm: () => {}, title: "sure?" } }],
    ["secret", { secret: { envVar: "T", prompt: "t", requestId: "s" } }],
    ["sudo", { sudo: { requestId: "p" } }],
  ])("does not open over a %s prompt", (_name, overlay) => {
    patchOverlayState(overlay as never);

    expect(openTunePicker()).toBe(false);
    expect(getOverlayState().tunePicker).toBe(false);
  });
});
