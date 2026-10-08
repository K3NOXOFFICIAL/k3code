import { describe, expect, it } from "vitest";

import {
  focusVisibleMessages,
  shouldShowInFocusMode,
} from "../../k3/focusPolicy.js";
import type { Msg } from "../../types.js";

const ev = (type: string, extra: Record<string, unknown> = {}) =>
  ({ payload: {}, type, ...extra }) as never;

describe("shouldShowInFocusMode (event type × importance)", () => {
  const table: [string, string | undefined, boolean][] = [
    ["message.complete", undefined, true],
    ["message.delta", undefined, false],
    ["message.interim", undefined, false],
    ["error", undefined, true],
    ["tool.start", undefined, false],
    ["tool.complete", undefined, false],
    ["reasoning.delta", undefined, false],
    ["thinking.delta", undefined, false],
    ["todo.updated", undefined, false],
    ["status.update", undefined, false],
    ["subagent.progress", undefined, false],
    ["tool.start", "essential", true],
    ["tool.start", "progress", false],
    ["tool.start", "debug", false],
    ["message.complete", "debug", false],
    ["message.complete", "progress", false],
    ["status.update", "essential", true],
    ["error", "debug", false],
  ];

  it.each(table)("%s importance=%s → shown=%s", (type, importance, shown) => {
    expect(
      shouldShowInFocusMode(ev(type, importance ? { importance } : {})),
    ).toBe(shown);
  });

  it("notifications: only warn/error show", () => {
    expect(
      shouldShowInFocusMode(
        ev("notification.show", { payload: { level: "error" } }),
      ),
    ).toBe(true);
    expect(
      shouldShowInFocusMode(
        ev("notification.show", { payload: { level: "warn" } }),
      ),
    ).toBe(true);
    expect(
      shouldShowInFocusMode(
        ev("notification.show", { payload: { level: "info" } }),
      ),
    ).toBe(false);
    expect(
      shouldShowInFocusMode(
        ev("notification.show", {
          importance: "essential",
          payload: { level: "info" },
        }),
      ),
    ).toBe(true);
  });
});

describe("focusVisibleMessages", () => {
  const m = (
    role: Msg["role"],
    text: string,
    extra: Partial<Msg> = {},
  ): Msg => ({ role, text, ...extra });

  it("keeps user msgs, errors and only the final assistant answer per turn", () => {
    const msgs = [
      m("user", "q1"),
      m("assistant", "let me look"),
      m("tool", "read_file x"),
      m("assistant", "", { thinking: "hmm" }),
      m("assistant", "answer 1"),
      m("system", "error: boom"),
      m("system", "status chatter"),
      m("user", "q2"),
      m("assistant", "answer 2"),
      m("assistant", "noisy", { importance: "debug" } as Partial<Msg>),
      m("assistant", "pinned", { importance: "essential" } as Partial<Msg>),
    ];

    expect(focusVisibleMessages(msgs)).toEqual([
      true,
      false,
      false,
      false,
      true,
      true,
      false,
      true,
      true,
      false,
      true,
    ]);
  });
});
