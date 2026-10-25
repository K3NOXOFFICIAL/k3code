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
