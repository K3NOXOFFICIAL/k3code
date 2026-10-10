import { PassThrough } from "stream";

import { renderSync } from "@k3code/ink";
import React, { useEffect } from "react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";

import type { ComposerToken } from "../app/interfaces.js";
import { submitPrompt, type SubmitPromptDeps } from "../app/submissionCore.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import { queueItemFromSlash, useSubmission } from "../app/useSubmission.js";
import {
  codePointSpans,
  expandTokens,
  expandTokensWithSpans,
} from "../domain/attachments.js";
import type { GatewayClient } from "../gatewayClient.js";
import { type QueueItem, takeQueueItem } from "../hooks/useQueue.js";

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
          : method === "shell.exec"
            ? { code: 0, stdout: "ok" }
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
  extras: { appendMessage?: Mock; edit?: QueueItem; slash?: Mock } = {},
) {
  let api: null | ReturnType<typeof useSubmission> = null;
  const tokensRef = { current: tokens };

  function Probe() {
    const submission = useSubmission({
      appendMessage: extras.appendMessage ?? vi.fn(),
      composerActions: {
        clearIn: () => {
          tokensRef.current = [];
        },
        enqueue,
        prependQueue: vi.fn(),
        pushHistory: vi.fn(),
        setQueueEdit: vi.fn(),
        takeQueue: (i: number, edited?: string, toks?: ComposerToken[]) =>
          takeQueueItem(extras.edit ? [extras.edit] : [], i, edited, toks),
      } as never,
      composerRefs: {
        queueEditRef: { current: extras.edit ? 0 : null },
        tokensRef,
      } as never,
      composerState: {
        completions: [],
        input: "",
        inputBuf: [],
        queueEditIdx: null,
        queuedDisplay: [],
      } as never,
      gw,
      setLastUserMsg: vi.fn(),
      slashRef: { current: extras.slash ?? (() => false) },
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

// What a paste keeps, wherever the prompt waits or is sent from. Pasted text is content: it neither starts a mode nor
// runs as a slash command, `!cmd` or `{!cmd}`.
describe("paste spans on the queue, retry and edit paths", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("/queue <paste> queues the expansion with its span", async () => {
    const { gw } = makeGateway();
    const enqueue = vi.fn();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)], enqueue)();

    sub.dispatchSubmission("/queue go [[ log ]]");

    expect(enqueue).toHaveBeenCalledWith(`go ${LOG}`, "go [[ log ]]", [
      [3, 3 + LOG.length],
    ]);
  });

  it("queueItemFromSlash keeps image labels and a plain argument as they were", () => {
    expect(queueItemFromSlash("/q fix it", "/q fix it")).toEqual({
      display: "fix it",
      text: "fix it",
    });
    expect(
      queueItemFromSlash("/q [[ Image 1 ]] look", "/q [[ Image 1 ]] look", [
        { index: 1, kind: "image", label: "[[ Image 1 ]]", path: "/tmp/a.png" },
      ]),
    ).toEqual({ display: "[[ Image 1 ]] look", text: "[[ Image 1 ]] look" });
  });

  it("a drained item goes out untrimmed, with spans that still fit its text", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [])();

    sub.sendQueued(` see ${LOG}`, [[5, 5 + LOG.length]]);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[5, 5 + LOG.length]],
      text: ` see ${LOG}`,
    });
  });

  it("a drained item shows its compact display, not the whole paste", async () => {
    const { calls, gw } = makeGateway();
    const appendMessage = vi.fn();
    const sub = await mountSubmission(gw, [], vi.fn(), { appendMessage })();

    sub.sendQueued(`see ${LOG}`, [[4, 4 + LOG.length]], "see [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(appendMessage).toHaveBeenCalledWith({
      role: "user",
      text: "see [[ log ]]",
    });
  });

  it("an interrupting correction shows the display too, and sends the expansion once", async () => {
    const { calls, gw } = makeGateway();
    const appendMessage = vi.fn();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)], vi.fn(), {
      appendMessage,
    })();

    patchUiState({ busy: true, busyInputMode: "interrupt" });
    sub.dispatchSubmission("see [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(appendMessage).toHaveBeenCalledWith({
      role: "user",
      text: "see [[ log ]]",
    });
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[4, 4 + LOG.length]],
      text: `see ${LOG}`,
    });
  });

  it("a pasted !cmd at the start of a drained item is text, not a shell command", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [])();
    const pasted = "!rm -rf build\nthen ultracode";

    sub.sendQueued(pasted, [[0, pasted.length]]);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("shell.exec")).toEqual([]);
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[0, pasted.length]],
      text: pasted,
    });
  });

  it("a drained item runs only the {!cmd} the user typed, and its spans move with the result", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [])();
    const pasted = "ultracode {!touch /tmp/x}";

    sub.sendQueued(`{!date} ${pasted}`, [[8, 8 + pasted.length]]);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("shell.exec")).toEqual([{ command: "date" }]);
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[3, 3 + pasted.length]],
      text: `ok ${pasted}`,
    });
  });

  it("a {!cmd} that shows in a paste label is the paste's, not run", async () => {
    const { calls, gw } = makeGateway();
    const label = "[[ {!touch /tmp/x} more.. [2 lines] ]]";
    const text = "{!touch /tmp/x} more\nlines";
    const sub = await mountSubmission(gw, [paste(label, text)])();

    sub.dispatchSubmission(`see ${label}`);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("shell.exec")).toEqual([]);
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[4, 4 + text.length]],
      text: `see ${text}`,
    });
  });

  it("...while a {!cmd} typed beside that paste still runs", async () => {
    const { calls, gw } = makeGateway();
    const label = "[[ {!touch /tmp/x} more.. [2 lines] ]]";
    const text = "{!touch /tmp/x} more\nlines";
    const sub = await mountSubmission(gw, [paste(label, text)])();

    sub.dispatchSubmission(`{!date} ${label}`);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("shell.exec")).toEqual([{ command: "date" }]);
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[3, 3 + text.length]],
      text: `ok ${text}`,
    });
  });

  it.each([
    ["slash command", "/clear ultracode all of it"],
    ["shell escape", "!ultracode and more"],
  ])(
    "a queued item sent early (Ctrl+K, double Enter) is not routed as a %s",
    async (_kind, pasted) => {
      const { calls, gw } = makeGateway();
      const slash = vi.fn(() => true);
      const sub = await mountSubmission(gw, [], vi.fn(), { slash })();

      sub.dispatchSubmission(pasted, [[0, pasted.length]]);
      await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

      expect(slash).not.toHaveBeenCalled();
      expect(calls("shell.exec")).toEqual([]);
      expect(calls("prompt.submit")[0]).toMatchObject({
        paste_spans: [[0, pasted.length]],
        text: pasted,
      });
    },
  );

  it("/retry sends the same text with the same spans", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)])();

    sub.dispatchSubmission("see [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.resend(`see ${LOG}`);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(2));

    expect(calls("prompt.submit")[1]).toEqual(calls("prompt.submit")[0]);

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.resend("something else");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(3));
    expect(calls("prompt.submit")[2]).not.toHaveProperty("paste_spans");
  });
});

describe("takeQueueItem, edit inside the display", () => {
  const item = {
    display: "[[ log ]]",
    pasteSpans: [[0, LOG.length]] as [number, number][],
    text: LOG,
  };

  it("marks a label the edit left behind as pasted, and sends the text as it reads", () => {
    const edited = takeQueueItem([item], 0, "see [[ log edited ]] now")!;

    expect(edited.text).toBe("see [[ log edited ]] now");
    expect(edited.pasteSpans).toEqual([[4, 4 + "[[ log edited ]]".length]]);
  });

  it("does not read $ in the pasted text as a replacement pattern", () => {
    const text = "cost $& and $' too";
    const edited = takeQueueItem(
      [{ display: "[[ log ]]", pasteSpans: [[0, text.length]], text }],
      0,
      "a [[ log ]] b",
    )!;

    expect(edited.text).toBe(`a ${text} b`);
    expect(edited.pasteSpans).toEqual([[2, 2 + text.length]]);
  });
});

describe("codePointSpans, spans outside the text", () => {
  it("stays inside the text the gateway will check them against", () => {
    expect(codePointSpans("abc", [[1, 99]])).toEqual([[1, 3]]);
    expect(codePointSpans("\u{1F600}b", [[-4, 9]])).toEqual([[0, 2]]);
  });
});

describe("paste spans, review follow-ups", () => {
  beforeEach(() => {
    resetUiState();
    patchUiState({ sid: "sess-1" });
  });

  it("a paste added while editing a queued item is expanded and marked next to the item's own", () => {
    const item: QueueItem = {
      display: "[[ a ]]",
      pasteSpans: [[0, 4]],
      text: "AAAA",
    };
    const edited = takeQueueItem([item], 0, "x [[ a ]] y [[ log ]]", [
      paste("[[ log ]]", LOG),
    ])!;

    expect(edited.text).toBe(`x AAAA y ${LOG}`);
    expect(edited.display).toBe("x [[ a ]] y [[ log ]]");
    expect(edited.pasteSpans).toEqual([
      [2, 6],
      [9, 9 + LOG.length],
    ]);
  });

  it("...also when the item is plain text, and when the edit reached into its label", () => {
    const plain = takeQueueItem(
      [{ display: "fix it", text: "fix it" }],
      0,
      "fix it [[ log ]]",
      [paste("[[ log ]]", LOG)],
    )!;

    expect(plain.text).toBe(`fix it ${LOG}`);
    expect(plain.pasteSpans).toEqual([[7, 7 + LOG.length]]);

    const reached = takeQueueItem(
      [{ display: "[[ a ]]", pasteSpans: [[0, 4]], text: "AAAA" }],
      0,
      "[[ a edited ]] [[ log ]]",
      [paste("[[ log ]]", LOG)],
    )!;

    expect(reached.text).toBe(`[[ a edited ]] ${LOG}`);
    expect(reached.pasteSpans).toContainEqual([15, 15 + LOG.length]);
    expect(reached.pasteSpans).toContainEqual([0, 14]);
  });

  it("submitting an edited queue item sends the new paste's text, marked as pasted", async () => {
    const { calls, gw } = makeGateway();
    const edit: QueueItem = { display: "fix it", text: "fix it" };
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)], vi.fn(), {
      edit,
    })();

    sub.dispatchSubmission("fix it [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[7, 7 + LOG.length]],
      text: `fix it ${LOG}`,
    });
  });

  it("a ! command with a paste in it is a prompt, typed or queued", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)])();

    sub.dispatchSubmission("![[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));
    expect(calls("shell.exec")).toEqual([]);
    expect(calls("prompt.submit")[0]).toMatchObject({
      paste_spans: [[1, 1 + LOG.length]],
      text: `!${LOG}`,
    });

    resetUiState();
    patchUiState({ sid: "sess-1" });
    sub.sendQueued(`!${LOG}`, [[1, 1 + LOG.length]]);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(2));
    expect(calls("shell.exec")).toEqual([]);
  });

  it("a queued item sent early keeps its compact display", async () => {
    const { calls, gw } = makeGateway();
    const appendMessage = vi.fn();
    const sub = await mountSubmission(gw, [], vi.fn(), { appendMessage })();

    sub.dispatchSubmission(
      `see ${LOG}`,
      [[4, 4 + LOG.length]],
      "see [[ log ]]",
    );
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    expect(appendMessage).toHaveBeenCalledWith({
      role: "user",
      text: "see [[ log ]]",
    });
  });

  it("/retry keeps the spans of the last prompt that went out, not of a send that stopped early", async () => {
    const { calls, gw } = makeGateway();
    const sub = await mountSubmission(gw, [paste("[[ log ]]", LOG)])();

    sub.dispatchSubmission("see [[ log ]]");
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(1));

    resetUiState(); // no session: this send stops before anything goes out
    sub.send("something typed too early");
    patchUiState({ sid: "sess-1" });
    sub.resend(`see ${LOG}`);
    await vi.waitFor(() => expect(calls("prompt.submit")).toHaveLength(2));

    expect(calls("prompt.submit")[1]).toEqual(calls("prompt.submit")[0]);
  });
});
