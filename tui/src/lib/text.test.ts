import { describe, expect, it } from "vitest";

import {
  clarifyAnswerDisplay,
  clarifyBatchRevisitState,
  decodeMultiAnswer,
  encodeMultiAnswer,
  formatAbandonedClarify,
  formatAbandonedClarifyBatch,
  stripTrailingPasteNewlines,
} from "./text.js";

describe("stripTrailingPasteNewlines", () => {
  it("removes trailing newline runs from pasted text", () => {
    expect(stripTrailingPasteNewlines("alpha\n")).toBe("alpha");
    expect(stripTrailingPasteNewlines("alpha\nbeta\n\n")).toBe("alpha\nbeta");
  });

  it("preserves interior newlines", () => {
    expect(stripTrailingPasteNewlines("alpha\nbeta\ngamma")).toBe(
      "alpha\nbeta\ngamma",
    );
  });

  it("preserves newline-only pastes", () => {
    expect(stripTrailingPasteNewlines("\n\n")).toBe("\n\n");
  });
});

describe("formatAbandonedClarify", () => {
  it("renders the question, numbered options, and reason", () => {
    const out = formatAbandonedClarify(
      "How do you want to scope?",
      ["Option A", "Option B", "Option C"],
      "timed out",
    );

    expect(out).toBe(
      [
        "ask How do you want to scope?",
        "  1. Option A",
        "  2. Option B",
        "  3. Option C",
        "  (timed out — no selection)",
      ].join("\n"),
    );
  });

  it("handles a prompt with no choices (free-text clarify)", () => {
    const out = formatAbandonedClarify(
      "What is the target branch?",
      null,
      "cancelled",
    );

    expect(out).toBe(
      ["ask What is the target branch?", "  (cancelled — no selection)"].join(
        "\n",
      ),
    );
  });

  it("trims surrounding whitespace on the question", () => {
    const out = formatAbandonedClarify("  trailing space  ", [], "timed out");

    expect(out.split("\n")[0]).toBe("ask trailing space");
  });
});

describe("formatAbandonedClarifyBatch", () => {
  it("shows locked answers and marks unanswered questions", () => {
    const out = formatAbandonedClarifyBatch(
      [
        { qid: "q0", question: "One?" },
        { qid: "q1", question: "Two?" },
      ],
      { q0: "alpha" },
      "timed out",
    );

    expect(out).toBe(
      [
        "ask (2 questions)",
        "  ✓ One? → alpha",
        "  · Two? (no answer)",
        "  (timed out)",
      ].join("\n"),
    );
  });

  it("shows a multi-select answer decoded, a single-select one verbatim", () => {
    const out = formatAbandonedClarifyBatch(
      [
        { multiSelect: true, qid: "q0", question: "Many?" },
        { qid: "q1", question: "One?" },
        { multiSelect: true, qid: "q2", question: "None?" },
      ],
      { q0: '["a","b"]', q1: '["x"]', q2: "[]" },
      "timed out",
    );

    expect(out).toBe(
      [
        "ask (3 questions)",
        "  ✓ Many? → a, b",
        '  ✓ One? → ["x"]',
        "  · None? (no answer)",
        "  (timed out)",
      ].join("\n"),
    );
  });

  it("treats an empty locked answer as unanswered in the record", () => {
    const out = formatAbandonedClarifyBatch(
      [{ qid: "q0", question: "One?" }],
      { q0: "" },
      "cancelled",
    );

    expect(out).toContain("· One? (no answer)");
  });
});

describe("multi-select answer encoding", () => {
  it("round-trips labels and typed text through a JSON array string", () => {
    const raw = encodeMultiAnswer(["a", "typed, with comma", '"q"']);

    expect(raw).toBe('["a","typed, with comma","\\"q\\""]');
    expect(decodeMultiAnswer(raw)).toEqual(["a", "typed, with comma", '"q"']);
  });

  it("decodes anything that is not a JSON list of strings as [raw]", () => {
    expect(decodeMultiAnswer("plain")).toEqual(["plain"]);
    expect(decodeMultiAnswer("[1,2]")).toEqual(["[1,2]"]);
    expect(decodeMultiAnswer('{"a":1}')).toEqual(['{"a":1}']);
    expect(decodeMultiAnswer("[")).toEqual(["["]);
  });
});

describe("clarifyAnswerDisplay", () => {
  it("joins a multi-select answer's items", () => {
    expect(clarifyAnswerDisplay({ multiSelect: true }, '["a","b"]')).toBe(
      "a, b",
    );
  });

  it("keeps a typed single-select answer verbatim even when it looks like JSON", () => {
    expect(clarifyAnswerDisplay({ multiSelect: false }, '["x"]')).toBe('["x"]');
    expect(clarifyAnswerDisplay({}, '["x"]')).toBe('["x"]');
  });

  it("shows an empty multi-select answer as empty", () => {
    expect(clarifyAnswerDisplay({ multiSelect: true }, "[]")).toBe("");
  });
});

describe("clarifyBatchRevisitState", () => {
  it("restores the cursor onto a choice answer", () => {
    expect(clarifyBatchRevisitState(["red", "blue"], "blue")).toEqual({
      custom: "",
      sel: 1,
    });
  });

  it("stages a typed answer on the Other row for editing", () => {
    expect(clarifyBatchRevisitState(["red", "blue"], "chartreuse")).toEqual({
      custom: "chartreuse",
      sel: 2,
    });
  });

  it("stages a typed answer for an open-ended question (no choices)", () => {
    expect(clarifyBatchRevisitState([], "free text")).toEqual({
      custom: "free text",
      sel: 0,
    });
  });

  it("resets cleanly for unanswered and empty answers", () => {
    expect(clarifyBatchRevisitState(["red"], undefined)).toEqual({
      custom: "",
      sel: 0,
    });
    expect(clarifyBatchRevisitState(["red"], "")).toEqual({
      custom: "",
      sel: 0,
    });
  });
});
