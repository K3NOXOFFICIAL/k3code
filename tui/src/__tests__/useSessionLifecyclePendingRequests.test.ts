import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import type { ServerRequest } from "@k3code/shared/json-rpc-channel";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createServerRequestHandler } from "../app/createServerRequestHandler.js";
import { getOverlayState, resetOverlayState } from "../app/overlayStore.js";
import {
  hasOpenServerRequest,
  resetServerRequestsForTests,
  respondToServerRequest,
} from "../app/serverRequestStore.js";
import { turnController } from "../app/turnController.js";
import { resetTurnState } from "../app/turnStore.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import { useSessionLifecycle } from "../app/useSessionLifecycle.js";

const onServerRequest = createServerRequestHandler({
  ringPromptBell: vi.fn(),
  setStatus: (status) => patchUiState({ status }),
});

/** Mount the real hook, wired to the real server-request handler as useMainApp wires it. */
function mountLifecycle(request: (method: string) => Promise<unknown>) {
  let api: null | ReturnType<typeof useSessionLifecycle> = null;

  function Probe() {
    const lifecycle = useSessionLifecycle({
      colsRef: { current: 80 },
      composerActions: { setComposerTokens: vi.fn() } as any,
      gw: { request } as any,
      panel: vi.fn(),
      reopenServerRequest: (r) => {
        onServerRequest(r);
      },
      rpc: vi.fn(async () => null),
      scrollRef: { current: null },
      setHistoryItems: vi.fn(),
      setLastUserMsg: vi.fn(),
      setSessionStartedAt: vi.fn(),
      setStickyPrompt: vi.fn(),
      sys: vi.fn(),
    });

    useEffect(() => {
      api = lifecycle;
    });

    return null;
  }

  const stream = () =>
    Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 });

  const instance = renderSync(React.createElement(Probe), {
    patchConsole: false,
    stderr: stream() as unknown as NodeJS.WriteStream,
    stdin: stream() as unknown as NodeJS.ReadStream,
    stdout: stream() as unknown as NodeJS.WriteStream,
  });

  return { api: () => api!, unmount: () => instance.unmount() };
}

const activated = (sid: string) => ({
  messages: [],
  running: false,
  session_id: sid,
  status: "needs_input",
});

const approvalFor = (sid: string, respond = vi.fn()): ServerRequest => ({
  fail: vi.fn(),
  id: `approval-${sid}`,
  method: "approval",
  params: { command: "rm -rf build", session_id: sid },
  replayed: true,
  respond,
});

describe("attaching a session that waits for an approval", () => {
  beforeEach(() => {
    resetUiState();
    resetTurnState();
    resetOverlayState();
    resetServerRequestsForTests();
    turnController.fullReset();
    patchUiState({ sid: "sid-A" });
  });

  it("keeps the approval the gateway re-sent before the activate .then reset the view", async () => {
    let answer: (v: unknown) => void = () => {};
    const request = vi.fn(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const lifecycle = mountLifecycle(request);

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      lifecycle.api().activateLiveSession("sid-B");

      // The order seen live: the activate response, then the re-sent approval, then the `.then` (resetSession).
      answer(activated("sid-B"));
      const respond = vi.fn();
      onServerRequest(approvalFor("sid-B", respond));

      await vi.waitFor(() => expect(getUiState().sid).toBe("sid-B"));
      expect(getOverlayState().approval?.requestId).toBe("approval-sid-B");
      expect(getUiState().status).toBe("approval needed");
      expect(respondToServerRequest("approval-sid-B", { choice: "once" })).toBe(
        true,
      );
      expect(respond).toHaveBeenCalledWith({ choice: "once" });
    } finally {
      lifecycle.unmount();
    }
  });

  it("does not bring back a left session's request the gateway no longer re-sends", async () => {
    const request = vi.fn(async (_method: string) => activated("sid-C"));
    const lifecycle = mountLifecycle(request);

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      patchUiState({ sid: "sid-B" });
      onServerRequest(approvalFor("sid-B")); // shown while on B, then the user leaves B unanswered

      lifecycle.api().activateLiveSession("sid-C");
      await vi.waitFor(() => expect(getUiState().sid).toBe("sid-C"));
      expect(getOverlayState().approval).toBeNull();
      expect(hasOpenServerRequest("approval-sid-B")).toBe(false);

      // Answered elsewhere meanwhile: back on B the gateway re-sends nothing, so no dead card may appear.
      request.mockImplementation(async () => activated("sid-B"));
      lifecycle.api().activateLiveSession("sid-B");
      await vi.waitFor(() => expect(getUiState().sid).toBe("sid-B"));
      expect(getOverlayState().approval).toBeNull();
    } finally {
      lifecycle.unmount();
    }
  });
});
