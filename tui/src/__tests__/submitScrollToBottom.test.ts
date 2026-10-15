import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { patchUiState, resetUiState } from "../app/uiStore.js";
import { useSubmission } from "../app/useSubmission.js";
import type { GatewayClient } from "../gatewayClient.js";

/**
 * Pressing Enter with a prompt returns the transcript to the bottom (#154): a user who scrolled up to read, then asks
 * a follow-up, must see the echo of the message and the streaming reply instead of the old output.
 */

function mountSubmission() {
  const scrollToBottom = vi.fn();

  const gw = {
    request: vi.fn((method: string) =>
      Promise.resolve(
        method === "input.detect_drop" ? { matched: false } : { status: "ok" },
      ),
    ),
  } as unknown as GatewayClient;

  let api: null | ReturnType<typeof useSubmission> = null;

  function Probe() {
    const submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions: {
        clearIn: vi.fn(),
        enqueue: vi.fn(),
        pushHistory: vi.fn(),
      } as never,
      composerRefs: {
        queueEditRef: { current: null },
        queueRef: { current: [] },
        tokensRef: { current: [] },
      } as never,
      composerState: { completions: [], input: "", inputBuf: [] } as never,
      gw,
      scrollRef: { current: { scrollToBottom } } as never,
      setLastUserMsg: vi.fn(),
      slashRef: { current: () => true },
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
    api: async () => {
      await vi.waitFor(() => expect(api).toBeTruthy());

      return api!;
    },
    scrollToBottom,
  };
}

describe("submit returns the transcript to the bottom", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("a typed prompt scrolls to the bottom", async () => {
    const { api, scrollToBottom } = mountSubmission();

    (await api()).submit("hello msg 5");

    expect(scrollToBottom).toHaveBeenCalledTimes(1);
  });

  it("a slash command scrolls to the bottom too", async () => {
    const { api, scrollToBottom } = mountSubmission();

    (await api()).submit("/help");

    expect(scrollToBottom).toHaveBeenCalledTimes(1);
  });

  it("an empty Enter leaves the view where the user put it", async () => {
    const { api, scrollToBottom } = mountSubmission();

    (await api()).submit("   ");

    expect(scrollToBottom).not.toHaveBeenCalled();
  });

  it("a queued prompt sent when the turn ends does not yank the view", async () => {
    const { api, scrollToBottom } = mountSubmission();

    (await api()).sendQueued("queued follow-up");

    expect(scrollToBottom).not.toHaveBeenCalled();
  });
});
