import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ComposerToken } from "../app/interfaces.js";
import { turnController } from "../app/turnController.js";
import { getTurnState, resetTurnState } from "../app/turnStore.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import {
  hydrateLiveSessionInflight,
  liveSessionInflightMessages,
  signalFreshSessionBoundary,
  useSessionLifecycle,
  writeActiveSessionFile,
} from "../app/useSessionLifecycle.js";

describe("fresh session boundary", () => {
  it("signals only when a live session is replaced by a different session", () => {
    const onFreshSessionStarted = vi.fn();

    expect(
      signalFreshSessionBoundary(
        "old-session",
        "new-session",
        onFreshSessionStarted,
      ),
    ).toBe(true);
    expect(
      signalFreshSessionBoundary(null, "first-session", onFreshSessionStarted),
    ).toBe(false);
    expect(
      signalFreshSessionBoundary(
        "same-session",
        "same-session",
        onFreshSessionStarted,
      ),
    ).toBe(false);
    expect(
      signalFreshSessionBoundary("old-session", null, onFreshSessionStarted),
    ).toBe(false);
    expect(signalFreshSessionBoundary("old-session", "new-session")).toBe(
      false,
    );
    expect(onFreshSessionStarted).toHaveBeenCalledOnce();
    expect(onFreshSessionStarted).toHaveBeenCalledWith("new-session");
  });
});

describe("writeActiveSessionFile", () => {
  let dir = "";

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true });
      dir = "";
    }
  });

  it("writes the actual resumed session id for the shell exit summary", () => {
    dir = mkdtempSync(join(tmpdir(), "hermes-tui-active-"));
    const path = join(dir, "active.json");

    writeActiveSessionFile("actual_session", path);

    expect(JSON.parse(readFileSync(path, "utf8"))).toEqual({
      session_id: "actual_session",
    });
  });
});

describe("live session activation in-flight state", () => {
  beforeEach(() => {
    resetUiState();
    resetTurnState();
    turnController.fullReset();
    patchUiState({ streaming: true });
  });

  it("keeps the in-flight user prompt in history and hydrates partial assistant text", () => {
    const inflight = {
      assistant: "partial answer",
      streaming: true,
      user: "write a long answer",
    };

    expect(liveSessionInflightMessages(inflight)).toEqual([
      { role: "user", text: "write a long answer" },
    ]);

    hydrateLiveSessionInflight(inflight);

    expect(turnController.bufRef).toBe("partial answer");
    expect(getTurnState().streaming).toBe("partial answer");
  });

  it("preserves synthetic turn display metadata while rebuilding live history", () => {
    const inflight = {
      assistant: "",
      display_kind: "process_complete",
      display_metadata: { display_text: "Finished syncing the workspace" },
      streaming: true,
      user: "process completed",
    };

    expect(liveSessionInflightMessages(inflight)).toEqual([
      {
        kind: "event",
        role: "system",
        text: "Finished syncing the workspace",
      },
    ]);
  });

  it("ignores empty in-flight payloads", () => {
    expect(
      liveSessionInflightMessages({
        assistant: "",
        streaming: false,
        user: "   ",
      }),
    ).toEqual([]);

    hydrateLiveSessionInflight({ assistant: "", streaming: false, user: "" });

    expect(turnController.bufRef).toBe("");
    expect(getTurnState().streaming).toBe("");
  });
});

describe("the composer draft across a session reset", () => {
  const paste: ComposerToken = {
    kind: "paste",
    label: "[[ Done.. [412 lines] .. already on it. ]]",
    text: "Done\nline two",
  };

  const image: ComposerToken = {
    index: 1,
    kind: "image",
    label: "[[ Image 1 ]]",
    path: "/tmp/shot.png",
  };

  // the real hook over a composer whose tokens are plain state
  function mountLifecycle(tokens: ComposerToken[]) {
    const held = { tokens };
    let api: null | ReturnType<typeof useSessionLifecycle> = null;

    function Probe() {
      const lifecycle = useSessionLifecycle({
        colsRef: { current: 80 },
        composerActions: {
          setComposerTokens: (next: unknown) => {
            held.tokens =
              typeof next === "function"
                ? (next as (t: ComposerToken[]) => ComposerToken[])(held.tokens)
                : (next as ComposerToken[]);
          },
        } as never,
        gw: { request: vi.fn() } as never,
        panel: vi.fn(),
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
      Object.assign(new PassThrough(), {
        columns: 80,
        isTTY: false,
        rows: 24,
      });

    renderSync(React.createElement(Probe), {
      patchConsole: false,
      stderr: stream() as unknown as NodeJS.WriteStream,
      stdin: stream() as unknown as NodeJS.ReadStream,
      stdout: stream() as unknown as NodeJS.WriteStream,
    });

    return {
      held,
      lifecycle: async () => {
        await vi.waitFor(() => expect(api).toBeTruthy());

        return api!;
      },
    };
  }

  beforeEach(() => {
    resetUiState();
    resetTurnState();
    turnController.fullReset();
  });

  it("resetSession keeps the pastes of the draft that stays, and drops the old session's images", async () => {
    const mounted = mountLifecycle([paste, image]);

    (await mounted.lifecycle()).resetSession();

    expect(mounted.held.tokens).toEqual([paste]);
  });

  it("resetVisibleHistory does too", async () => {
    const mounted = mountLifecycle([paste, image]);

    (await mounted.lifecycle()).resetVisibleHistory();

    expect(mounted.held.tokens).toEqual([paste]);
  });
});
