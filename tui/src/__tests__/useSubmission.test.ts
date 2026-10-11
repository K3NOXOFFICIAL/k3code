import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ComposerToken } from "../app/interfaces.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import {
  prepareSubmission,
  shouldInterpolateSubmission,
  useSubmission,
} from "../app/useSubmission.js";
import type { GatewayClient } from "../gatewayClient.js";

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
      appendMessage: vi.fn(),
      composerActions: {
        clearIn,
        enqueue: vi.fn(),
        prependQueue: vi.fn(),
        pushHistory: vi.fn(),
        setQueueEdit: vi.fn(),
      } as never,
      composerRefs: {
        queueEditRef: { current: null },
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
      setLastUserMsg: vi.fn(),
      slashRef: { current: slash },
      submitRef: { current: () => {} },
      sys: vi.fn(),
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
    prompts,
    shells,
    slash,
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
