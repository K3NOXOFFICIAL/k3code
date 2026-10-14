import { EventEmitter } from "events";

import { renderSync } from "@k3code/ink";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setInputSelection } from "../app/inputSelectionStore.js";
import type { InputHandlerContext } from "../app/interfaces.js";
import {
  $shellRuns,
  resetShellRunsForTests,
  shellRunsToCancel,
  startShellRun,
} from "../app/shellRunStore.js";
import { turnController } from "../app/turnController.js";
import { patchUiState, resetUiState } from "../app/uiStore.js";
import {
  resolveQueueForceSend,
  useInputHandlers,
} from "../app/useInputHandlers.js";
import type { QueueItem } from "../hooks/useQueue.js";

// The real useInputHandlers on a fake terminal: keys go in as the bytes a terminal sends, the composer's setters are
// spies. Each test states the composer the key lands on; nothing re-renders between keys.

class FakeTty extends EventEmitter {
  columns = 80;
  rows = 24;
  isTTY = true;
  isRaw = false;
  private pendingReads: string[] = [];
  ref(): void {}
  unref(): void {}
  read(): string | null {
    return this.pendingReads.shift() ?? null;
  }
  send(chunk: string): void {
    this.pendingReads.push(chunk);
    this.emit("readable");
  }
  setEncoding(): this {
    return this;
  }
  setRawMode(mode: boolean): this {
    this.isRaw = mode;

    return this;
  }
  write(
    _chunk: string | Uint8Array,
    cb?: (err?: Error | null) => void,
  ): boolean {
    cb?.();

    return true;
  }
}

const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

const KEY = {
  ctrlC: "\x03",
  ctrlK: "\x0b",
  down: "\x1b[B",
  esc: "\x1b",
  up: "\x1b[A",
};

const item = (text: string): QueueItem => ({ display: text, text });

interface Composer {
  busy?: boolean;
  busyInputMode?: "interrupt" | "queue" | "steer";
  /** where the cursor is; the end of `input` by default */
  cursor?: number;
  /** the draft historyDraftRef holds (what the composer had before a history walk or a queue edit) */
  draft?: string;
  history?: string[];
  historyIdx?: null | number;
  input?: string;
  queue?: QueueItem[];
  queueEditIdx?: null | number;
}

function setup(over: Composer = {}) {
  const queue = over.queue ?? [];
  const input = over.input ?? "";
  const draftRef = { current: over.draft ?? "" };

  const cActions = {
    clearIn: vi.fn(),
    dequeueItem: vi.fn(() => queue.shift()),
    removeQueue: vi.fn(),
    setHistoryIdx: vi.fn(),
    setInput: vi.fn(),
    setQueueEdit: vi.fn(),
  };

  const actions = {
    appendMessage: vi.fn(),
    die: vi.fn(),
    dispatchSubmission: vi.fn(),
    sys: vi.fn(),
  };

  const gw = {
    publishLocalEvent: vi.fn(),
    request: vi.fn((_method: string, _params?: unknown) => Promise.resolve({})),
  };

  const ctx = {
    actions,
    composer: {
      actions: cActions,
      refs: {
        historyDraftRef: draftRef,
        historyRef: { current: over.history ?? [] },
        queueEditRef: { current: over.queueEditIdx ?? null },
        queueRef: { current: queue },
        tokensRef: { current: [] },
      },
      state: {
        compIdx: 0,
        completions: [],
        historyIdx: over.historyIdx ?? null,
        input,
        inputBuf: [],
        queueEditIdx: over.queueEditIdx ?? null,
        queuedDisplay: queue.map((q) => q.display),
        tokens: [],
      },
    },
    gateway: { gw, rpc: vi.fn(() => Promise.resolve(null)) },
    terminal: {
      hasSelection: false,
      scrollRef: { current: null },
      scrollWithSelection: vi.fn(),
      selection: {},
      stdout: undefined,
    },
    wheelStep: 3,
  };

  const cursor = over.cursor ?? input.length;

  setInputSelection({
    clear: vi.fn(),
    collapseToEnd: vi.fn(),
    copy: vi.fn(),
    cut: vi.fn(),
    end: cursor,
    start: cursor,
    value: input,
  });
  patchUiState({
    busy: over.busy ?? false,
    busyInputMode: over.busyInputMode ?? "queue",
    sid: "sess-1",
  });

  return { actions, cActions, ctx, draftRef, gw };
}

async function press(ctx: unknown, ...keys: string[]) {
  const stdin = new FakeTty();

  function Probe() {
    useInputHandlers(ctx as InputHandlerContext);

    return null;
  }

  const instance = renderSync(React.createElement(Probe), {
    // as the app runs: Ctrl+C reaches the handlers instead of unmounting
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: new FakeTty() as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: new FakeTty() as unknown as NodeJS.WriteStream,
  });

  try {
    await tick();

    for (const key of keys) {
      stdin.send(key);
      // a lone Esc is held back until the parser's escape timeout (50 ms) says no sequence follows
      await (key === KEY.esc ? new Promise((r) => setTimeout(r, 80)) : tick());
    }
  } finally {
    instance.unmount();
    instance.cleanup();
  }
}

describe("useInputHandlers keys", () => {
  beforeEach(() => {
    resetUiState();
    vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  });

  afterEach(() => {
    setInputSelection(null);
    vi.restoreAllMocks();
  });

  describe("Ctrl+K sends the next queued message", () => {
    it("from an empty composer, as a queued item", async () => {
      const t = setup({ queue: [item("first"), item("second")] });

      await press(t.ctx, KEY.ctrlK);

      expect(t.actions.dispatchSubmission).toHaveBeenCalledWith(
        "first",
        [],
        "first",
      );
      expect(t.cActions.setQueueEdit).toHaveBeenCalledWith(null);
    });

    it("but not over a typed draft: there it is the kill-to-end, and the queue stays", async () => {
      const t = setup({ input: "my draft", queue: [item("first")] });

      await press(t.ctx, KEY.ctrlK);

      expect(t.cActions.dequeueItem).not.toHaveBeenCalled();
      expect(t.actions.dispatchSubmission).not.toHaveBeenCalled();
      expect(t.cActions.clearIn).not.toHaveBeenCalled();
    });

    it("mid-turn in 'queue' mode it stops the turn and leaves the queue to drain once", async () => {
      const t = setup({ busy: true, queue: [item("first")] });

      await press(t.ctx, KEY.ctrlK);

      expect(t.gw.request).toHaveBeenCalledWith("session.interrupt", {
        session_id: "sess-1",
      });
      expect(t.cActions.dequeueItem).not.toHaveBeenCalled();
      expect(t.actions.dispatchSubmission).not.toHaveBeenCalled();
    });

    it("mid-turn in 'interrupt' mode it sends the message itself", async () => {
      const t = setup({
        busy: true,
        busyInputMode: "interrupt",
        queue: [item("first")],
      });

      await press(t.ctx, KEY.ctrlK);

      expect(t.actions.dispatchSubmission).toHaveBeenCalledWith(
        "first",
        [],
        "first",
      );
    });

    it("does nothing with an empty queue", async () => {
      const t = setup({ busy: true });

      await press(t.ctx, KEY.ctrlK);

      expect(t.gw.request).not.toHaveBeenCalled();
      expect(t.actions.dispatchSubmission).not.toHaveBeenCalled();
    });
  });

  describe("↑ / ↓ into the queue keep the draft", () => {
    it("↑ saves the draft before the queued message replaces it", async () => {
      const t = setup({ input: "hello", queue: [item("queued")] });

      await press(t.ctx, KEY.up);

      expect(t.draftRef.current).toBe("hello");
      expect(t.cActions.setQueueEdit).toHaveBeenCalledWith(0);
      expect(t.cActions.setInput).toHaveBeenCalledWith("queued");
    });

    it("↑ from a history walk keeps the draft the walk started from", async () => {
      const t = setup({
        draft: "hello",
        historyIdx: 0,
        input: "an old prompt",
        queue: [item("queued")],
      });

      await press(t.ctx, KEY.up);

      expect(t.draftRef.current).toBe("hello");
    });

    it("Esc in a queue edit brings the draft back", async () => {
      const t = setup({
        draft: "hello",
        input: "queued",
        queue: [item("queued")],
        queueEditIdx: 0,
      });

      await press(t.ctx, KEY.esc);

      expect(t.cActions.setQueueEdit).toHaveBeenCalledWith(null);
      expect(t.cActions.setInput).toHaveBeenCalledWith("hello");
      expect(t.cActions.clearIn).not.toHaveBeenCalled();
    });

    it("Esc in a queue edit entered from an empty composer still clears it", async () => {
      const t = setup({
        input: "queued",
        queue: [item("queued")],
        queueEditIdx: 0,
      });

      await press(t.ctx, KEY.esc);

      expect(t.cActions.clearIn).toHaveBeenCalledOnce();
    });

    it("↓ past the first message brings the draft back instead of wrapping", async () => {
      const t = setup({
        draft: "hello",
        input: "one",
        queue: [item("one"), item("two")],
        queueEditIdx: 0,
      });

      await press(t.ctx, KEY.down);

      expect(t.cActions.setInput).toHaveBeenCalledWith("hello");
      expect(t.cActions.setQueueEdit).toHaveBeenCalledWith(null);
      expect(t.cActions.setQueueEdit).not.toHaveBeenCalledWith(1);
    });

    it("↑ past the last message brings the draft back too, and moves on inside the queue", async () => {
      const t = setup({
        draft: "hello",
        input: "two",
        queue: [item("one"), item("two")],
        queueEditIdx: 1,
      });

      await press(t.ctx, KEY.up);

      expect(t.cActions.setInput).toHaveBeenCalledWith("hello");

      const inside = setup({
        draft: "hello",
        input: "one",
        queue: [item("one"), item("two")],
        queueEditIdx: 0,
      });

      await press(inside.ctx, KEY.up);

      expect(inside.cActions.setQueueEdit).toHaveBeenCalledWith(1);
      expect(inside.cActions.setInput).toHaveBeenCalledWith("two");
    });
  });

  describe("↑ / ↓ leave a multi-line draft only from its first / last line", () => {
    const entry = "a\nb\nc";

    it("↓ in a recalled history entry moves on only when no line is below the cursor", async () => {
      const first = setup({
        cursor: 0,
        history: [entry, "newer"],
        historyIdx: 0,
        input: entry,
      });

      await press(first.ctx, KEY.down);
      expect(first.cActions.setInput).not.toHaveBeenCalled();

      const last = setup({
        history: [entry, "newer"],
        historyIdx: 0,
        input: entry,
      });

      await press(last.ctx, KEY.down);
      expect(last.cActions.setInput).toHaveBeenCalledWith("newer");
    });

    it("↑ at position 0 of a draft that starts with a newline reaches history", async () => {
      const t = setup({
        cursor: 0,
        history: ["older"],
        input: "\nfoo",
      });

      await press(t.ctx, KEY.up);

      expect(t.cActions.setInput).toHaveBeenCalledWith("older");
    });

    it("↑ below that newline stays in the draft", async () => {
      const t = setup({ cursor: 2, history: ["older"], input: "\nfoo" });

      await press(t.ctx, KEY.up);

      expect(t.cActions.setInput).not.toHaveBeenCalled();
    });
  });
});

describe("a cancel press with a running !cmd", () => {
  beforeEach(() => {
    resetUiState();
    resetShellRunsForTests();
    vi.spyOn(process.stdout, "write").mockImplementation(() => true);
  });

  afterEach(() => {
    setInputSelection(null);
    resetShellRunsForTests();
    vi.restoreAllMocks();
  });

  const calls = (
    gw: { request: { mock: { calls: unknown[][] } } },
    m: string,
  ) => gw.request.mock.calls.filter(([method]) => method === m);

  const watchInterrupt = () => vi.spyOn(turnController, "interruptTurn");

  describe("Ctrl+C", () => {
    it("mid-turn cancels the !cmd once and leaves the turn running", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toEqual([
        ["shell.cancel", { session_id: "sess-1" }],
      ]);
      expect(interrupt).not.toHaveBeenCalled();
      expect(t.actions.sys).toHaveBeenCalledWith(
        "cancelling the running ! command …",
      );
      expect(shellRunsToCancel()).toEqual([]);
    });

    it("a second press, with every run already cancelling, interrupts the turn as before", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlC, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(interrupt).toHaveBeenCalledTimes(1);
    });

    it("with no !cmd running interrupts the turn as before", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toEqual([]);
      expect(interrupt).toHaveBeenCalledTimes(1);
    });

    it("idle on an empty composer cancels a !cmd that outlived its turn instead of quitting", async () => {
      const t = setup();

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(t.actions.die).not.toHaveBeenCalled();
    });

    it("idle on an empty composer with no !cmd running quits as before", async () => {
      const t = setup();

      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toEqual([]);
      expect(t.actions.die).toHaveBeenCalledTimes(1);
    });

    it("over a draft still clears the draft first", async () => {
      const t = setup({ busy: true, input: "draft" });

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlC);

      expect(t.cActions.clearIn).toHaveBeenCalledTimes(1);
      expect(calls(t.gw, "shell.cancel")).toEqual([]);
    });

    it("one press cancels two concurrent runs", async () => {
      const t = setup({ busy: true });

      startShellRun("x1");
      startShellRun("x2");
      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect($shellRuns.get()).toMatchObject({
        x1: { cancelling: true },
        x2: { cancelling: true },
      });
    });

    it("a failed shell.cancel shows the error and unflags the runs, so a later press retries", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      t.gw.request.mockImplementation((method: string) =>
        method === "shell.cancel"
          ? Promise.reject(new Error("socket closed"))
          : Promise.resolve({}),
      );
      startShellRun("x1");
      await press(t.ctx, KEY.ctrlC);

      expect(t.actions.sys).toHaveBeenCalledWith(
        expect.stringMatching(/^error: /),
      );
      expect(shellRunsToCancel()).toEqual(["x1"]);

      await press(t.ctx, KEY.ctrlC);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(2);
      expect(interrupt).not.toHaveBeenCalled();
    });
  });

  describe("Esc Esc", () => {
    it("mid-turn cancels the !cmd and leaves the turn running", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.esc, KEY.esc);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(interrupt).not.toHaveBeenCalled();
    });

    it("a second pair, with the run already cancelling, interrupts the turn", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.esc, KEY.esc, KEY.esc, KEY.esc);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(interrupt).toHaveBeenCalledTimes(1);
    });

    it("with no !cmd running interrupts the turn as before", async () => {
      const t = setup({ busy: true });
      const interrupt = watchInterrupt();

      await press(t.ctx, KEY.esc, KEY.esc);

      expect(calls(t.gw, "shell.cancel")).toEqual([]);
      expect(interrupt).toHaveBeenCalledTimes(1);
    });
  });

  describe("Ctrl+K mid-turn in 'queue' mode", () => {
    it("cancels the !cmd instead of stopping the turn", async () => {
      const t = setup({ busy: true, queue: [item("first")] });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlK);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(interrupt).not.toHaveBeenCalled();
    });

    it("a second press, with the run already cancelling, stops the turn", async () => {
      const t = setup({ busy: true, queue: [item("first")] });
      const interrupt = watchInterrupt();

      startShellRun("x1");
      await press(t.ctx, KEY.ctrlK, KEY.ctrlK);

      expect(calls(t.gw, "shell.cancel")).toHaveLength(1);
      expect(interrupt).toHaveBeenCalledTimes(1);
    });
  });
});

describe("resolveQueueForceSend", () => {
  const base = {
    busy: false,
    busyInputMode: "queue" as const,
    hasDraft: false,
    hasQueue: true,
    hasSession: true,
  };

  it("sends when idle, interrupts a turn in 'queue' mode and leaves the other modes to the message", () => {
    expect(resolveQueueForceSend(base)).toBe("send");
    expect(resolveQueueForceSend({ ...base, busy: true })).toBe("interrupt");
    expect(
      resolveQueueForceSend({ ...base, busy: true, busyInputMode: "steer" }),
    ).toBe("send");
  });

  it("does nothing over a draft, without a queue or without a session", () => {
    expect(resolveQueueForceSend({ ...base, hasDraft: true })).toBeNull();
    expect(resolveQueueForceSend({ ...base, hasQueue: false })).toBeNull();
    expect(resolveQueueForceSend({ ...base, hasSession: false })).toBeNull();
  });
});
