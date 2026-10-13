import { beforeEach, describe, expect, it, vi } from "vitest";

import { createSlashHandler } from "../app/createSlashHandler.js";
import {
  $isBlocked,
  getOverlayState,
  hasFloatingPanel,
  openDashboard,
  patchOverlayState,
  resetFlowOverlays,
  resetOverlayState,
} from "../app/overlayStore.js";
import { findSlashCommand } from "../app/slash/registry.js";
import { ctrlCOverlayTarget } from "../app/useInputHandlers.js";

const buildCtx = () => ({
  composer: { setInput: vi.fn() },
  gateway: { gw: { request: vi.fn() }, rpc: vi.fn(() => Promise.resolve({})) },
  local: {
    catalog: null,
    getHistoryItems: vi.fn(() => []),
    maybeWarn: vi.fn(),
  },
  session: { guardBusySessionSwitch: vi.fn(() => false) },
  slashFlightRef: { current: 0 },
  transcript: { page: vi.fn(), panel: vi.fn(), sys: vi.fn() },
});

const run = (ctx: ReturnType<typeof buildCtx>, line: string) =>
  createSlashHandler(ctx as never)(line);

describe("/dashboard and /display", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resetOverlayState();
  });

  it("are one command", () => {
    expect(findSlashCommand("display")).toBe(findSlashCommand("dashboard"));
    expect(findSlashCommand("dashboard")).toBeDefined();
  });

  it.each([
    ["/dashboard", "look"],
    ["/display", "look"],
    ["/dashboard details", "details"],
    ["/display Input", "input"],
    ["/dashboard 2", "details"],
    ["/dashboard context", "context"],
    ["/dashboard session", "session"],
    ["/dashboard 6", "status"],
  ])("%j opens the popup on %s and nothing else", (line, tab) => {
    const ctx = buildCtx();

    expect(run(ctx, line)).toBe(true);
    expect(getOverlayState().dashboard).toBe(tab);
    expect(getOverlayState().tunePicker).toBe(false);
    expect(ctx.gateway.gw.request).not.toHaveBeenCalled();
    expect(ctx.transcript.sys).not.toHaveBeenCalled();
  });

  it("names the tabs when it is given a word it does not know", () => {
    const ctx = buildCtx();

    run(ctx, "/dashboard colours");
    expect(getOverlayState().dashboard).toBeNull();
    expect(ctx.transcript.sys).toHaveBeenCalledWith(
      "usage: /dashboard [look|details|input|context|session|status]",
    );
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
  ])("stays shut while a %s prompt waits for an answer", (_name, lock) => {
    patchOverlayState(lock as never);

    const ctx = buildCtx();

    run(ctx, "/dashboard");
    expect(getOverlayState().dashboard).toBeNull();
    expect(openDashboard("input")).toBe(false);
    expect(ctx.transcript.sys).toHaveBeenCalledWith(
      "answer the open prompt first, then run the command again",
    );
  });
});

describe("/settings", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resetOverlayState();
  });

  it.each(["/settings", "/settings model", "/settings Session"])(
    "%j opens the dashboard on its Session tab and asks the gateway for nothing yet",
    (line) => {
      const ctx = buildCtx();

      expect(run(ctx, line)).toBe(true);
      expect(getOverlayState().dashboard).toBe("session");
      expect(getOverlayState().tunePicker).toBe(false);
      expect(ctx.gateway.gw.request).not.toHaveBeenCalled();
      expect(ctx.transcript.page).not.toHaveBeenCalled();
      expect(ctx.transcript.sys).not.toHaveBeenCalled();
    },
  );

  it("tells in its usage that the text is /settings status or text, and in its help that the popup is the dashboard", () => {
    const settings = findSlashCommand("settings")!;

    expect(settings.usage).toBe("/settings [status|text]");
    expect(settings.help).toContain("/dashboard");
  });

  it("has no panel of its own any more", () => {
    run(buildCtx(), "/settings");
    expect(Object.keys(getOverlayState())).not.toContain("settingsPanel");
  });

  it.each(["/settings status", "/settings text"])(
    "%j still prints the text in the pager and opens no popup",
    async (line) => {
      const ctx = buildCtx();

      ctx.gateway.gw.request.mockResolvedValue({
        output: "model: a\neffort: b",
      });

      expect(run(ctx, line)).toBe(true);
      await vi.waitFor(() =>
        expect(ctx.transcript.page).toHaveBeenCalledWith(
          "model: a\neffort: b",
          "Settings",
        ),
      );
      expect(ctx.gateway.gw.request).toHaveBeenCalledWith(
        "slash.exec",
        expect.objectContaining({ command: "settings" }),
      );
      expect(getOverlayState().dashboard).toBeNull();
    },
  );

  it.each([
    [
      "approval",
      { approval: { command: "ls", description: "", requestId: "a" } },
    ],
    ["clarify", { clarify: { choices: null, question: "?", requestId: "c" } }],
    ["confirm", { confirm: { onConfirm: () => {}, title: "sure?" } }],
    ["secret", { secret: { envVar: "T", prompt: "t", requestId: "s" } }],
    ["sudo", { sudo: { requestId: "p" } }],
  ])("stays shut while a %s prompt waits for an answer", (_name, lock) => {
    patchOverlayState(lock as never);

    const ctx = buildCtx();

    run(ctx, "/settings");
    expect(getOverlayState().dashboard).toBeNull();
    expect(ctx.transcript.sys).toHaveBeenCalledWith(
      "finish the pending prompt first",
    );
  });
});

describe("the dashboard overlay", () => {
  beforeEach(resetOverlayState);

  it("blocks the composer and counts as a floating panel", () => {
    expect($isBlocked.get()).toBe(false);
    openDashboard("details");
    expect($isBlocked.get()).toBe(true);
    expect(hasFloatingPanel(getOverlayState())).toBe(true);
  });

  it("survives the end of a turn like the other popups the user opened", () => {
    openDashboard("input");
    resetFlowOverlays();
    expect(getOverlayState().dashboard).toBe("input");
  });

  it("Ctrl+C dismisses it, and answers a prompt first", () => {
    openDashboard();
    expect(ctrlCOverlayTarget(getOverlayState())).toBe("dashboard");
    patchOverlayState({
      approval: { command: "ls", description: "", requestId: "a" },
    });
    expect(ctrlCOverlayTarget(getOverlayState())).toBe("approval");
  });
});
