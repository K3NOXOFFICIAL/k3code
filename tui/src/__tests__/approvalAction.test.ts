import { describe, expect, it } from "vitest";

import {
  approvalAction,
  approvalOptions,
  DIFF_VIEW_LINES,
  diffLineKind,
  previewWindow,
} from "../components/prompts.js";

describe("approvalAction — pure key dispatch for ApprovalPrompt", () => {
  it("maps Esc to deny — parity with global Ctrl+C cancellation", () => {
    expect(approvalAction("", { escape: true }, 0)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("", { escape: true }, 2)).toEqual({
      kind: "choose",
      choice: "deny",
    });
  });

  it("maps number keys 1..4 to once/session/always/deny in registration order", () => {
    expect(approvalAction("1", {}, 0)).toEqual({
      kind: "choose",
      choice: "once",
    });
    expect(approvalAction("2", {}, 0)).toEqual({
      kind: "choose",
      choice: "session",
    });
    expect(approvalAction("3", {}, 0)).toEqual({
      kind: "choose",
      choice: "always",
    });
    expect(approvalAction("4", {}, 0)).toEqual({
      kind: "choose",
      choice: "deny",
    });
  });

  it("ignores out-of-range numbers", () => {
    expect(approvalAction("0", {}, 1)).toEqual({ kind: "noop" });
    expect(approvalAction("5", {}, 1)).toEqual({ kind: "noop" });
    expect(approvalAction("9", {}, 1)).toEqual({ kind: "noop" });
  });

  it("confirms the current selection on Enter", () => {
    expect(approvalAction("", { return: true }, 0)).toEqual({
      kind: "choose",
      choice: "once",
    });
    expect(approvalAction("", { return: true }, 3)).toEqual({
      kind: "choose",
      choice: "deny",
    });
  });

  it("moves selection up/down within bounds", () => {
    expect(approvalAction("", { upArrow: true }, 2)).toEqual({
      kind: "move",
      delta: -1,
    });
    expect(approvalAction("", { downArrow: true }, 1)).toEqual({
      kind: "move",
      delta: 1,
    });
  });

  it("clamps selection movement at the edges", () => {
    expect(approvalAction("", { upArrow: true }, 0)).toEqual({ kind: "noop" });
    expect(approvalAction("", { downArrow: true }, 3)).toEqual({
      kind: "noop",
    });
  });

  it("Esc beats numeric/return — denying is always the first interpretation", () => {
    // If a terminal somehow delivers Esc + a digit in the same event, deny
    // wins.  Documents the precedence so a future refactor doesn't flip it.
    expect(approvalAction("1", { escape: true }, 0)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("", { escape: true, return: true }, 1)).toEqual({
      kind: "choose",
      choice: "deny",
    });
  });

  it("disarms on typed text instead of letting it pass silently", () => {
    // A letter, a space or any other printable key is not an answer; it
    // disarms the prompt so the Enter that usually follows cannot approve.
    expect(approvalAction("a", {}, 0)).toEqual({ kind: "disarm" });
    expect(approvalAction(" ", {}, 0)).toEqual({ kind: "disarm" });
    expect(approvalAction("é", {}, 0)).toEqual({ kind: "disarm" });
  });

  it("does not disarm on keys that carry no text", () => {
    expect(approvalAction("", {}, 0)).toEqual({ kind: "noop" });
    expect(approvalAction("\t", {}, 0)).toEqual({ kind: "noop" });
    expect(approvalAction("x", { ctrl: true }, 0)).toEqual({ kind: "noop" });
    expect(approvalAction("x", { meta: true }, 0)).toEqual({ kind: "noop" });
  });

  it("respects a reduced option set when permanent allow is disabled", () => {
    // tirith content-security warning present → no "always"; the 3-item set is
    // once/session/deny, so 3 maps to deny and 4 is out of range.
    const opts = ["once", "session", "deny"] as const;

    expect(approvalAction("3", {}, 0, opts)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("4", {}, 0, opts)).toEqual({ kind: "noop" });
    expect(approvalAction("", { downArrow: true }, 2, opts)).toEqual({
      kind: "noop",
    });
    expect(approvalAction("", { return: true }, 2, opts)).toEqual({
      kind: "choose",
      choice: "deny",
    });
  });

  it("offers only once and deny for Smart DENY owner override", () => {
    const opts = approvalOptions({
      allowPermanent: true,
      command: "rm -rf /",
      description: "blocked",
      smartDenied: true,
    });

    expect(opts).toEqual(["once", "deny"]);
    expect(approvalAction("2", {}, 0, opts)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("3", {}, 0, opts)).toEqual({ kind: "noop" });
  });

  it("quick-picks only on one typed digit, never on a burst or a paste", () => {
    // A buffered typing burst and a bracketed paste reach useInput as one
    // string; reading its leading digit would answer the prompt.
    expect(approvalAction("3 tests failed", {}, 0)).toEqual({
      kind: "disarm",
    });
    expect(approvalAction("2nd try", {}, 0)).toEqual({ kind: "disarm" });
    expect(approvalAction("1", { isPasted: true }, 0)).toEqual({
      kind: "disarm",
    });
    expect(approvalAction("", { isPasted: true }, 0)).toEqual({
      kind: "disarm",
    });
  });

  it("disarmed: Enter and allow digits do nothing, deny still works", () => {
    expect(approvalAction("", { return: true }, 0, undefined, true)).toEqual({
      kind: "noop",
    });
    expect(approvalAction("1", {}, 0, undefined, true)).toEqual({
      kind: "noop",
    });
    expect(approvalAction("3", {}, 0, undefined, true)).toEqual({
      kind: "noop",
    });
    expect(approvalAction("4", {}, 0, undefined, true)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("", { escape: true }, 0, undefined, true)).toEqual({
      kind: "choose",
      choice: "deny",
    });
    expect(approvalAction("d", {}, 0, undefined, true)).toEqual({
      kind: "disarm",
    });
  });

  it("disarmed: ↑/↓ re-arm, also at the edges", () => {
    expect(approvalAction("", { downArrow: true }, 0, undefined, true)).toEqual(
      { kind: "move", delta: 1 },
    );
    expect(approvalAction("", { upArrow: true }, 0, undefined, true)).toEqual({
      kind: "arm",
    });
    expect(approvalAction("", { downArrow: true }, 3, undefined, true)).toEqual(
      { kind: "arm" },
    );
  });

  it("uses explicit gateway choices as the prompt contract", () => {
    expect(
      approvalOptions({
        allowPermanent: true,
        choices: ["once", "deny"],
        command: "rm -rf /",
        description: "blocked",
      }),
    ).toEqual(["once", "deny"]);
  });

  it("pages the change preview with [ and ] without touching the choice", () => {
    expect(approvalAction("[", {}, 1)).toEqual({ kind: "scroll", delta: -1 });
    expect(approvalAction("]", {}, 1)).toEqual({ kind: "scroll", delta: 1 });
  });

  it("scrolls while disarmed, but a pasted [ or ] disarms", () => {
    expect(approvalAction("]", {}, 0, undefined, true)).toEqual({
      kind: "scroll",
      delta: 1,
    });
    expect(approvalAction("[", { isPasted: true }, 0)).toEqual({
      kind: "disarm",
    });
    expect(approvalAction("[x] done", {}, 0)).toEqual({ kind: "disarm" });
  });
});

describe("change preview of an edit/write approval (issue #131)", () => {
  const diff = [
    "--- a/app.py",
    "+++ b/app.py",
    "@@ -1,3 +1,3 @@",
    " import os",
    "-x = 1",
    "+x = 2",
    " print(x)",
  ].join("\n");

  it("classifies diff rows for colouring", () => {
    expect(diff.split("\n").map(diffLineKind)).toEqual([
      "file",
      "file",
      "hunk",
      "context",
      "del",
      "add",
      "context",
    ]);
  });

  it("shows the whole preview when it fits", () => {
    expect(previewWindow(diff, 0)).toEqual({
      lines: diff.split("\n"),
      offset: 0,
      total: 7,
    });
  });

  it("windows a long preview and clamps at both ends", () => {
    const long = Array.from({ length: 50 }, (_, i) => `+line ${i}`).join("\n");

    expect(previewWindow(long, 0).lines).toHaveLength(DIFF_VIEW_LINES);
    expect(previewWindow(long, -5).offset).toBe(0);
    expect(previewWindow(long, 1000).offset).toBe(50 - DIFF_VIEW_LINES);
    expect(previewWindow(long, 1000).lines.at(-1)).toBe("+line 49");
    expect(previewWindow(long, 20, 10).lines[0]).toBe("+line 20");
  });
});
