import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { patchOverlayState, resetOverlayState } from "../app/overlayStore.js";
import { getUiState, patchUiState, resetUiState } from "../app/uiStore.js";
import { Dashboard } from "../components/dashboard.js";
import type * as DashboardModule from "../domain/dashboard.js";
import {
  applySnapshot,
  type DashboardChange,
  type DashboardTabId,
  type DashboardUi,
  failSnapshot,
} from "../domain/dashboard.js";
import type { GatewayClient } from "../gatewayClient.js";
import { DEFAULT_THEME } from "../theme.js";

import { waitFor } from "./waitFor.js";

// The real functions, watched: a late answer must never reach them.
vi.mock("../domain/dashboard.js", async (importActual) => {
  const actual = await importActual<typeof DashboardModule>();

  return {
    ...actual,
    applySnapshot: vi.fn(actual.applySnapshot),
    failSnapshot: vi.fn(actual.failSnapshot),
  };
});

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

const settle = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));

const DOWN = "\u001b[B";
const RIGHT = "\u001b[C";
const LEFT = "\u001b[D";
const PGDN = "\u001b[6~";
const WHEEL_UP = "\u001b[<64;10;5M";
const WHEEL_DOWN = "\u001b[<65;10;5M";
const ENTER = "\r";
const ESC = "\u001b";
const TAB = "\t";

/** The `settings.get` result: model `a` of two, plan mode available, two output styles, a status text. */
const settings = (over: Record<string, unknown> = {}, lines = 3) => ({
  output: Array.from({ length: lines }, (_, i) => `status line ${i + 1}`).join(
    "\n",
  ),
  settings: {
    effort: "default",
    model_key: "a",
    model_keys: ["a", "b"],
    output_style: "default",
    output_styles: ["default", "concise"],
    permission_mode: "default",
    permission_modes: ["default", "plan"],
    theme: "default",
    ...over,
  },
  type: "settings",
});

/** A request the test answers (or fails) when it likes. */
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<unknown>((res, rej) => {
    resolve = res;
    reject = rej;
  });

  return { promise, reject, resolve };
}

function mount(
  tab: DashboardTabId = "look",
  opts: {
    reply?: (params: Record<string, unknown>) => Promise<unknown>;
    sessionId?: null | string;
  } = {},
) {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 });
  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const request = vi.fn((_method: string, params: Record<string, unknown>) =>
    (opts.reply ?? (() => Promise.resolve(settings())))(params),
  );
  const onSave =
    vi.fn<(changes: DashboardChange[], patch: Partial<DashboardUi>) => void>();
  const onCancel = vi.fn();
  const gw = { request } as unknown as GatewayClient;

  const view = (sessionId: null | string) => (
    <Dashboard
      gw={gw}
      initialTab={tab}
      onCancel={onCancel}
      onSave={onSave}
      sessionId={sessionId}
      t={DEFAULT_THEME}
    />
  );

  const instance = renderSync(
    view(opts.sessionId === undefined ? "sid-1" : opts.sessionId),
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  return {
    frame: () => stripAnsi(output),
    onCancel,
    onSave,
    request,
    rerender: (sessionId: null | string) => instance.rerender(view(sessionId)),
    stdin,
    stop: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}

beforeEach(() => {
  vi.mocked(applySnapshot).mockClear();
  vi.mocked(failSnapshot).mockClear();
  resetUiState();
  resetOverlayState();
});

afterEach(() => {
  resetUiState();
  resetOverlayState();
});

describe("Dashboard popup", () => {
  it("paints the tabs, the rows and the picked chips from the ui it found", async () => {
    patchUiState({ indicatorStyle: "emoji", statusBar: "bottom" });

    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Status bar"));
    expect(t.frame()).toContain("[Look]");
    expect(t.frame()).toContain("[bottom]");
    expect(t.frame()).toContain("[emoji]");
    t.stop();
  });

  it("paints a change live and Esc puts the ui back", async () => {
    const t = mount();

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    t.stdin.send(ESC);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(getUiState().statusBar).toBe("top");
    expect(t.onSave).not.toHaveBeenCalled();
    t.stop();
  });

  it("closing without saving (Ctrl+C unmounts it) restores the ui too", async () => {
    const t = mount();

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    t.stop();

    expect(getUiState().statusBar).toBe("top");
  });

  it("Enter hands the changes and the whole draft to onSave and leaves the ui as painted", async () => {
    const t = mount("input");

    await settle();
    t.stdin.send(`${RIGHT}${DOWN}${RIGHT}${DOWN}${RIGHT}${ENTER}`);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "mouse", value: "buttons" },
      { key: "busy", value: "steer" },
      { key: "focus", value: "on" },
    ]);
    expect(t.onSave.mock.calls[0]![1]).toMatchObject({
      busyInputMode: "steer",
      focusView: true,
      mouseTracking: "buttons",
    });
    t.stop();
    // saved: the unmount must not undo what the user saved
    expect(getUiState().mouseTracking).toBe("buttons");
    expect(getUiState().busyInputMode).toBe("steer");
  });

  it("the Context tab hands its change over as the /autocompact command", async () => {
    const t = mount("context");

    await waitFor(() => expect(t.frame()).toContain("Autocompact"));
    t.stdin.send(`${RIGHT}${RIGHT}${ENTER}`);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { command: "/autocompact 60%", key: "autocompact", value: "60%" },
    ]);
    t.stop();
  });

  it("a field that waits for Enter does not touch the ui before it", async () => {
    const t = mount("input");

    await settle();
    t.stdin.send(`${DOWN}${DOWN}${RIGHT}`);
    await settle();
    expect(getUiState().focusView).toBe(false);
    t.stop();
  });

  it("Tab changes tab and the details rows show", async () => {
    const t = mount();

    await settle();
    t.stdin.send(TAB);
    await waitFor(() => expect(t.frame()).toContain("Tool calls"));
    expect(t.frame()).toContain("[Details]");
    t.stop();
  });

  it("ignores keys while a question is open", async () => {
    patchOverlayState({
      approval: { command: "ls", description: "", requestId: "a" },
    });

    const t = mount();

    await settle();
    t.stdin.send(`${RIGHT}${ENTER}${ESC}`);
    await settle(120);

    expect(getUiState().statusBar).toBe("top");
    expect(t.onSave).not.toHaveBeenCalled();
    expect(t.onCancel).not.toHaveBeenCalled();
    expect(t.frame()).toContain("Answer the question above first");
    t.stop();
  });

  it("two keys that land before a re-render are both counted", async () => {
    const t = mount();

    await settle();
    t.stdin.send(`${DOWN}${RIGHT}${LEFT}${RIGHT}`);
    await settle(120);
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "indicator", value: "unicode" },
    ]);
    t.stop();
  });

  it("shows the Session rows once settings.get has answered, for the session it was opened on", async () => {
    const answer = deferred();
    const t = mount("session", { reply: () => answer.promise });

    await waitFor(() => expect(t.frame()).toContain("loading settings…"));
    expect(t.request).toHaveBeenCalledTimes(1);
    expect(t.request).toHaveBeenCalledWith("settings.get", {
      session_id: "sid-1",
    });
    expect(t.frame()).not.toContain("Reasoning effort");

    answer.resolve(
      settings({
        model_key: "b",
        permission_mode: "plan",
        theme: "dark",
      }),
    );
    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));

    expect(t.frame()).toContain("[b]");
    expect(t.frame()).toContain("[plan]");
    expect(t.frame()).toContain("[dark]");
    t.stop();
  });

  it("asks without a session id before the TUI has one", async () => {
    const t = mount("session", { sessionId: null });

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    expect(t.request).toHaveBeenCalledWith("settings.get", {});
    t.stop();
  });

  it("a failing request says why on the Session tab and on the Status tab", async () => {
    const t = mount("session", {
      reply: () => Promise.reject(new Error("gateway is down")),
    });

    await waitFor(() =>
      expect(t.frame()).toContain("settings unavailable: gateway is down"),
    );
    expect(t.frame()).not.toContain("Reasoning effort");
    expect(failSnapshot).toHaveBeenCalledTimes(1);
    expect(applySnapshot).not.toHaveBeenCalled();

    t.stdin.send(TAB);
    await waitFor(() => expect(t.frame()).toContain("[Status]"));
    expect(t.frame().lastIndexOf("settings unavailable")).toBeGreaterThan(
      t.frame().lastIndexOf("[Status]"),
    );
    t.stop();
  });

  it("the Look rows work while the answer is on its way, and what was changed meanwhile survives it", async () => {
    const answer = deferred();
    const t = mount("look", { reply: () => answer.promise });

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    answer.resolve(settings());
    await waitFor(() => expect(applySnapshot).toHaveBeenCalledTimes(1));
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "statusbar", value: "bottom" },
    ]);
    t.stop();
  });

  it("a request that fails does not undo what was changed meanwhile either", async () => {
    const answer = deferred();
    const t = mount("look", { reply: () => answer.promise });

    await settle();
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    answer.reject(new Error("gateway is down"));
    await waitFor(() => expect(failSnapshot).toHaveBeenCalledTimes(1));
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { key: "statusbar", value: "bottom" },
    ]);
    t.stop();
  });

  it("Enter hands each changed Session row to onSave as its slash command", async () => {
    const t = mount("session");

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send(`${RIGHT}${DOWN}${RIGHT}${RIGHT}${RIGHT}${ENTER}`);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));

    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { command: "/model b", key: "model", value: "b" },
      { command: "/reasoning high", key: "effort", value: "high" },
    ]);
    // the commands apply these: the ui is not painted with them
    expect(Object.keys(t.onSave.mock.calls[0]![1])).not.toContain("model");
    t.stop();
  });

  it("Esc out of a Session popup changes nothing", async () => {
    const t = mount("session");

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send(`${RIGHT}${ESC}`);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(t.onSave).not.toHaveBeenCalled();
    t.stop();
  });

  it("an answer that comes after the popup closed is ignored", async () => {
    const answer = deferred();
    const t = mount("session", { reply: () => answer.promise });

    await settle();
    t.stop();
    answer.resolve(settings());
    await settle();

    expect(applySnapshot).not.toHaveBeenCalled();
  });

  it("a failure that comes after the popup closed is ignored", async () => {
    const answer = deferred();
    const t = mount("session", { reply: () => answer.promise });

    await settle();
    t.stop();
    answer.reject(new Error("too late"));
    await settle();

    expect(failSnapshot).not.toHaveBeenCalled();
  });

  it("an answer for a session the popup has left is ignored, the new session's is shown", async () => {
    const first = deferred();
    const second = deferred();
    const t = mount("session", {
      reply: (params) =>
        params.session_id === "sid-1" ? first.promise : second.promise,
    });

    await settle();
    t.rerender("sid-2");
    await waitFor(() => expect(t.request).toHaveBeenCalledTimes(2));
    expect(t.request).toHaveBeenLastCalledWith("settings.get", {
      session_id: "sid-2",
    });

    first.resolve(settings({ model_key: "a" }));
    await settle();
    expect(applySnapshot).not.toHaveBeenCalled();

    second.resolve(settings({ model_keys: ["x", "y"], model_key: "y" }));
    await waitFor(() => expect(t.frame()).toContain("[y]"));
    expect(applySnapshot).toHaveBeenCalledTimes(1);
    t.stop();
  });

  it("a second answer that fails hides the Session rows, and Enter no longer sends their edits", async () => {
    let asked = 0;
    const t = mount("session", {
      reply: () =>
        ++asked === 1
          ? Promise.resolve(settings())
          : Promise.reject(new Error("gateway restarting")),
    });

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send(RIGHT);
    await settle();
    t.rerender("sid-2");
    await waitFor(() =>
      expect(t.frame()).toContain("settings unavailable: gateway restarting"),
    );
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));

    expect(t.onSave).not.toHaveBeenCalled();
    t.stop();
  });

  it("Alt/Option and Ctrl with an arrow, and Alt+Enter, change nothing; the plain keys still do", async () => {
    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Status bar"));
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    // Option+→ / Option+← (ESC f / ESC b on macOS terminals, or CSI 1;3), Ctrl+→, Alt+Enter
    t.stdin.send("\u001b[1;3C", "\u001b[1;3D", "\u001b[1;5C", "\u001b[1;5D");
    t.stdin.send("\u001bf", "\u001bb", "\u001b\r");
    await settle();
    expect(getUiState().statusBar).toBe("bottom");
    expect(t.onSave).not.toHaveBeenCalled();
    expect(t.onCancel).not.toHaveBeenCalled();
    // Shift+Tab is no chord: it goes back a tab (Look to Status)
    t.stdin.send("\u001b[Z");
    await waitFor(() => expect(t.frame()).toContain("[Status]"));
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));
    t.stop();
  });

  it("keys and clicks after Enter are ignored: Esc before the unmount does not undo the save", async () => {
    const t = mount();

    await waitFor(() => expect(t.frame()).toContain("Status bar"));
    t.stdin.send(RIGHT);
    await waitFor(() => expect(getUiState().statusBar).toBe("bottom"));
    t.stdin.send(ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));
    t.stdin.send(ESC, ENTER, RIGHT);
    await settle();

    expect(t.onCancel).not.toHaveBeenCalled();
    expect(t.onSave).toHaveBeenCalledTimes(1);
    expect(getUiState().statusBar).toBe("bottom");
    t.stop();
  });

  it("the Status tab shows the text and PgDn pages by the rows the popup paints", async () => {
    const t = mount("status", {
      reply: () => Promise.resolve(settings({}, 40)),
    });

    await waitFor(() => expect(t.frame()).toMatch(/lines 1-\d+ of 40/));

    // whatever height the terminal gives the window, a page is that many lines
    const page = Number(/lines 1-(\d+) of 40/.exec(t.frame())![1]);

    t.stdin.send(PGDN);
    await waitFor(() =>
      expect(t.frame()).toContain(`lines ${page + 1}-${2 * page} of 40`),
    );
    t.stop();
  });

  it("the wheel scrolls the Status text and stops at its end", async () => {
    const t = mount("status", {
      reply: () => Promise.resolve(settings({}, 40)),
    });

    await waitFor(() => expect(t.frame()).toMatch(/lines 1-\d+ of 40/));

    const page = Number(/lines 1-(\d+) of 40/.exec(t.frame())![1]);
    const bottom = `lines ${41 - page}-40 of 40`;

    t.stdin.send(...Array<string>(60).fill(WHEEL_DOWN));
    await waitFor(() => expect(t.frame()).toContain(bottom));

    // the end of the text is where it stops: one notch back moves it one line
    t.stdin.send(WHEEL_UP);
    await waitFor(() =>
      expect(
        t.frame().lastIndexOf(`lines ${40 - page}-39 of 40`),
      ).toBeGreaterThan(t.frame().lastIndexOf(bottom)),
    );
    t.stop();
  });

  it("/ searches every tab, and Esc clears the search before it closes", async () => {
    const t = mount("session");

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send("/");
    t.stdin.send("m");
    t.stdin.send("o");
    t.stdin.send("u");
    await waitFor(() => expect(t.frame()).toContain("search: mou▌"));
    expect(t.frame()).toContain("Mouse");
    t.stdin.send(ESC);
    await settle();
    expect(t.onCancel).not.toHaveBeenCalled();
    t.stdin.send(ESC);
    await waitFor(() => expect(t.onCancel).toHaveBeenCalledTimes(1));
    t.stop();
  });

  it("Backspace and Delete edit the search text, and an Alt chord in the box does nothing", async () => {
    const t = mount("look");

    await waitFor(() => expect(t.frame()).toContain("Status bar"));
    t.stdin.send("/", "x", "z", "\u007f", "y");
    await waitFor(() => expect(t.frame()).toContain("search: xy▌"));
    t.stdin.send("\u001b[1;3D", "\u001b[3~", "w");
    await waitFor(() => expect(t.frame()).toContain("search: xw▌"));
    // ^H is what some terminals send for Backspace
    t.stdin.send("\b", "v");
    await waitFor(() => expect(t.frame()).toContain("search: xv▌"));
    t.stop();
  });

  it("Esc out of the box leaves the popup where it was, on the row it was on", async () => {
    const t = mount("session");

    await waitFor(() => expect(t.frame()).toContain("Reasoning effort"));
    t.stdin.send(DOWN);
    t.stdin.send("/");
    t.stdin.send("m");
    t.stdin.send("o");
    t.stdin.send("u");
    await waitFor(() => expect(t.frame()).toContain("search: mou▌"));
    t.stdin.send(ESC);
    await settle();
    // → changes the row under the cursor: Reasoning effort on the Session tab, not Mouse on the Input tab
    t.stdin.send(RIGHT, ENTER);
    await waitFor(() => expect(t.onSave).toHaveBeenCalledTimes(1));
    expect(t.onSave.mock.calls[0]![0]).toEqual([
      { command: "/reasoning low", key: "effort", value: "low" },
    ]);
    t.stop();
  });
});
