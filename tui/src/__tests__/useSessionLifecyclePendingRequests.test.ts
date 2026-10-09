import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import {
  JsonRpcGatewayError,
  type ServerRequest,
} from "@k3code/shared/json-rpc-channel";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createGatewayEventHandler } from "../app/createGatewayEventHandler.js";
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

type Rpc = (
  method: string,
  params?: Record<string, unknown>,
) => Promise<unknown>;

/** Mount the real hook, wired to the real server-request handler as useMainApp wires it. */
function mountLifecycle(
  request: Rpc,
  {
    rpc = vi.fn(async () => null),
    sys = vi.fn(),
  }: { rpc?: Rpc; sys?: (text: string) => void } = {},
) {
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
      rpc: rpc as any,
      scrollRef: { current: null },
      setHistoryItems: vi.fn(),
      setLastUserMsg: vi.fn(),
      setSessionStartedAt: vi.fn(),
      setStickyPrompt: vi.fn(),
      sys,
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

  it("does not bring back a request of the session /new left", async () => {
    const request = vi.fn(async (_method: string) => activated("sid-B"));
    const rpc = vi.fn(async (method: string) =>
      method === "session.create" ? { session_id: "sid-N" } : null,
    );
    const lifecycle = mountLifecycle(request, { rpc });

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      patchUiState({ sid: "sid-B" });
      onServerRequest(approvalFor("sid-B")); // shown while on B, then /new leaves B unanswered

      await lifecycle.api().newSession();
      expect(getUiState().sid).toBe("sid-N");
      expect(hasOpenServerRequest("approval-sid-B")).toBe(false);

      // Back on B the gateway re-sends nothing (answered elsewhere meanwhile), so no dead card may appear.
      lifecycle.api().activateLiveSession("sid-B");
      await vi.waitFor(() => expect(getUiState().sid).toBe("sid-B"));
      expect(getOverlayState().approval).toBeNull();
    } finally {
      lifecycle.unmount();
    }
  });
});

/** The gateway.ready handler as useMainApp builds it, recovering `recoverSid` through the real `resumeById`. */
const readyHandler = (
  lifecycle: ReturnType<typeof mountLifecycle>,
  recoverSid: string,
  newSession = vi.fn(),
) => {
  const recoverSidRef = { current: recoverSid as null | string };
  const onEvent = createGatewayEventHandler({
    composer: { setInput: vi.fn() },
    gateway: { gw: { request: vi.fn() }, rpc: vi.fn(async () => null) },
    session: {
      STARTUP_RESUME_ID: "",
      STARTUP_VIEW: "",
      colsRef: { current: 80 },
      newSession,
      recoverSidRef,
      resetSession: vi.fn(),
      resumeById: (id: string) => lifecycle.api().resumeById(id),
      setCatalog: vi.fn(),
    },
    submission: {
      submitLiteralRef: { current: vi.fn() },
      submitRef: { current: vi.fn() },
    },
    system: { bellOnComplete: false, sys: vi.fn() },
    transcript: {
      appendMessage: vi.fn(),
      panel: vi.fn(),
      setHistoryItems: vi.fn(),
    },
  } as any);

  return {
    newSession,
    ready: () => onEvent({ payload: {}, type: "gateway.ready" } as any),
    recoverSidRef,
  };
};

describe("recovering the session after the gateway came back", () => {
  beforeEach(() => {
    resetUiState();
    resetTurnState();
    resetOverlayState();
    resetServerRequestsForTests();
    turnController.fullReset();
  });

  it("does not reopen an approval stored before the transport dropped", async () => {
    const request = vi.fn(async (_method: string) => activated("sid-A"));
    const lifecycle = mountLifecycle(request);

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      patchUiState({ sid: "sid-A", storedSid: "sid-A" });
      onServerRequest(approvalFor("sid-A")); // shown, then the daemon restarts before it is answered

      // The exit handler clears sid and keeps the stored id for recovery; the old card goes with the reset.
      patchUiState({ sid: null });
      resetOverlayState();

      const { ready, recoverSidRef } = readyHandler(lifecycle, "sid-A");

      ready();
      await vi.waitFor(() => expect(recoverSidRef.current).toBeNull());
      expect(getUiState().sid).toBe("sid-A");
      // The new gateway does not know the old id (it re-sends what is still open itself), so no card may appear.
      expect(getOverlayState().approval).toBeNull();
      expect(hasOpenServerRequest("approval-sid-A")).toBe(false);
    } finally {
      lifecycle.unmount();
    }
  });

  it("starts a fresh session when the recovered one was reaped while empty", async () => {
    const request = vi.fn(async (_method: string) => {
      throw new JsonRpcGatewayError("unknown session: sid-A", { code: -32602 });
    });
    const lifecycle = mountLifecycle(request);

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());

      const { newSession, ready, recoverSidRef } = readyHandler(
        lifecycle,
        "sid-A",
      );

      ready();
      await vi.waitFor(() => expect(newSession).toHaveBeenCalledTimes(1));
      expect(request).toHaveBeenCalledWith(
        "session.resume",
        expect.objectContaining({ session_id: "sid-A" }),
      );
      expect(recoverSidRef.current).toBeNull();
    } finally {
      lifecycle.unmount();
    }
  });

  it("keeps the recovery target, forging nothing, when the resume fails for another reason", async () => {
    const request = vi.fn(async (_method: string) => {
      throw new Error("gateway not connected: session.resume");
    });
    const lifecycle = mountLifecycle(request);

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());

      const { newSession, ready, recoverSidRef } = readyHandler(
        lifecycle,
        "sid-A",
      );

      ready();
      await vi.waitFor(() => expect(getUiState().status).toBe("ready"));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(newSession).not.toHaveBeenCalled();
      expect(recoverSidRef.current).toBe("sid-A");
    } finally {
      lifecycle.unmount();
    }
  });
});

describe("leaving a session for another one", () => {
  beforeEach(() => {
    resetUiState();
    resetTurnState();
    resetOverlayState();
    resetServerRequestsForTests();
    turnController.fullReset();
    patchUiState({ sid: "sid-A" });
  });

  const closeCalls = (request: ReturnType<typeof vi.fn>) =>
    request.mock.calls.filter(([method]) => method === "session.close");

  it("asks the gateway to close the left session if disposable, once attached elsewhere, and swallows a refusal", async () => {
    const request = vi.fn(async (method: string) => {
      if (method === "session.close") {
        throw new Error("still in use");
      }

      return activated("sid-B");
    });
    const sys = vi.fn();
    const lifecycle = mountLifecycle(request, { sys });

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      lifecycle.api().activateLiveSession("sid-B", "sid-A");

      await vi.waitFor(() => expect(closeCalls(request)).toHaveLength(1));
      expect(getUiState().sid).toBe("sid-B");
      expect(closeCalls(request)[0]).toEqual([
        "session.close",
        { disposable_only: true, session_id: "sid-A" },
      ]);
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(sys).not.toHaveBeenCalled();
    } finally {
      lifecycle.unmount();
    }
  });

  it("does the same after starting a new live session", async () => {
    const request = vi.fn(async (_method: string) => ({
      closed: false,
      reason: "has messages",
    }));
    const rpc = vi.fn(async (method: string) =>
      method === "session.create" ? { session_id: "sid-N" } : null,
    );
    const lifecycle = mountLifecycle(request, { rpc });

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      await lifecycle.api().newLiveSession(undefined, undefined, "sid-A");

      await vi.waitFor(() => expect(closeCalls(request)).toHaveLength(1));
      expect(closeCalls(request)[0]).toEqual([
        "session.close",
        { disposable_only: true, session_id: "sid-A" },
      ]);
      expect(getUiState().sid).toBe("sid-N");
    } finally {
      lifecycle.unmount();
    }
  });
});
