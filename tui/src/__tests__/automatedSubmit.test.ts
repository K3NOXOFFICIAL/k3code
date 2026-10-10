import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createSlashHandler } from "../app/createSlashHandler.js";
import { resetOverlayState } from "../app/overlayStore.js";
import { submitPrompt, type SubmitPromptDeps } from "../app/submissionCore.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import { useSubmission } from "../app/useSubmission.js";
import { applyProposalAccept } from "../app/useMainApp.js";
import type { GatewayClient } from "../gatewayClient.js";

/**
 * `automated` marks text the TUI generated (a /skill expansion, the /go send, an accepted proposal card) so the
 * gateway never looks for wake words or an ultracode mode in it. A typed prompt, and a queued replay of one, carries
 * no `automated` key at all.
 */

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

function makeGateway() {
  const request = vi.fn((method: string, params?: unknown) => {
    void params;

    return Promise.resolve(
      method === "input.detect_drop" ? { matched: false } : { status: "ok" },
    );
  });

  return {
    gw: { request } as unknown as GatewayClient,
    // every prompt.submit payload, in order
    submits: () =>
      request.mock.calls
        .filter(([method]) => method === "prompt.submit")
        .map(([, params]) => params as Record<string, unknown>),
  };
}

function makeDeps(
  gw: GatewayClient,
  over: Partial<SubmitPromptDeps> = {},
): SubmitPromptDeps {
  return {
    appendMessage: vi.fn(),
    enqueue: vi.fn(),
    expand: (t: string) => t,
    gw,
    setLastUserMsg: vi.fn(),
    sys: vi.fn(),
    ...over,
  };
}

describe("submitPrompt automated flag", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("adds automated: true to prompt.submit when asked", async () => {
    const { gw, submits } = makeGateway();

    submitPrompt("expanded skill body", makeDeps(gw), true, undefined, {
      automated: true,
    });
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    expect(submits()[0]).toEqual({
      automated: true,
      session_id: "sess-1",
      text: "expanded skill body",
    });
  });

  it("a typed prompt carries no automated key at all", async () => {
    const { gw, submits } = makeGateway();

    submitPrompt("ultracode fix the bug", makeDeps(gw));
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    expect(submits()[0]).toEqual({
      session_id: "sess-1",
      text: "ultracode fix the bug",
    });
    expect(submits()[0]).not.toHaveProperty("automated");
  });

  it("automated: false is the same as typed", async () => {
    const { gw, submits } = makeGateway();

    submitPrompt("hello", makeDeps(gw), true, undefined, { automated: false });
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    expect(submits()[0]).not.toHaveProperty("automated");
  });
});

/** The real useSubmission, mounted; the test drives its returned callbacks. */
function mountSubmission(gw: GatewayClient) {
  let api: null | ReturnType<typeof useSubmission> = null;

  function Probe() {
    const submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions: { enqueue: vi.fn(), pushHistory: vi.fn() } as never,
      composerRefs: { tokensRef: { current: [] } } as never,
      composerState: { completions: [], input: "", inputBuf: [] } as never,
      gw,
      setLastUserMsg: vi.fn(),
      slashRef: { current: () => false },
      submitRef: { current: () => {} },
      sys: vi.fn(),
    });

    useEffect(() => {
      api = submission;
    });

    return null;
  }

  const stream = () =>
    Object.assign(new PassThrough(), { columns: 80, isTTY: false, rows: 24 });

  renderSync(React.createElement(Probe), {
    patchConsole: false,
    stderr: stream() as unknown as NodeJS.WriteStream,
    stdin: stream() as unknown as NodeJS.ReadStream,
    stdout: stream() as unknown as NodeJS.WriteStream,
  });

  return async () => {
    await vi.waitFor(() => expect(api).toBeTruthy());

    return api!;
  };
}

describe("useSubmission automated provenance", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("sendAutomated sends automated, with the display the transcript shows", async () => {
    const { gw, submits } = makeGateway();
    const sub = await mountSubmission(gw)();

    sub.sendAutomated("SKILL BODY", true, "/my-skill arg");
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    expect(submits()[0]).toMatchObject({ automated: true, text: "SKILL BODY" });
  });

  it("send, and a queued replay of it, stay typed", async () => {
    const { gw, submits } = makeGateway();
    const sub = await mountSubmission(gw)();

    sub.send("ultraplan the rollout");
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.sendQueued("ultraplan the rollout");
    await vi.waitFor(() => expect(submits()).toHaveLength(2));

    for (const payload of submits()) {
      expect(payload).not.toHaveProperty("automated");
    }
  });

  it("submitLiteral is typed unless the caller says automated (the startup -q query stays typed)", async () => {
    const { gw, submits } = makeGateway();
    const sub = await mountSubmission(gw)();

    sub.submitLiteral("run this once");
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.submitLiteral("gateway text", { automated: true });
    await vi.waitFor(() => expect(submits()).toHaveLength(2));

    expect(submits()[0]).not.toHaveProperty("automated");
    expect(submits()[1]).toMatchObject({
      automated: true,
      text: "gateway text",
    });
  });

  it("resend (/retry) goes out the way the last prompt first did", async () => {
    const { gw, submits } = makeGateway();
    const sub = await mountSubmission(gw)();

    sub.sendAutomated("SKILL BODY");
    await vi.waitFor(() => expect(submits()).toHaveLength(1));

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.resend("SKILL BODY");
    await vi.waitFor(() => expect(submits()).toHaveLength(2));
    expect(submits()[1]).toMatchObject({ automated: true, text: "SKILL BODY" });

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.send("a typed prompt");
    await vi.waitFor(() => expect(submits()).toHaveLength(3));

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.resend("a typed prompt");
    await vi.waitFor(() => expect(submits()).toHaveLength(4));
    expect(submits()[3]).toEqual({
      session_id: "sess-1",
      text: "a typed prompt",
    });
  });
});

/** The slash path end to end: the gateway's dispatch reply, through the real hook, to the prompt.submit payload. */
describe("/skill and /go submissions", () => {
  beforeEach(() => {
    resetOverlayState();
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  async function runSlash(line: string, dispatchReply: unknown) {
    const { gw, submits } = makeGateway();
    const sub = await mountSubmission(gw)();
    const sys = vi.fn();

    const request = vi.fn((method: string) =>
      method === "slash.exec"
        ? Promise.resolve(dispatchReply)
        : (gw.request as (m: string) => Promise<unknown>)(method),
    );

    const ctx = {
      composer: { setInput: vi.fn() },
      gateway: { gw: { request }, rpc: vi.fn() },
      local: { catalog: null, getHistoryItems: vi.fn(() => []) },
      session: {},
      slashFlightRef: { current: 0 },
      transcript: {
        page: vi.fn(),
        resend: sub.resend,
        send: sub.sendAutomated,
        sys,
      },
    };

    expect(createSlashHandler(ctx as never)(line)).toBe(true);
    await settle();

    return { submits, sys };
  }

  it("a skill dispatch goes out automated", async () => {
    const { submits } = await runSlash("/review src", {
      display: "/review src",
      message: "You are reviewing: src ...",
      name: "review",
      type: "skill",
    });

    await vi.waitFor(() => expect(submits()).toHaveLength(1));
    expect(submits()[0]).toMatchObject({
      automated: true,
      text: "You are reviewing: src ...",
    });
  });

  it("a send dispatch (the /go prompt) goes out automated", async () => {
    const { submits } = await runSlash("/go", {
      message: "Continue with the plan.",
      type: "send",
    });

    await vi.waitFor(() => expect(submits()).toHaveLength(1));
    expect(submits()[0]).toMatchObject({
      automated: true,
      text: "Continue with the plan.",
    });
  });
});

describe("applyProposalAccept", () => {
  it("submits a send proposal as automated text, once", () => {
    const submitLiteral = vi.fn();
    const sys = vi.fn();

    applyProposalAccept(
      { message: "Run the migration.", type: "send" },
      submitLiteral,
      sys,
    );

    expect(submitLiteral).toHaveBeenCalledTimes(1);
    expect(submitLiteral).toHaveBeenCalledWith("Run the migration.", {
      automated: true,
    });
    expect(sys).not.toHaveBeenCalled();
  });

  it("takes the text field when there is no message", () => {
    const submitLiteral = vi.fn();

    applyProposalAccept(
      { text: "Do it.", type: "send" },
      submitLiteral,
      vi.fn(),
    );
    expect(submitLiteral).toHaveBeenCalledWith("Do it.", { automated: true });
  });

  it("any other kind is only a note, never a prompt", () => {
    const submitLiteral = vi.fn();
    const sys = vi.fn();

    applyProposalAccept(
      { output: "Added a permission rule.", type: "exec" },
      submitLiteral,
      sys,
    );

    expect(submitLiteral).not.toHaveBeenCalled();
    expect(sys).toHaveBeenCalledWith("Added a permission rule.");
  });

  it("says nothing for an empty answer", () => {
    const submitLiteral = vi.fn();
    const sys = vi.fn();

    applyProposalAccept({}, submitLiteral, sys);
    expect(submitLiteral).not.toHaveBeenCalled();
    expect(sys).not.toHaveBeenCalled();
  });
});
