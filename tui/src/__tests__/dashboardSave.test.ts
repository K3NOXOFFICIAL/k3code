import { beforeEach, describe, expect, it, vi } from "vitest";

import { createSlashHandler } from "../app/createSlashHandler.js";
import { saveDashboard } from "../app/dashboardSave.js";
import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import {
  applySnapshot,
  type DashboardChange,
  dashboardKey,
  type DashboardSnapshot,
  initDashboardState,
  setField,
} from "../domain/dashboard.js";

const SNAP: DashboardSnapshot = {
  effort: "default",
  model: "a",
  modelKeys: ["a", "b"],
  outputStyle: "default",
  outputStyles: ["default", "concise"],
  permissionMode: "default",
  permissionModes: ["default", "plan"],
  statusLines: [],
  theme: "auto",
};

/** The slash handler as useMainApp builds it, over a gateway that answers as the test says. */
function app(
  rpc: (method: string, params: Record<string, unknown>) => Promise<unknown>,
) {
  const sys = vi.fn();
  const request = vi.fn((method: string) =>
    Promise.resolve(method === "slash.exec" ? { output: "mode set" } : {}),
  );
  const run = createSlashHandler({
    composer: { setInput: vi.fn() },
    gateway: { gw: { request }, rpc: vi.fn(rpc) },
    local: { catalog: null, getHistoryItems: vi.fn(() => []) },
    session: { guardBusySessionSwitch: vi.fn(() => false) },
    slashFlightRef: { current: 0 },
    transcript: { page: vi.fn(), panel: vi.fn(), send: vi.fn(), sys },
  } as never);

  return { request, run, sys };
}

/** What Enter on a popup with these Session rows changed hands to `onSave`. */
function enter(...picks: [id: string, value: string][]) {
  let state = applySnapshot(initDashboardState(getUiState(), "session"), SNAP);

  for (const [id, value] of picks) {
    state = setField(state, id, value).state;
  }

  const { action } = dashboardKey(state, "", { return: true });

  if (action.type !== "save") {
    throw new Error(`Enter did not save: ${action.type}`);
  }

  return action;
}

beforeEach(() => {
  resetUiState();
  resetOverlayState();
  patchUiState({ sid: "sid-1" });
});

describe("saveDashboard", () => {
  it("paints the draft, closes the popup and sends each config key", () => {
    patchOverlayState({ dashboard: "look" });

    const rpc = vi.fn(() => Promise.resolve({ value: "off" }));
    const sys = vi.fn();
    const run = vi.fn();

    saveDashboard(
      [{ key: "statusbar", value: "off" }],
      { statusBar: "off" },
      { rpc, run, sys },
    );

    expect(getUiState().statusBar).toBe("off");
    expect(getOverlayState().dashboard).toBeNull();
    expect(sys).toHaveBeenCalledWith("display: statusbar=off");
    expect(rpc).toHaveBeenCalledWith("config.set", {
      key: "statusbar",
      value: "off",
    });
    expect(run).not.toHaveBeenCalled();
  });

  it("names a key the gateway would not save", async () => {
    const sys = vi.fn();

    saveDashboard(
      [{ key: "statusbar", value: "off" }],
      {},
      { rpc: vi.fn(() => Promise.resolve(null)), run: vi.fn(), sys },
    );

    await vi.waitFor(() =>
      expect(sys).toHaveBeenCalledWith(
        "could not save statusbar: it applies to this session only",
      ),
    );
  });

  it("runs a change's slash command, the first on its own and the rest alongside it, and says nothing of them itself", () => {
    const sys = vi.fn();
    const run = vi.fn(() => true);
    const changes: DashboardChange[] = [
      { command: "/model b", key: "model", value: "b" },
      { command: "/reasoning high", key: "effort", value: "high" },
      { command: "/theme dark", key: "theme", value: "dark" },
    ];

    saveDashboard(changes, {}, { rpc: vi.fn(), run, sys });

    expect(run.mock.calls).toEqual([
      ["/model b", undefined],
      ["/reasoning high", { alongside: true }],
      ["/theme dark", { alongside: true }],
    ]);
    expect(sys).not.toHaveBeenCalled();
  });

  it("every command of one Enter answers: a failed one is not swallowed by the one after it", async () => {
    const { request, run, sys } = app((_method, params) =>
      params.key === "theme"
        ? Promise.reject(new Error("theme write failed"))
        : Promise.resolve({ value: params.value }),
    );
    const { changes, patch } = enter(
      ["theme", "dark"],
      ["permissions", "plan"],
      ["effort", "high"],
    );

    saveDashboard(changes, patch, { rpc: vi.fn(), run, sys });

    await vi.waitFor(() => {
      expect(sys).toHaveBeenCalledWith(
        expect.stringMatching(/^error: .*theme write failed/),
      );
      expect(sys).toHaveBeenCalledWith("mode set");
      expect(sys).toHaveBeenCalledWith("reasoning: high");
    });
    expect(request).toHaveBeenCalledWith(
      "slash.exec",
      expect.objectContaining({ command: "permissions mode plan" }),
    );
  });

  it("Show thinking and a pinned Thinking section saved together: the pin is what the screen keeps", async () => {
    patchUiState({ sections: {}, showReasoning: false });

    const { run, sys } = app((_method, params) =>
      Promise.resolve({ value: params.value }),
    );
    const cfg = vi.fn((_method: string, _params: Record<string, unknown>) =>
      Promise.resolve({}),
    );
    let state = applySnapshot(
      initDashboardState(getUiState(), "details"),
      SNAP,
    );

    state = setField(state, "reasoning", "on").state;
    state = setField(state, "section.thinking", "collapsed").state;

    const { action } = dashboardKey(state, "", { return: true });

    if (action.type !== "save") {
      throw new Error("Enter did not save");
    }

    expect(action.changes).toEqual([
      { command: "/reasoning show", key: "reasoning", value: "on" },
      { key: "details_mode.thinking", value: "collapsed" },
    ]);

    saveDashboard(action.changes, action.patch, { rpc: cfg, run, sys });

    await vi.waitFor(() => expect(getUiState().showReasoning).toBe(true));
    expect(getUiState().sections.thinking).toBe("collapsed");
    expect(cfg).toHaveBeenCalledWith("config.set", {
      key: "details_mode.thinking",
      value: "collapsed",
    });
  });
});
