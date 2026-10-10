import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ComposerToken } from "../app/interfaces.js";
import { submitPrompt, type SubmitPromptDeps } from "../app/submissionCore.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import { useSubmission } from "../app/useSubmission.js";
import {
  codePointSpans,
  expandTokens,
  expandTokensWithSpans,
} from "../domain/attachments.js";
import type { GatewayClient } from "../gatewayClient.js";
import { takeQueueItem } from "../hooks/useQueue.js";

// A wake word in pasted text is not the user asking for a mode (issue #56 B1):
// the TUI says where its pastes are, the gateway skips them.

const LOG = "ERROR ultracode worker crashed\nretrying";
const paste = (label: string, text: string): ComposerToken =>
  ({ kind: "paste", label, text }) as ComposerToken;

function makeGateway(detectDrop: Record<string, unknown> = { matched: false }) {
  const request = vi.fn((method: string, params?: unknown) => {
    void params;

    return Promise.resolve(
      method === "input.detect_drop"
        ? detectDrop
        : method === "session.steer"
          ? { status: "queued" }
          : { status: "ok" },
    );
  });

  const calls = (name: string) =>
    request.mock.calls
      .filter(([method]) => method === name)
      .map(([, params]) => params as Record<string, unknown>);

  return { calls, gw: { request } as unknown as GatewayClient };
}

describe("expandTokensWithSpans", () => {
  it("says where each paste landed, in the trimmed text", () => {
    const tokens = [paste("[[ log ]]", LOG), paste("[[ two ]]", "B")];
    const got = expandTokensWithSpans(tokens)("  fix [[ log ]] and [[ two ]] ");

    expect(got.text).toBe(`fix ${LOG} and B`);
    expect(got.text).toBe(
      expandTokens(tokens)("  fix [[ log ]] and [[ two ]] "),
    );
    expect(got.pasteSpans.map(([s, e]) => got.text.slice(s, e))).toEqual([
      LOG,
      "B",
    ]);
  });

  it("has no spans without pastes, and clamps a paste the trim cuts", () => {
    expect(expandTokensWithSpans([])("plain").pasteSpans).toEqual([]);

    const got = expandTokensWithSpans([paste("[[ p ]]", "  x  ")])("[[ p ]]");

    expect(got.text).toBe("x");
    expect(got.pasteSpans).toEqual([[0, 1]]);
  });
});

describe("codePointSpans", () => {
  it("counts a character outside the BMP once, like the Python side", () => {
    const text = "\u{1F600} a \u{1F600}bc";

    // UTF-16: the emoji is 2 units; code points: 1
    expect(
      codePointSpans(text, [
        [3, 4],
        [5, 8],
      ]),
    ).toEqual([
      [2, 3],
      [4, 6],
    ]);
    expect(codePointSpans("abc", [])).toEqual([]);
  });
});

describe("takeQueueItem", () => {
  it("keeps the pastes where they are when the edit adds text around them", () => {
    const item = {
      display: "[[ log ]]",
      pasteSpans: [[0, LOG.length]] as [number, number][],
      text: LOG,
    };
    const edited = takeQueueItem([item], 0, "please read [[ log ]]")!;

    expect(edited.text).toBe(`please read ${LOG}`);
    expect(edited.pasteSpans).toEqual([[12, 12 + LOG.length]]);
  });

  it("drops them when the edit removes the queued text", () => {
    const item = {
      display: "[[ log ]]",
      pasteSpans: [[0, 3]] as [number, number][],
      text: LOG,
    };

    expect(takeQueueItem([item], 0, "ultracode now")!.pasteSpans ?? []).toEqual(
      [],
    );
  });
});

describe("submitPrompt paste_spans", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  const deps = (gw: GatewayClient, over: Partial<SubmitPromptDeps> = {}) =>
    ({
      appendMessage: vi.fn(),
      enqueue: vi.fn(),
      expand: (t: string) => t,
      gw,
      setLastUserMsg: vi.fn(),
      sys: vi.fn(),
      ...over,
    }) as SubmitPromptDeps;

  it("sends where the pastes are, in code points", async () => {
    const { calls, gw } = makeGateway();
    const text = `\u{1F600} read ${LOG}`;

    submitPrompt(text, deps(gw), true, undefined, {
      pasteSpans: [[8, 8 + LOG.length]],
    });
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[7, 7 + LOG.length]],
      text,
    });
  });

  it("sends none when there are none, or when detect_drop rewrote the text", async () => {
    const plain = makeGateway();

    submitPrompt("ultracode it", deps(plain.gw));
    await vi.waitFor(() =>
      expect(plain.calls("prompt.submit")).toHaveLength(1),
    );
    expect(plain.calls("prompt.submit")[0]).not.toHaveProperty("paste_spans");

    resetUiState();
    patchUiState({ sid: "sess-1" });

    const dropped = makeGateway({
      matched: true,
      text: "[User attached image: a.png] caption",
    });

    submitPrompt("/tmp/a.png caption", deps(dropped.gw), true, undefined, {
      pasteSpans: [[0, 4]],
    });
    await vi.waitFor(() =>
      expect(dropped.calls("prompt.submit")).toHaveLength(1),
    );
    expect(dropped.calls("prompt.submit")[0]).not.toHaveProperty("paste_spans");
  });
});

function mountSubmission(
  gw: GatewayClient,
  tokens: ComposerToken[],
  enqueue = vi.fn(),
) {
  let api: null | ReturnType<typeof useSubmission> = null;
  const tokensRef = { current: tokens };

  function Probe() {
    const submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions: {
        clearIn: () => {
          tokensRef.current = [];
        },
        enqueue,
        pushHistory: vi.fn(),
        setQueueEdit: vi.fn(),
      } as never,
      composerRefs: { queueEditRef: { current: null }, tokensRef } as never,
      composerState: {
        completions: [],
        input: "",
        inputBuf: [],
        queueEditIdx: null,
        queuedDisplay: [],
      } as never,
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

describe("useSubmission paste spans", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("a typed prompt with a paste says where the paste is", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)])();

    sub.dispatchSubmission("see [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[4, 4 + LOG.length]],
      text: `see ${LOG}`,
    });
  });

  it("queued while busy: the expansion and its spans are queued, not the label", async () => {
    const { gw } = makeGateway();
    const enqueue = vi.fn();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)], enqueue)();

    patchUiState({ busy: true, busyInputMode: "queue" });
    sub.dispatchSubmission("see [[ log ]]");

    expect(enqueue).toHaveBeenCalledWith(`see ${LOG}`, "see [[ log ]]", [
      [4, 4 + LOG.length],
    ]);
  });

  it("steered while busy: session.steer carries the spans", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)])();

    patchUiState({ busy: true, busyInputMode: "steer" });
    sub.dispatchSubmission("see [[ log ]]");
    await vi.waitFor(() => expect(calls("session.steer")).toHaveLength(1));

    expect(calls("session.steer")[0]).toMatchObject({
      paste_spans: [[4, 4 + LOG.length]],
      text: `see ${LOG}`,
    });
  });

  it("a queued item sent later keeps its spans", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [])();

    sub.sendQueued(`see ${LOG}`, [[4, 4 + LOG.length]]);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[4, 4 + LOG.length]],
    });
  });
});
