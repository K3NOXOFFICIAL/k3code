import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { renderSync, stringWidth } from "@k3code/ink";
import React from "react";
import stripAnsi from "strip-ansi";
import { afterEach, describe, expect, it, vi } from "vitest";

import { optionWindow, QuestionPicker } from "../components/questionPicker.js";
import { encodeMultiAnswer } from "../lib/text.js";
import { DEFAULT_THEME } from "../theme.js";
import type {
  ClarifyBatchQuestion,
  ClarifyBatchResult,
  ClarifyReq,
} from "../types.js";

import { waitFor } from "./waitFor.js";

class FakeInput extends EventEmitter {
  chunks: string[] = [];
  isRaw = false;
  isTTY = true;
  readableLength = 0;

  read() {
    const next = this.chunks.shift() ?? null;
    this.readableLength = this.chunks.length;

    return next;
  }

  ref = vi.fn();

  send(...chunks: string[]) {
    this.chunks.push(...chunks);
    this.readableLength = this.chunks.length;
    this.emit("readable");
  }

  setEncoding = vi.fn();

  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled;
  });

  unref = vi.fn();
}

const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

const DOWN = "\u001b[B";
const UP = "\u001b[A";
const ENTER = "\r";
const ESC = "\u001b";
const TAB = "\t";
const SHIFT_TAB = "\u001b[Z";
const SPACE = " ";

const req = (
  questions: ClarifyBatchQuestion[],
  answers?: Record<string, string>,
): ClarifyReq => ({
  answers,
  choices: null,
  question: "",
  questions,
  requestId: "batch-1",
});

const COLOUR: ClarifyBatchQuestion = {
  choices: ["red", "blue"],
  header: "Colour",
  qid: "q1",
  question: "Which colour?",
};

const SIZE: ClarifyBatchQuestion = {
  choices: ["small", "medium"],
  header: "Size",
  qid: "q2",
  question: "Which size?",
};

const TOPPINGS: ClarifyBatchQuestion = {
  choices: ["cheese", "ham", "olives"],
  descriptions: ["Melted on top", "", "Green ones"],
  header: "Toppings",
  multiSelect: true,
  qid: "q3",
  question: "Which toppings?",
};

function mount(request: ClarifyReq, cols = 100) {
  const stdin = new FakeInput();
  const stdout = new PassThrough();
  let output = "";

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 30 });
  stdout.on("data", (chunk) => {
    output += String(chunk);
  });

  const onClarifyBatch = vi.fn<(r: ClarifyBatchResult | null) => void>();

  const instance = renderSync(
    <QuestionPicker
      cols={cols}
      onClarifyBatch={onClarifyBatch}
      req={request}
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: new PassThrough() as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
    },
  );

  // One key per read; a lone Esc is only emitted after Ink's 50 ms escape-sequence flush.
  const press = async (...keys: string[]) => {
    for (const key of keys) {
      stdin.send(key);
      await settle(key === ESC ? 100 : 30);
    }
  };

  const mark = () => output.length;

  return {
    frame: () => stripAnsi(output),
    /** Everything painted since `mark()`. */
    since: (at: number) => stripAnsi(output.slice(at)),
    mark,
    onClarifyBatch,
    press,
    stop: () => {
      instance.unmount();
      instance.cleanup();
    },
  };
}

const mounted: { stop: () => void }[] = [];
const open = (request: ClarifyReq, cols?: number) => {
  const view = mount(request, cols);

  mounted.push(view);

  return view;
};

afterEach(() => {
  mounted.splice(0).forEach((view) => view.stop());
});

describe("QuestionPicker", () => {
  it("paints a tab per question plus Submit, the question and its rows", async () => {
    const view = open(req([COLOUR, TOPPINGS]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));

    const frame = view.frame();

    expect(frame).toMatch(/← +☐ Colour +☐ Toppings +✓ Submit +→/);
    expect(frame).toContain("▸ 1. red");
    expect(frame).toContain("2. blue");
    expect(frame).toContain("Type something");
    expect(frame).toContain("Chat about this");
    expect(frame).toContain("Tab switch question");
  });

  it("advances on each single-select pick and sends one frame from Submit", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press("2");
    await waitFor(() => expect(view.frame()).toContain("Which size?"));
    await view.press(DOWN, ENTER);
    await waitFor(() => expect(view.frame()).toContain("Review your answers"));

    const review = view.frame();

    expect(review).toMatch(/☑ Colour +☑ Size/);
    expect(review).toContain("Colour → blue");
    expect(review).toContain("Size → medium");
    expect(view.onClarifyBatch).not.toHaveBeenCalled();

    await view.press(ENTER);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: { q1: "blue", q2: "medium" },
    });

    // The picker is done: further keys send nothing.
    await view.press(ENTER, ESC);
    expect(view.onClarifyBatch).toHaveBeenCalledTimes(1);
  });

  it("Shift+Tab and ← go back; the Submit review warns about unanswered questions", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press(TAB, TAB);
    await waitFor(() => expect(view.frame()).toContain("2 unanswered"));
    await view.press(SHIFT_TAB);

    const at = view.mark();

    await view.press("\u001b[D");
    await waitFor(() => expect(view.since(at)).toContain("Which colour?"));
    await view.press("1", TAB, DOWN, ENTER);
    // Submit's second row is Cancel.
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledWith(null));
  });

  it("toggles multi-select rows, adds the typed entry, and encodes the answer", async () => {
    const view = open(req([TOPPINGS]));

    await waitFor(() => expect(view.frame()).toContain("Which toppings?"));
    expect(view.frame()).toContain("Melted on top");
    expect(view.frame()).toContain("Next →");

    // Space toggles cheese, "3" toggles olives, then ↓ ↓ to Type something.
    await view.press(SPACE, "3", DOWN, DOWN, ENTER);
    await waitFor(() => expect(view.frame()).toContain("Enter save"));
    await view.press("m", "i", "n", "e", ENTER);
    await waitFor(() => expect(view.frame()).toContain("[x] mine"));
    expect(view.frame()).toContain("1. [x] cheese");
    expect(view.frame()).toContain("3. [x] olives");

    // ↑ to Next, which advances to the Submit review.
    await view.press(UP, ENTER);
    await waitFor(() =>
      expect(view.frame()).toContain("Toppings → cheese, olives, mine"),
    );
    await view.press(ENTER);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: { q3: encodeMultiAnswer(["cheese", "olives", "mine"]) },
    });
  });

  it("sends a lone single-select question on the pick, without a Submit tab", async () => {
    const view = open(req([COLOUR]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    expect(view.frame()).not.toContain("Submit");
    expect(view.frame()).not.toContain("Tab switch question");

    await view.press("2");
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: { q1: "blue" },
    });
  });

  it("the Enter that commits typed text does not also pick a row on the next question", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press(DOWN, DOWN, ENTER);
    await waitFor(() => expect(view.frame()).toContain("Enter save"));
    await view.press("a", "b", "c", ENTER);
    await waitFor(() => expect(view.frame()).toContain("Which size?"));
    await settle(60);

    // The cursor on Size sits on "small": had the same Enter reached it, Size would be answered and Submit shown.
    expect(view.frame()).not.toContain("Review your answers");
    expect(view.frame()).toMatch(/☑ Colour +☐ Size +✓ Submit/);
    await view.press(TAB);
    await waitFor(() => expect(view.frame()).toContain("Colour → abc"));
    expect(view.onClarifyBatch).not.toHaveBeenCalled();
  });

  it("Chat about this sends chat: true with the answers so far", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press("1");
    await waitFor(() => expect(view.frame()).toContain("Which size?"));
    // small, medium, Type something, Chat about this
    await view.press(DOWN, DOWN, DOWN, ENTER);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: { q1: "red" },
      chat: true,
    });
  });

  it("Esc inside the text field only leaves it; a second Esc cancels", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press(DOWN, DOWN, ENTER);
    await waitFor(() => expect(view.frame()).toContain("Enter save"));
    await view.press("x");

    const at = view.mark();

    await view.press(ESC);
    await waitFor(() => expect(view.since(at)).toContain("Type something"));
    expect(view.onClarifyBatch).not.toHaveBeenCalled();

    await view.press(ESC);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledWith(null));
    expect(view.onClarifyBatch).toHaveBeenCalledTimes(1);
  });

  it("seeds the state from replayed answers and opens on the first unanswered question", async () => {
    const view = open(
      req([COLOUR, TOPPINGS, SIZE], {
        q1: "blue",
        q3: encodeMultiAnswer(["ham", "extra spicy"]),
      }),
    );

    await waitFor(() => expect(view.frame()).toContain("Which size?"));
    expect(view.frame()).toMatch(/☑ Colour +☑ Toppings +☐ Size/);

    // Back to Toppings: ham and the typed entry are checked, cheese is not.
    // (Toppings and Colour were never painted before, so the whole output proves what their tabs show.)
    await view.press(SHIFT_TAB);
    await waitFor(() => expect(view.frame()).toContain("[x] extra spicy"));
    expect(view.frame()).toContain("2. [x] ham");
    expect(view.frame()).toContain("1. [ ] cheese");

    // Back to Colour: blue carries the recorded mark and the cursor.
    await view.press(SHIFT_TAB);
    await waitFor(() => expect(view.frame()).toContain("▸ 2. blue ✓"));

    await view.press(TAB, TAB, "1");
    await waitFor(() => expect(view.frame()).toContain("Review your answers"));
    await view.press(ENTER);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: {
        q1: "blue",
        q2: "small",
        q3: encodeMultiAnswer(["ham", "extra spicy"]),
      },
    });
  });

  it('keeps a ["x"] single-select answer verbatim, replayed or typed', async () => {
    const view = open(req([COLOUR, SIZE], { q1: '["x"]' }));

    await waitFor(() => expect(view.frame()).toContain("Which size?"));

    // The replayed answer matches no choice: it is staged on Type something as is.
    const at = view.mark();

    await view.press(SHIFT_TAB);
    await waitFor(() => expect(view.since(at)).toContain('▸ ["x"] ✓'));
    await view.press(TAB, DOWN, DOWN, ENTER);
    await waitFor(() => expect(view.frame()).toContain("Enter save"));

    for (const ch of '["y"]') {
      await view.press(ch);
    }

    await view.press(ENTER);
    await waitFor(() => expect(view.frame()).toContain('Size → ["y"]'));
    expect(view.frame()).toContain('Colour → ["x"]');
    await view.press(ENTER);
    await waitFor(() => expect(view.onClarifyBatch).toHaveBeenCalledTimes(1));
    expect(view.onClarifyBatch).toHaveBeenCalledWith({
      answers: { q1: '["x"]', q2: '["y"]' },
    });
  });
});

describe("QuestionPicker in a short terminal", () => {
  const original = Object.getOwnPropertyDescriptor(process.stdout, "rows");

  afterEach(() => {
    if (original) {
      Object.defineProperty(process.stdout, "rows", original);
    } else {
      delete (process.stdout as { rows?: number }).rows;
    }
  });

  it("windows the options at 14 rows (app chrome included) and keeps Next, Type something, Chat and the hint", async () => {
    // The vendored Ink's useStdout() reads process.stdout, not the fake stdout.
    Object.defineProperty(process.stdout, "rows", {
      configurable: true,
      value: 14,
      writable: true,
    });

    const many: ClarifyBatchQuestion = {
      choices: Array.from({ length: 8 }, (_, i) => `option ${i + 1}`),
      descriptions: Array.from({ length: 8 }, (_, i) =>
        `A long description of option ${i + 1} `.repeat(12),
      ),
      header: "Many",
      multiSelect: true,
      qid: "q1",
      question: "Pick any?",
    };
    const view = open(req([many]));

    await waitFor(() => expect(view.frame()).toContain("Pick any?"));
    await settle(60);

    // Nothing was pressed, so the whole output is the one frame.
    const lines = view.frame().replace(/\n+$/, "").split("\n");

    expect(lines.length).toBeLessThanOrEqual(14);
    expect(view.frame()).toContain("Next →");
    expect(view.frame()).toContain("Type something");
    expect(view.frame()).toContain("Chat about this");
    expect(view.frame()).toContain("Esc cancel");
    expect(view.frame()).toContain("1-1 of 8");
    expect(view.frame()).toContain("option 1");
    expect(view.frame()).not.toContain("option 2");
    // Each description is cut to one line, marked with an ellipsis.
    const descs = lines.filter((line) => line.includes("A long description"));

    expect(descs).toHaveLength(1);
    descs.forEach((line) => expect(line.trimEnd()).toMatch(/…$/));
  });
});

describe("QuestionPicker layout", () => {
  const HEADED = [
    "Alpha header",
    "Bravo header",
    "Charl header",
    "Delta header",
  ].map((header, i): ClarifyBatchQuestion => ({
    choices: ["yes", "no"],
    header,
    qid: `h${i}`,
    question: `Question ${i}?`,
  }));

  it("shows a long question whole instead of cutting it to two lines", async () => {
    const long = `Should we ${"really carefully consider ".repeat(10)}the final step?`;
    const view = open(req([{ ...COLOUR, question: long }, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("the final step?"));
    expect(view.frame()).not.toContain("…");
  });

  it.each([80, 60])(
    "fits the tab strip into %i columns, keeping the active tab and Submit",
    async (cols) => {
      const view = open(req(HEADED), cols);

      await waitFor(() => expect(view.frame()).toContain("Question 0?"));

      const strip = view
        .frame()
        .split("\n")
        .find((line) => line.includes("←"))!;

      expect(strip).toContain("Alpha header");
      expect(strip).toMatch(/✓ Submit +→/);
      expect(stringWidth(strip.trimEnd())).toBeLessThanOrEqual(cols);
    },
  );

  it("digit 1 on the Submit tab submits, 2 cancels", async () => {
    const view = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(view.frame()).toContain("Which colour?"));
    await view.press("1", "2");
    await waitFor(() => expect(view.frame()).toContain("Review your answers"));
    await view.press("1");
    await waitFor(() =>
      expect(view.onClarifyBatch).toHaveBeenCalledWith({
        answers: { q1: "red", q2: "medium" },
      }),
    );

    const cancel = open(req([COLOUR, SIZE]));

    await waitFor(() => expect(cancel.frame()).toContain("Which colour?"));
    await cancel.press(TAB, TAB, "2");
    await waitFor(() =>
      expect(cancel.onClarifyBatch).toHaveBeenCalledWith(null),
    );
  });

  it("cuts emoji and CJK descriptions by display width on grapheme boundaries", async () => {
    const view = open(
      req([
        {
          choices: ["emoji", "cjk"],
          descriptions: [
            `a${"🎉👍".repeat(60)}`,
            `a${"漢字テスト".repeat(30)}`,
          ],
          header: "Wide",
          qid: "w1",
          question: "Wide?",
        },
      ]),
      60,
    );

    await waitFor(() => expect(view.frame()).toContain("Wide?"));

    const descs = view
      .frame()
      .split("\n")
      .filter((line) => /[🎉漢]/u.test(line));

    expect(descs.length).toBeGreaterThan(0);
    descs.forEach((line) => {
      expect(stringWidth(line.trimEnd())).toBeLessThanOrEqual(60 - 8);
      // No lone surrogate: the cut never splits an emoji.
      expect(line).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
    });
  });
});

describe("optionWindow", () => {
  it("keeps the cursor visible and scrolls only as far as needed", () => {
    const heights = [2, 2, 2, 2, 2];

    expect(optionWindow(heights, 6, 0, 0)).toEqual({ end: 3, start: 0 });
    expect(optionWindow(heights, 6, 2, 0)).toEqual({ end: 3, start: 0 });
    expect(optionWindow(heights, 6, 3, 0)).toEqual({ end: 4, start: 1 });
    expect(optionWindow(heights, 6, 2, 1)).toEqual({ end: 4, start: 1 });
    expect(optionWindow(heights, 6, 4, 0)).toEqual({ end: 5, start: 2 });
    expect(optionWindow([], 6, 0, 0)).toEqual({ end: 0, start: 0 });
  });
});
