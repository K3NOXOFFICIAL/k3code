import { describe, expect, it, vi } from "vitest";

import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import {
  rememberServerRequest,
  resetServerRequestsForTests,
} from "../app/serverRequestStore.js";
import {
  composerHasDraft,
  dismissSensitivePrompt,
  handleIdleHotkeyExit,
  resolveCtrlCComposerAction,
  shouldDetachEditedHistoryInput,
  shouldFallThroughForScroll,
  shouldOpenAgentView,
} from "../app/useInputHandlers.js";

const baseKey = {
  downArrow: false,
  pageDown: false,
  pageUp: false,
  shift: false,
  upArrow: false,
  wheelDown: false,
  wheelUp: false,
};

describe("shouldFallThroughForScroll — keep transcript scrolling alive during prompt overlays", () => {
  it("falls through for wheel scrolls", () => {
    expect(shouldFallThroughForScroll({ ...baseKey, wheelUp: true })).toBe(
      true,
    );
    expect(shouldFallThroughForScroll({ ...baseKey, wheelDown: true })).toBe(
      true,
    );
  });

  it("falls through for PageUp / PageDown", () => {
    expect(shouldFallThroughForScroll({ ...baseKey, pageUp: true })).toBe(true);
    expect(shouldFallThroughForScroll({ ...baseKey, pageDown: true })).toBe(
      true,
    );
  });

  it("falls through for Shift+ArrowUp / Shift+ArrowDown", () => {
    expect(
      shouldFallThroughForScroll({ ...baseKey, shift: true, upArrow: true }),
    ).toBe(true);
    expect(
      shouldFallThroughForScroll({ ...baseKey, shift: true, downArrow: true }),
    ).toBe(true);
  });

  it("does NOT fall through for plain arrows — those drive in-prompt selection", () => {
    expect(shouldFallThroughForScroll({ ...baseKey, upArrow: true })).toBe(
      false,
    );
    expect(shouldFallThroughForScroll({ ...baseKey, downArrow: true })).toBe(
      false,
    );
  });

  it("does NOT fall through for plain Shift — without an arrow it is a no-op", () => {
    expect(shouldFallThroughForScroll({ ...baseKey, shift: true })).toBe(false);
  });

  it("does NOT fall through for unrelated state (no scroll keys held)", () => {
    expect(shouldFallThroughForScroll(baseKey)).toBe(false);
  });
});

describe("composerHasDraft — Ctrl+D exits only from an empty composer (#116443)", () => {
  it("is false for an empty composer and true for text, multi-line buffer or attachments", () => {
    expect(composerHasDraft({ input: "", inputBuf: [], tokens: [] })).toBe(
      false,
    );
    expect(composerHasDraft({ input: "hi", inputBuf: [], tokens: [] })).toBe(
      true,
    );
    expect(
      composerHasDraft({ input: "", inputBuf: ["line 1"], tokens: [] }),
    ).toBe(true);
    expect(
      composerHasDraft({
        input: "",
        inputBuf: [],
        tokens: [{ kind: "image" }],
      }),
    ).toBe(true);
  });
});

describe("shouldDetachEditedHistoryInput", () => {
  const history = ["older message", "line one\nline two"];

  it("detaches a recalled entry as soon as the user edits it", () => {
    expect(
      shouldDetachEditedHistoryInput(1, history, "line one edited\nline two"),
    ).toBe(true);
  });

  it("keeps unchanged recalled entries in history navigation", () => {
    expect(
      shouldDetachEditedHistoryInput(1, history, "line one\nline two"),
    ).toBe(false);
  });

  it("does not detach an ordinary current draft", () => {
    expect(shouldDetachEditedHistoryInput(null, history, "new draft")).toBe(
      false,
    );
  });
});

describe("resolveCtrlCComposerAction — draft wins over interrupt", () => {
  it("clears a non-empty composer even while the agent is streaming", () => {
    expect(
      resolveCtrlCComposerAction({
        busy: true,
        hasDraft: true,
        hasSession: true,
      }),
    ).toBe("clear");
  });

  it("interrupts a running turn when the composer is empty", () => {
    expect(
      resolveCtrlCComposerAction({
        busy: true,
        hasDraft: false,
        hasSession: true,
      }),
    ).toBe("interrupt");
  });

  it("clears an idle composer instead of exiting", () => {
    expect(
      resolveCtrlCComposerAction({
        busy: false,
        hasDraft: true,
        hasSession: true,
      }),
    ).toBe("clear");
  });

  it("exits when idle with an empty composer", () => {
    expect(
      resolveCtrlCComposerAction({
        busy: false,
        hasDraft: false,
        hasSession: true,
      }),
    ).toBe("exit");
  });

  it("does not interrupt a busy session that has no sid yet", () => {
    expect(
      resolveCtrlCComposerAction({
        busy: true,
        hasDraft: false,
        hasSession: false,
      }),
    ).toBe("exit");
  });
});

describe("handleIdleHotkeyExit", () => {
  it("exits in normal terminals", () => {
    const actions = { die: vi.fn(), sys: vi.fn() };

    handleIdleHotkeyExit(actions, false);

    expect(actions.die).toHaveBeenCalledTimes(1);
    expect(actions.sys).not.toHaveBeenCalled();
  });

  it("asks the dashboard for a fresh chat instead of leaving a ghost session", () => {
    const actions = { die: vi.fn(), sys: vi.fn() };
    const requestDashboardNewSession = vi.fn();

    handleIdleHotkeyExit(actions, true, requestDashboardNewSession);

    expect(actions.die).not.toHaveBeenCalled();
    expect(requestDashboardNewSession).toHaveBeenCalledTimes(1);
    expect(actions.sys).toHaveBeenCalled();
  });
});

describe("dismissSensitivePrompt", () => {
  const openRequest = (id: string, method: string) => {
    const respond = vi.fn();

    rememberServerRequest({ fail: vi.fn(), id, method, params: {}, respond });

    return respond;
  };

  it("clears a sudo overlay and answers the server request with an empty value", () => {
    resetOverlayState();
    resetServerRequestsForTests();
    patchOverlayState({ sudo: { requestId: "srq-sudo" } });
    const respond = openRequest("srq-sudo", "sudo");
    const sys = vi.fn();

    dismissSensitivePrompt(getOverlayState(), vi.fn(), sys);

    expect(getOverlayState().sudo).toBeNull();
    expect(sys).toHaveBeenCalled();
    expect(respond).toHaveBeenCalledWith({ value: "" });
  });

  it("clears a secret overlay even when its request already expired (nothing left to answer)", () => {
    resetOverlayState();
    resetServerRequestsForTests();
    patchOverlayState({
      secret: {
        envVar: "API_KEY",
        prompt: "Enter API key",
        requestId: "srq-gone",
      },
    });
    const sys = vi.fn();

    dismissSensitivePrompt(getOverlayState(), vi.fn(), sys);

    expect(getOverlayState().secret).toBeNull();
    expect(sys).toHaveBeenCalled();
  });
});

describe("shouldOpenAgentView — ← opens the agent view only from an idle, empty prompt", () => {
  const idle = {
    blocked: false,
    historyIdx: null,
    input: "",
    inputBuf: [] as string[],
    key: { leftArrow: true },
    queueEditIdx: null,
    stripFocused: false,
  };

  it("opens on a plain ← with an empty prompt", () => {
    expect(shouldOpenAgentView(idle)).toBe(true);
  });

  it("does not open for other keys", () => {
    expect(shouldOpenAgentView({ ...idle, key: { leftArrow: false } })).toBe(
      false,
    );
  });

  it("keeps ← as cursor movement over typed text or a multi-line buffer", () => {
    expect(shouldOpenAgentView({ ...idle, input: "draft" })).toBe(false);
    expect(shouldOpenAgentView({ ...idle, inputBuf: ["line 1"] })).toBe(false);
  });

  it("does not open during a history walk or a queue edit", () => {
    expect(shouldOpenAgentView({ ...idle, historyIdx: 2 })).toBe(false);
    expect(shouldOpenAgentView({ ...idle, queueEditIdx: 0 })).toBe(false);
  });

  it("does not open with the agent strip focused or an overlay up", () => {
    expect(shouldOpenAgentView({ ...idle, stripFocused: true })).toBe(false);
    expect(shouldOpenAgentView({ ...idle, blocked: true })).toBe(false);
  });

  it("does not open with Shift, Alt or Ctrl held", () => {
    for (const mod of ["shift", "meta", "ctrl"] as const) {
      expect(
        shouldOpenAgentView({ ...idle, key: { leftArrow: true, [mod]: true } }),
      ).toBe(false);
    }
  });
});
