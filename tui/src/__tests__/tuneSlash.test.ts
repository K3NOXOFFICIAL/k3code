import { beforeEach, describe, expect, it, vi } from "vitest";

import { createSlashHandler } from "../app/createSlashHandler.js";
import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import { findSlashCommand } from "../app/slash/registry.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";

const buildCtx = (
  request: (method: string, params: unknown) => Promise<unknown> = () =>
    Promise.resolve({}),
) => ({
  composer: {
    enqueue: vi.fn(),
    hasSelection: false,
    openEditor: vi.fn(async () => {}),
    paste: vi.fn(),
    queueRef: { current: [] as string[] },
    selection: { copySelection: vi.fn(async () => "") },
    setInput: vi.fn(),
  },
  gateway: {
    gw: {
      getLogTail: vi.fn(() => ""),
      kill: vi.fn(),
      request: vi.fn(request),
    },
    rpc: vi.fn(() => Promise.resolve({})),
  },
  local: {
    catalog: null,
    getHistoryItems: vi.fn(() => []),
    getLastUserMsg: vi.fn(() => ""),
    maybeWarn: vi.fn(),
    setCatalog: vi.fn(),
  },
  session: {
    closeSession: vi.fn(() => Promise.resolve(null)),
    die: vi.fn(),
    dieWithCode: vi.fn(),
    guardBusySessionSwitch: vi.fn(() => false),
    newLiveSession: vi.fn(),
    newSession: vi.fn(),
    resetVisibleHistory: vi.fn(),
    resumeById: vi.fn(),
    setSessionStartedAt: vi.fn(),
  },
  slashFlightRef: { current: 0 },
  transcript: {
    page: vi.fn(),
    panel: vi.fn(),
    resend: vi.fn(),
    send: vi.fn(),
    setHistoryItems: vi.fn(),
    sys: vi.fn(),
    trimLastExchange: vi.fn((items: unknown) => items),
  },
});

const run = (ctx: ReturnType<typeof buildCtx>, line: string) =>
  createSlashHandler(ctx as never)(line);

const LOCKS: [string, Record<string, unknown>][] = [
  [
    "approval",
    { approval: { command: "ls", description: "", requestId: "a" } },
  ],
  ["clarify", { clarify: { choices: null, question: "?", requestId: "c" } }],
  ["confirm", { confirm: { onConfirm: () => {}, title: "sure?" } }],
  ["secret", { secret: { envVar: "T", prompt: "t", requestId: "s" } }],
  ["sudo", { sudo: { requestId: "p" } }],
];

describe("/tune, /model and /effort", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resetOverlayState();
    resetUiState();
    patchUiState({ sid: "sid-abc" });
  });

  it.each(["/tune", "/model", "/effort", "/tune  ", "/model  "])(
    "bare %j opens the one popup, and nothing else",
    (line) => {
      const ctx = buildCtx();

      expect(run(ctx, line)).toBe(true);
      expect(getOverlayState().tunePicker).toBe(true);
      expect(getOverlayState().modelPicker).toBe(false);
      expect(ctx.gateway.gw.request).not.toHaveBeenCalled();
      expect(ctx.gateway.rpc).not.toHaveBeenCalled();
      expect(ctx.transcript.sys).not.toHaveBeenCalled();
    },
  );

  it("opens without a session too (the popup greys effort and ultracode itself)", () => {
    resetUiState();
    const ctx = buildCtx();

    run(ctx, "/tune");
    expect(getOverlayState().tunePicker).toBe(true);
  });

  it("/tune <args> goes to the gateway, which owns the grammar, and prints its answer", async () => {
    const ctx = buildCtx(() =>
      Promise.resolve({ output: "tune: effort high (session)" }),
    );

    run(ctx, "/tune fast high ultracode on --global");

    expect(getOverlayState().tunePicker).toBe(false);
    expect(ctx.gateway.gw.request).toHaveBeenCalledWith("slash.exec", {
      command: "tune fast high ultracode on --global",
      session_id: "sid-abc",
    });
    await vi.waitFor(() =>
      expect(ctx.transcript.sys).toHaveBeenCalledWith(
        "tune: effort high (session)",
      ),
    );
  });

  it("/effort <level> goes to the gateway and prints its answer", async () => {
    const ctx = buildCtx(() => Promise.resolve({ output: "effort: high" }));

    run(ctx, "/effort high");

    expect(getOverlayState().tunePicker).toBe(false);
    expect(ctx.gateway.gw.request).toHaveBeenCalledWith("slash.exec", {
      command: "effort high",
      session_id: "sid-abc",
    });
    await vi.waitFor(() =>
      expect(ctx.transcript.sys).toHaveBeenCalledWith("effort: high"),
    );
  });

  it("shows the gateway's error instead of swallowing it", async () => {
    const ctx = buildCtx(() => Promise.reject(new Error("unknown effort")));

    run(ctx, "/effort sideways");

    await vi.waitFor(() =>
      expect(ctx.transcript.sys).toHaveBeenCalledWith(
        expect.stringContaining("unknown effort"),
      ),
    );
  });

  describe("/model with arguments is unchanged", () => {
    it("/model <key> switches this session over config.set", () => {
      const ctx = buildCtx();

      expect(run(ctx, "/model x-model")).toBe(true);
      expect(getOverlayState().tunePicker).toBe(false);
      expect(ctx.gateway.rpc).toHaveBeenCalledWith("config.set", {
        confirm_expensive_model: false,
        key: "model",
        session_id: "sid-abc",
        value: "x-model",
      });
    });

    it("/model x-model --global stays one --global", () => {
      const ctx = buildCtx();

      run(ctx, "/model x-model --global");
      expect(ctx.gateway.rpc).toHaveBeenCalledWith("config.set", {
        confirm_expensive_model: false,
        key: "model",
        session_id: "sid-abc",
        value: "x-model --global",
      });
    });

    it("/model chain is a gateway slash.exec, not the popup", () => {
      const ctx = buildCtx();

      expect(run(ctx, "/model chain")).toBe(true);
      expect(getOverlayState().tunePicker).toBe(false);
      expect(ctx.gateway.gw.request).toHaveBeenCalledWith(
        "slash.exec",
        expect.objectContaining({ command: "model chain" }),
      );
    });

    it("/model --refresh still opens the old picker with refresh, not the popup", () => {
      const ctx = buildCtx();

      expect(run(ctx, "/model --refresh")).toBe(true);
      expect(getOverlayState().modelPicker).toEqual({ refresh: true });
      expect(getOverlayState().tunePicker).toBe(false);
      expect(ctx.gateway.rpc).not.toHaveBeenCalled();
    });
  });

  describe("/ultracode, /ultraplan and /ultraresearch stay gateway pass-throughs", () => {
    it.each(["ultracode", "ultraplan", "ultraresearch"])(
      "/%s has no local handler",
      (name) => {
        expect(findSlashCommand(name)).toBeUndefined();
      },
    );

    it("tune, effort and model do have one", () => {
      for (const name of ["tune", "effort", "model"]) {
        expect(findSlashCommand(name), name).toBeDefined();
      }
    });

    it.each([
      "/ultracode",
      "/ultracode on",
      "/ultracode off",
      "/ultracode status",
      "/ultraplan add a retry loop",
      "/ultraresearch what changed in 2.0",
    ])("%s goes to slash.exec untouched and never opens the popup", (line) => {
      const ctx = buildCtx(() => Promise.resolve({ output: "ok" }));

      expect(run(ctx, line)).toBe(true);
      expect(getOverlayState().tunePicker).toBe(false);
      expect(ctx.gateway.gw.request).toHaveBeenCalledWith("slash.exec", {
        command: line.slice(1),
        session_id: "sid-abc",
      });
    });

    it("prints what the gateway answers", async () => {
      const ctx = buildCtx(() => Promise.resolve({ output: "ultracode: on" }));

      run(ctx, "/ultracode status");
      await vi.waitFor(() =>
        expect(ctx.transcript.sys).toHaveBeenCalledWith("ultracode: on"),
      );
    });
  });

  describe("while a prompt is open", () => {
    it.each(LOCKS)(
      "bare /tune, /model and /effort refuse to open over a %s prompt",
      (_name, overlay) => {
        patchOverlayState(overlay as never);

        for (const line of ["/tune", "/model", "/effort"]) {
          const ctx = buildCtx();

          expect(run(ctx, line)).toBe(true);
          expect(getOverlayState().tunePicker).toBe(false);
          expect(ctx.transcript.sys).toHaveBeenCalledWith(
            expect.stringContaining("answer the open prompt first"),
          );
        }
      },
    );

    it("a command with arguments still runs: only the popup is locked", () => {
      patchOverlayState({ sudo: { requestId: "p" } } as never);

      const ctx = buildCtx();

      run(ctx, "/effort high");
      expect(ctx.gateway.gw.request).toHaveBeenCalledWith("slash.exec", {
        command: "effort high",
        session_id: "sid-abc",
      });
    });
  });
});
