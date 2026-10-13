import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GatewayProvider } from "../app/gatewayContext.js";
import {
  getOverlayState,
  patchOverlayState,
  resetOverlayState,
} from "../app/overlayStore.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import { FloatingOverlays } from "../components/appOverlays.js";
import type { GatewayClient } from "../gatewayClient.js";

import { waitFor } from "./waitFor.js";

class FakeInput extends EventEmitter {
  chunks: string[] = [];
  isRaw = false;
  isTTY = true;
  readableLength = 0;

  read() {
    const next = this.chunks.shift() ?? null;
    this.readableLength = this.chunks.length;

    return next;
  }

  ref = vi.fn();

  send(...chunks: string[]) {
    this.chunks.push(...chunks);
    this.readableLength = this.chunks.length;
    this.emit("readable");
  }

  setEncoding = vi.fn();

  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled;
  });

  unref = vi.fn();
}

const RIGHT = "\u001b[C";
const ENTER = "\r";
const ESC = "\u001b";

const SETTINGS = {
  output: "model: a",
  settings: {
    effort: "default",
    model_key: "a",
    model_keys: ["a", "b"],
    output_style: "default",
    output_styles: ["default"],
    permission_mode: "default",
    permission_modes: ["default", "plan"],
    theme: "default",
  },
  type: "settings",
};

/** The overlay layer as the app mounts it, with the /dashboard popup open on Session and a gateway that answers settings.get. */
function mount() {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 });
  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const request = vi.fn((_method: string, _params: unknown) =>
    Promise.resolve(SETTINGS),
  );
  const onDashboardSave = vi.fn();
  const none = vi.fn();
  const instance = renderSync(
    <GatewayProvider
      value={{
        gw: { request } as unknown as GatewayClient,
        rpc: vi.fn(),
      }}
    >
      <FloatingOverlays
        cols={100}
        compIdx={0}
        completions={[]}
        nativeMode
        onActiveSessionClose={vi.fn()}
        onActiveSessionSelect={none}
        onDashboardSave={onDashboardSave}
        onModelSelect={none}
        onNewLiveSession={none}
        onNewPromptSession={none}
        onResumeSelect={none}
        onTuneApply={none}
        pagerPageSize={10}
      />
    </GatewayProvider>,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  return {
    frame: () => stripAnsi(output),
    onDashboardSave,
    request,
    stdin,
    stop: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}

beforeEach(() => {
  resetUiState();
  resetOverlayState();
});

afterEach(() => {
  resetUiState();
  resetOverlayState();
});

describe("the /dashboard popup in the overlay layer", () => {
  it("opens on the tab it was asked for and asks settings.get about the store's session", async () => {
    patchUiState({ sid: "sid-9" });
    patchOverlayState({ dashboard: "session" });

    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    expect(t.frame()).toContain("[Session]");
    expect(t.request).toHaveBeenCalledWith("settings.get", {
      session_id: "sid-9",
    });
    t.stop();
  });

  it("hands Enter's changes to onDashboardSave, and Esc closes the popup", async () => {
    patchUiState({ sid: "sid-9" });
    patchOverlayState({ dashboard: "session" });

    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send(RIGHT, ENTER);
    await waitFor(() => expect(t.onDashboardSave).toHaveBeenCalledTimes(1));
    expect(t.onDashboardSave.mock.calls[0]![0]).toEqual([
      { command: "/model b", key: "model", value: "b" },
    ]);
    t.stop();

    patchOverlayState({ dashboard: "session" });

    const again = mount();

    await waitFor(() => expect(again.frame()).toContain("Reasoning effort"));
    again.stdin.send(ESC);
    await waitFor(() => expect(getOverlayState().dashboard).toBeNull());
    again.stop();
  });
});
