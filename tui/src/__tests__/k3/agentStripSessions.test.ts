import { beforeEach, describe, expect, it, vi } from "vitest";

import { createGatewayEventHandler } from "../../app/createGatewayEventHandler.js";
import { patchUiState, resetUiState } from "../../app/uiStore.js";
import type { SessionActiveItem } from "../../gatewayTypes.js";
import {
  $stripSessions,
  applyStripSessions,
} from "../../k3/agentStripStore.js";

const row = (over: Partial<SessionActiveItem> = {}): SessionActiveItem => ({
  id: "bg-1",
  status: "working",
  title: "fix the login test",
  turn_started_at: 100,
  ...over,
});

// Only what the handler touches at construction; the `session.active_list` case uses none of it.
const ctx = () =>
  ({
    composer: { dequeue: () => undefined, queueEditRef: { current: null } },
    gateway: { gw: { request: vi.fn() }, rpc: vi.fn(async () => null) },
    session: {
      STARTUP_RESUME_ID: "",
      STARTUP_VIEW: "",
      colsRef: { current: 80 },
    },
    submission: { submitRef: { current: vi.fn() } },
    system: { bellOnComplete: false, sys: vi.fn() },
    transcript: {
      appendMessage: vi.fn(),
      panel: vi.fn(),
      setHistoryItems: vi.fn(),
    },
  }) as any;

describe("strip sessions", () => {
  beforeEach(() => {
    resetUiState();
    $stripSessions.set([]);
  });

  it("an unchanged snapshot sets nothing, so the idle TUI does not re-render", () => {
    applyStripSessions([row()]);
    const sets = vi.fn();
    const off = $stripSessions.listen(sets);

    applyStripSessions([row()]);
    // the pushed event marks no row current, the poll marks the TUI's own: the same state either way
    applyStripSessions([row({ current: true })]);
    applyStripSessions([row({ current: false })]);
    expect(sets).not.toHaveBeenCalled();

    applyStripSessions([row({ status: "needs_input" })]);
    expect(sets).toHaveBeenCalledTimes(1);
    off();
  });

  it("a pushed change keeps the poll's current mark, so the next (deduped) poll does not leave the store without it", () => {
    applyStripSessions([row({ current: true, id: "mine" }), row()]);
    // pushed: no row is marked current, and the background row changed
    applyStripSessions([row({ id: "mine" }), row({ status: "needs_input" })]);
    // the poll's copy of the same state is deduped, so whatever the push stored stays
    applyStripSessions([
      row({ current: true, id: "mine" }),
      row({ status: "needs_input" }),
    ]);

    expect($stripSessions.get()).toEqual([
      row({ current: true, id: "mine" }),
      row({ status: "needs_input" }),
    ]);
  });

  it("the current mark moving to another session is a change, not a duplicate", () => {
    // the TUI switched from session A to B: the rows are the same but for who is current
    applyStripSessions([
      row({ current: true, id: "a" }),
      row({ current: false, id: "b" }),
    ]);
    const sets = vi.fn();
    const off = $stripSessions.listen(sets);

    applyStripSessions([
      row({ current: false, id: "a" }),
      row({ current: true, id: "b" }),
    ]);
    expect(sets).toHaveBeenCalledTimes(1);
    expect($stripSessions.get().map((s) => [s.id, s.current])).toEqual([
      ["a", false],
      ["b", true],
    ]);
    off();
  });

  it("a pushed event marks the TUI's own session current when nothing was stored for it yet", () => {
    patchUiState({ sid: "mine" });
    const onEvent = createGatewayEventHandler(ctx());

    onEvent({
      payload: { sessions: [row({ id: "mine" }), row()] },
      type: "session.active_list",
    } as any);

    expect($stripSessions.get()).toEqual([
      row({ current: true, id: "mine" }),
      row(),
    ]);

    // after a switch to bg-1 the stored mark on "mine" is out of date: the known own session wins
    patchUiState({ sid: "bg-1" });
    onEvent({
      payload: { sessions: [row({ id: "mine" }), row({ status: "idle" })] },
      type: "session.active_list",
    } as any);

    expect($stripSessions.get()).toEqual([
      row({ id: "mine" }),
      row({ current: true, status: "idle" }),
    ]);
  });

  it("a pushed session.active_list event updates the strip without waiting for the poll", () => {
    patchUiState({ sid: "mine" });
    const onEvent = createGatewayEventHandler(ctx());

    onEvent({
      payload: { sessions: [row({ status: "needs_input" })] },
      type: "session.active_list",
    } as any);
    expect($stripSessions.get()).toEqual([row({ status: "needs_input" })]);

    const sets = vi.fn();
    const off = $stripSessions.listen(sets);

    onEvent({
      payload: { sessions: [row({ status: "needs_input" })] },
      type: "session.active_list",
    } as any);
    expect(sets).not.toHaveBeenCalled();
    off();
  });
});
