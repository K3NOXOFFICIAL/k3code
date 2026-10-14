import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ComposerToken } from "../app/interfaces.js";
import {
  appendShellOutput,
  resetShellRunsForTests,
  shellRunCount,
  shellRunsToCancel,
} from "../app/shellRunStore.js";
import { turnController } from "../app/turnController.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import {
  prepareSubmission,
  shouldInterpolateSubmission,
  useSubmission,
} from "../app/useSubmission.js";
import type { GatewayClient } from "../gatewayClient.js";
import type { Msg } from "../types.js";

describe("prepareSubmission", () => {
  it("keeps the collapsed paste for display and expands the model payload", () => {
    const label = "[[ first.. [3 lines] .. last ]]";
    const tokens: ComposerToken[] = [
      { kind: "paste", label, text: "first\nmiddle\nlast" },
    ];

    expect(prepareSubmission(`review this: ${label}`, tokens)).toEqual({
      display: `review this: ${label}`,
      pasteSpans: [[13, 30]],
      text: "review this: first\nmiddle\nlast",
    });
  });

  it("does not execute interpolation syntax hidden inside pasted content", () => {
    const label = "[[ copied log [1 lines] ]]";
    const tokens: ComposerToken[] = [
      { kind: "paste", label, text: "untrusted {!touch /tmp/pwned}" },
    ];
    const submission = prepareSubmission(label, tokens);

    expect(shouldInterpolateSubmission(submission.display)).toBe(false);
    expect(submission.text).toContain("{!touch /tmp/pwned}");
  });
});

describe("visible interpolation combined with a collapsed paste", () => {
  it("routes to interpolation when {!...} is visible in the composer alongside a paste token", () => {
    const label = "[[ log [2 lines] ]]";

    expect(shouldInterpolateSubmission(`show {!date} for ${label}`)).toBe(true);
  });

  // The interpolation branch of dispatchSubmission submits
  //   send(prepareSubmission(text, tokens).text, true, text, identity)
  // where `text` is interpolate()'s output: the visible {!...} already resolved,
  // with the collapsed paste label still intact. This asserts both halves of
  // that composition so the transcript shows resolved interpolation + the
  // compact paste, while the model receives resolved interpolation + the full
  // expanded paste.
  it("display keeps resolved interpolation and the compact paste; payload expands the paste", () => {
    const label = "[[ log [2 lines] ]]";
    const tokens: ComposerToken[] = [
      { kind: "paste", label, text: "line one\nline two" },
    ];

    // interpolate() has resolved the visible {!date} -> "Tue" and left the paste label alone.
    const interpolated = `Tue for ${label}`;
    const submission = prepareSubmission(interpolated, tokens);

    expect(submission.display).toBe(`Tue for ${label}`);
    expect(submission.display).not.toContain("{!");
    expect(submission.text).toBe("Tue for line one\nline two");
  });
});

// The real hook, with a gateway whose `shell.exec` answers when the test says so.
function mountSubmission(slash = vi.fn(() => true)) {
  const shells: ((reply: unknown) => void)[] = [];
  const prompts: Record<string, unknown>[] = [];
  const clearIn = vi.fn();
  const enqueue = vi.fn();
  const pushHistory = vi.fn();
  const sys = vi.fn();
  // The transcript the hook appends to and patches.
  const messages: Msg[] = [];
  const appendMessage = (msg: Msg) => messages.push(msg);
  const setHistoryItems = (value: ((prev: Msg[]) => Msg[]) | Msg[]) =>
    messages.splice(
      0,
      messages.length,
      ...(typeof value === "function" ? value([...messages]) : value),
    );

  const gw = {
    request: vi.fn((method: string, params?: Record<string, unknown>) => {
      if (method === "shell.exec") {
        return new Promise((resolve) => shells.push(resolve));
      }

      if (method === "prompt.submit") {
        prompts.push(params ?? {});
      }

      return Promise.resolve(
        method === "input.detect_drop" ? { matched: false } : { status: "ok" },
      );
    }),
  } as unknown as GatewayClient;

  let api: null | ReturnType<typeof useSubmission> = null;

  function Probe() {
    const submission = useSubmission({
      appendMessage,
      composerActions: {
        clearIn,
        enqueue,
        prependQueue: vi.fn(),
        pushHistory,
        setQueueEdit: vi.fn(),
      } as never,
      composerRefs: {
        queueEditRef: { current: null },
        queueRef: { current: [] },
        tokensRef: { current: [] },
      } as never,
      composerState: {
        completions: [],
        input: "",
        inputBuf: [],
        queueEditIdx: null,
        queuedDisplay: [],
      } as never,
      gw,
      setHistoryItems,
      setLastUserMsg: vi.fn(),
      slashRef: { current: slash },
      submitRef: { current: () => {} },
      sys,
    });

    useEffect(() => {
      api = submission;
    });

    return null;
  }

  const stream = () =>
    Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 });

  renderSync(React.createElement(Probe), {
    patchConsole: false,
    stderr: stream() as unknown as NodeJS.WriteStream,
    stdin: stream() as unknown as NodeJS.ReadStream,
    stdout: stream() as unknown as NodeJS.WriteStream,
  });

  return {
    clearIn,
    enqueue,
    gw,
    messages,
    prompts,
    pushHistory,
    shells,
    slash,
    sys,
    sub: async () => {
      await vi.waitFor(() => expect(api).toBeTruthy());

      return api!;
    },
  };
}

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

describe("a !cmd and the session's busy state", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("typed mid-turn, it leaves the running turn busy when it ends", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    patchUiState({ busy: true, status: "thinking…" });
    sub.dispatchSubmission("!ls");
    expect(mounted.shells).toHaveLength(1);
    expect(getUiState()).toMatchObject({ busy: true, status: "thinking…" });

    mounted.shells[0]!({ code: 0, stdout: "a.txt" });
    await settle();

    expect(getUiState()).toMatchObject({ busy: true, status: "thinking…" });
  });

  it("typed while idle, it holds busy until it ends", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!ls");
    expect(getUiState()).toMatchObject({ busy: true, status: "running…" });

    mounted.shells[0]!({ code: 0, stdout: "a.txt" });
    await settle();

    expect(getUiState()).toMatchObject({ busy: false, status: "ready" });
  });

  it("overlapping ones release busy only when the last ends", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!sleep 1");
    sub.dispatchSubmission("!sleep 2");

    mounted.shells[0]!({ code: 0, stdout: "" });
    await settle();
    expect(getUiState().busy).toBe(true);

    mounted.shells[1]!({ code: 0, stdout: "" });
    await settle();
    expect(getUiState().busy).toBe(false);
  });

  it("a queued one the drain sent still releases the busy the drain set for it", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    // the drain marks the session busy, then sends the item
    patchUiState({ busy: true, status: "running…" });
    sub.sendQueued("!echo hi");
    mounted.shells[0]!({ code: 0, stdout: "hi" });
    await settle();

    expect(getUiState()).toMatchObject({ busy: false, status: "ready" });
  });
});

describe("a queued item sent early (Ctrl+K, a double Enter)", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("without pastes goes out as the drain sends it, and leaves the draft alone", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("/clear all of it", [], "/clear all of it");
    await vi.waitFor(() => expect(mounted.prompts).toHaveLength(1));

    expect(mounted.slash).not.toHaveBeenCalled();
    expect(mounted.clearIn).not.toHaveBeenCalled();
    expect(mounted.prompts[0]).toMatchObject({ text: "/clear all of it" });
  });
});

describe("a typed !cmd", () => {
  beforeEach(() => {
    resetUiState();
    resetShellRunsForTests();
    patchUiState({ sid: "sess-1" });
  });

  const execCalls = (gw: GatewayClient) =>
    vi
      .mocked(gw.request)
      .mock.calls.filter(([method]) => method === "shell.exec");

  it("runs in the session, streamed and recorded, with a timeout past the gateway's 300 s", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!ls -la");

    const [[, params, timeout]] = execCalls(mounted.gw) as [
      [string, Record<string, unknown>, number],
    ];

    expect(params).toMatchObject({
      command: "ls -la",
      record: true,
      session_id: "sess-1",
      stream: true,
    });
    expect(params.exec_id).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    expect(timeout).toBeGreaterThanOrEqual(330_000);
    expect(mounted.pushHistory).toHaveBeenCalledWith("!ls -la");
    // known to the run store until it replies, so its shell.output lands
    expect(shellRunCount()).toBe(1);
    expect(
      appendShellOutput({ exec_id: String(params.exec_id), text: "a\n" }),
    ).toBe(true);

    mounted.shells[0]!({ code: 0, stdout: "a" });
    await settle();
    expect(shellRunCount()).toBe(0);
  });

  it("with no session yet is queued, not run", async () => {
    patchUiState({ sid: null });
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!ls");

    expect(execCalls(mounted.gw)).toHaveLength(0);
    expect(mounted.enqueue).toHaveBeenCalledWith("!ls", "!ls", []);
    expect(mounted.pushHistory).toHaveBeenCalledWith("!ls");
  });

  const shellMsgs = (messages: Msg[]) =>
    messages.filter((msg) => msg.kind === "shell");

  it("shows the !cmd bubble and one live shell message, patched in place when the reply lands", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!ls -la");

    const execId = String(execCalls(mounted.gw)[0]![1]!.exec_id);

    expect(mounted.messages).toEqual([
      { role: "user", text: "!ls -la" },
      {
        kind: "shell",
        role: "system",
        shell: { command: "ls -la", execId, running: true },
        text: "",
      },
    ]);

    mounted.shells[0]!({ code: 0, stderr: "", stdout: "a\nb" });
    await settle();

    expect(mounted.messages).toEqual([
      { role: "user", text: "!ls -la" },
      {
        kind: "shell",
        role: "system",
        shell: {
          cancelled: false,
          code: 0,
          command: "ls -la",
          execId,
          running: false,
          stderr: "",
          stdout: "a\nb",
          timedOut: false,
        },
        text: "",
      },
    ]);
    expect(mounted.sys).not.toHaveBeenCalled();
  });

  it("patches only the run whose exec id the reply carries", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!first");
    sub.dispatchSubmission("!second");
    mounted.shells[1]!({ code: 2, stdout: "two" });
    await settle();
    mounted.shells[0]!({ code: 0, stdout: "one" });
    await settle();

    expect(shellMsgs(mounted.messages).map((msg) => msg.shell)).toMatchObject([
      { code: 0, command: "first", running: false, stdout: "one" },
      { code: 2, command: "second", running: false, stdout: "two" },
    ]);
  });

  it("shows the gateway's reason in the block when the hardline rules deny it", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!env");
    mounted.shells[0]!({ code: 126, stderr: "denied: env dumps secrets" });
    await settle();

    expect(shellMsgs(mounted.messages)[0]?.shell).toMatchObject({
      code: 126,
      command: "env",
      running: false,
      stderr: "denied: env dumps secrets",
    });
    expect(mounted.sys).not.toHaveBeenCalled();
  });

  it("shows a transport error, stops the block and forgets the run", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    vi.mocked(mounted.gw.request).mockImplementationOnce(() =>
      Promise.reject(new Error("gateway not running")),
    );
    sub.dispatchSubmission("!ls");
    await settle();

    expect(shellMsgs(mounted.messages)[0]?.shell).toMatchObject({
      command: "ls",
      running: false,
    });
    expect(shellMsgs(mounted.messages)[0]?.shell?.code).toBeUndefined();
    expect(mounted.sys).toHaveBeenCalledWith("error: gateway not running");
    expect(shellRunCount()).toBe(0);
    expect(getUiState().busy).toBe(false);
  });

  it("marks a cancelled run", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!sleep 30");
    mounted.shells[0]!({ cancelled: true, code: 130, stdout: "" });
    await settle();

    expect(shellMsgs(mounted.messages)[0]?.shell).toMatchObject({
      cancelled: true,
      code: 130,
      running: false,
      timedOut: false,
    });
  });

  it("marks a timed-out run by its 124", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("!sleep 999");
    mounted.shells[0]!({ code: 124, stdout: "" });
    await settle();

    expect(shellMsgs(mounted.messages)[0]?.shell).toMatchObject({
      cancelled: false,
      code: 124,
      timedOut: true,
    });
  });
});

describe("{!cmd} interpolation", () => {
  beforeEach(() => {
    resetUiState();
    resetShellRunsForTests();
    patchUiState({ sid: "sess-1" });
  });

  it("runs one at a time in the session, unrecorded", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();

    sub.dispatchSubmission("compare {!date} with {!uptime}");
    await settle();

    // the second waits for the first: the gateway refuses more than 8 at once
    expect(mounted.shells).toHaveLength(1);
    mounted.shells[0]!({ code: 0, stdout: "Tue" });
    await vi.waitFor(() => expect(mounted.shells).toHaveLength(2));

    const calls = vi
      .mocked(mounted.gw.request)
      .mock.calls.filter(([method]) => method === "shell.exec");

    expect(calls.map(([, params]) => params)).toEqual([
      { command: "date", record: false, session_id: "sess-1" },
      { command: "uptime", record: false, session_id: "sess-1" },
    ]);

    mounted.shells[1]!({ code: 0, stdout: "up 3 days" });
    await vi.waitFor(() => expect(mounted.prompts).toHaveLength(1));
    expect(mounted.prompts[0]).toMatchObject({
      text: "compare Tue with up 3 days",
    });
  });
});

describe("a double Enter with a running !cmd", () => {
  beforeEach(() => {
    resetUiState();
    resetShellRunsForTests();
    patchUiState({ sid: "sess-1" });
  });

  afterEach(() => {
    resetShellRunsForTests();
    vi.restoreAllMocks();
  });

  const cancelCalls = (gw: GatewayClient) =>
    vi
      .mocked(gw.request)
      .mock.calls.filter(([method]) => method === "shell.cancel");

  it("cancels the !cmd instead of interrupting; the next double Enter reaches the turn", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();
    const interrupt = vi.spyOn(turnController, "interruptTurn");

    sub.dispatchSubmission("!sleep 9");
    expect(getUiState().busy).toBe(true);

    sub.submit("");
    sub.submit("");

    expect(cancelCalls(mounted.gw)).toEqual([
      ["shell.cancel", { session_id: "sess-1" }],
    ]);
    expect(interrupt).not.toHaveBeenCalled();
    expect(mounted.sys).toHaveBeenCalledWith(
      "cancelling the running ! command …",
    );
    expect(shellRunsToCancel()).toEqual([]);

    sub.submit("");

    expect(cancelCalls(mounted.gw)).toHaveLength(1);
    expect(interrupt).toHaveBeenCalledTimes(1);
  });

  it("with no !cmd running interrupts the turn as before", async () => {
    const mounted = mountSubmission();
    const sub = await mounted.sub();
    const interrupt = vi.spyOn(turnController, "interruptTurn");

    patchUiState({ busy: true, status: "thinking…" });
    sub.submit("");
    sub.submit("");

    expect(cancelCalls(mounted.gw)).toEqual([]);
    expect(interrupt).toHaveBeenCalledTimes(1);
  });
});
