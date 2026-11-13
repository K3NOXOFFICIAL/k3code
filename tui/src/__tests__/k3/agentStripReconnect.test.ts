import { beforeEach, describe, expect, it, vi } from "vitest";

import { createGatewayEventHandler } from "../../app/createGatewayEventHandler.js";
import { resetUiState } from "../../app/uiStore.js";
import type { SessionActiveItem } from "../../gatewayTypes.js";
import {
  $gatewayEpoch,
  $stripSessions,
  applyStripSessions,
  stripBackOnline,
  stripOffline,
} from "../../k3/agentStripStore.js";

const row = (over: Partial<SessionActiveItem> = {}): SessionActiveItem => ({
  id: "bg-1",
  status: "working",
  title: "fix the login test",
  turn_started_at: 100,
  ...over,
});

const ctx = () =>
  ({
    composer: { dequeue: () => undefined, queueEditRef: { current: null } },
    gateway: {
      gw: { request: vi.fn(async () => ({})) },
      rpc: vi.fn(async () => null),
    },
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

describe("strip across a gateway drop", () => {
  beforeEach(() => {
    resetUiState();
    $stripSessions.set([]);
  });

  it("empties the strip when the gateway drops, so a session that died with it is not shown as working", () => {
    applyStripSessions([row(), row({ id: "bg-2", status: "needs_input" })]);
    expect($stripSessions.get()).toHaveLength(2);

    stripOffline();
    expect($stripSessions.get()).toEqual([]);
  });

  it("sets nothing when the strip is already empty (no re-render for a drop with nothing to show)", () => {
    const sets = vi.fn();
    const off = $stripSessions.listen(sets);

    stripOffline();
    expect(sets).not.toHaveBeenCalled();
    off();
  });

  it("the next snapshot after the reconnect fills it again", () => {
    applyStripSessions([row()]);
    stripOffline();
    applyStripSessions([row()]);
    expect($stripSessions.get()).toHaveLength(1);
  });

  it("the gateway.reconnecting event clears the rows", () => {
    const onEvent = createGatewayEventHandler(ctx());

    applyStripSessions([row()]);
    onEvent({
      payload: { attempt: 1, delay_ms: 500 },
      type: "gateway.reconnecting",
    } as any);
    expect($stripSessions.get()).toEqual([]);
  });

  it("coming back online bumps the epoch the agent view reloads its lists on", () => {
    const before = $gatewayEpoch.get();

    stripBackOnline();
    expect($gatewayEpoch.get()).toBe(before + 1);
  });
});
