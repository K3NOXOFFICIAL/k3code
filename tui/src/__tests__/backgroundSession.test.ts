import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  backgroundCwd,
  sendTurnToBackground,
} from "../app/backgroundSession.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import type * as EnvModule from "../config/env.js";

const envState = { workspaceCwd: "" };
vi.mock("../config/env.js", async (importActual) => {
  const actual = await importActual<typeof EnvModule>();

  return {
    ...actual,
    get STARTUP_WORKSPACE_CWD() {
      return envState.workspaceCwd;
    },
  };
});

describe("Ctrl+B background session", () => {
  beforeEach(() => {
    resetUiState();
    envState.workspaceCwd = "";
  });

  it("sends the session's cwd with prompt.background, even with K3CODE_TUI_CWD set", async () => {
    envState.workspaceCwd = "/work/shell-project";
    patchUiState({ info: { cwd: "/work/other-project" } as never });
    const rpc = vi.fn(() => Promise.resolve({ new_session_id: "s2" }));

    await sendTurnToBackground(rpc as never, "sid-abc");

    expect(rpc).toHaveBeenCalledWith("prompt.background", {
      session_id: "sid-abc",
      cwd: "/work/other-project",
    });
  });

  it("falls back to K3CODE_TUI_CWD, and omits cwd when neither is known", async () => {
    const rpc = vi.fn(() => Promise.resolve({}));

    envState.workspaceCwd = "/work/proj";
    expect(backgroundCwd()).toBe("/work/proj");
    await sendTurnToBackground(rpc as never, "sid-abc");
    expect(rpc).toHaveBeenLastCalledWith("prompt.background", {
      session_id: "sid-abc",
      cwd: "/work/proj",
    });

    envState.workspaceCwd = "";
    await sendTurnToBackground(rpc as never, "sid-abc");
    expect(rpc).toHaveBeenLastCalledWith("prompt.background", {
      session_id: "sid-abc",
    });
  });
});
