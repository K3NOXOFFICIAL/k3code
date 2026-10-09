import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  $isBlocked,
  getOverlayState,
  patchOverlayState,
  resetFlowOverlays,
  resetOverlayState,
} from "../app/overlayStore.js";
import { findSlashCommand } from "../app/slash/registry.js";
import { turnController } from "../app/turnController.js";
import { resetTurnState } from "../app/turnStore.js";
import { resetUiState } from "../app/uiStore.js";
import { useSessionLifecycle } from "../app/useSessionLifecycle.js";

const runSlash = (name: string, arg = "") =>
  findSlashCommand(name)!.run(
    arg,
    {
      gateway: { gw: { request: vi.fn(() => Promise.resolve({})) } },
      guardedErr: vi.fn(),
      transcript: { sys: vi.fn() },
    } as never,
    `/${name}${arg ? ` ${arg}` : ""}`,
  );

/** Mount the real lifecycle hook against a gateway that never answers: only the synchronous part of each call runs. */
function mountLifecycle() {
  let api: null | ReturnType<typeof useSessionLifecycle> = null;
  const pending = () => new Promise<never>(() => {});

  function Probe() {
    const lifecycle = useSessionLifecycle({
      colsRef: { current: 80 },
      composerActions: { setComposerTokens: vi.fn() } as never,
      gw: { request: pending } as never,
      panel: vi.fn(),
      rpc: vi.fn(pending) as never,
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

beforeEach(() => {
  resetOverlayState();
  resetUiState();
  resetTurnState();
  turnController.fullReset();
});

afterEach(() => {
  resetOverlayState();
  resetUiState();
});

describe("/agents opens the agent view", () => {
  it("/agents with no argument opens the agent view, not the spawn tree", () => {
    runSlash("agents");

    expect(getOverlayState().agentView).toBe(true);
    expect(getOverlayState().agents).toBe(false);
  });

  it("/agents tree and /tasks open the spawn-tree dashboard", () => {
    runSlash("agents", "tree");

    expect(getOverlayState().agents).toBe(true);
    expect(getOverlayState().agentView).toBe(false);

    resetOverlayState();
    runSlash("tasks");

    expect(getOverlayState().agents).toBe(true);
    expect(getOverlayState().agentView).toBe(false);
  });

  it("keeps the delegation subcommands off both overlays", () => {
    runSlash("agents", "status");

    expect(getOverlayState().agentView).toBe(false);
    expect(getOverlayState().agents).toBe(false);
  });
});

describe("agent view overlay state", () => {
  it("blocks the composer while open", () => {
    expect($isBlocked.get()).toBe(false);

    patchOverlayState({ agentView: true });

    expect($isBlocked.get()).toBe(true);
  });

  it("survives the end of a turn", () => {
    patchOverlayState({ agentView: true, pager: { lines: ["x"], offset: 0 } });
    resetFlowOverlays();

    expect(getOverlayState().agentView).toBe(true);
    expect(getOverlayState().pager).toBeNull();
  });

  it.each([
    ["activating a live session", "activateLiveSession"],
    ["starting a new live session", "newLiveSession"],
    ["resuming a stored session", "resumeById"],
  ] as const)("%s closes it", async (_label, method) => {
    const lifecycle = mountLifecycle();

    try {
      await vi.waitFor(() => expect(lifecycle.api()).toBeTruthy());
      patchOverlayState({ agentView: true });

      void (lifecycle.api()[method] as (id?: string) => unknown)("sid-2");

      expect(getOverlayState().agentView).toBe(false);
    } finally {
      lifecycle.unmount();
    }
  });
});
