import { describe, expect, it } from "vitest";

import {
  CellWidth,
  CharPool,
  createScreen,
  HyperlinkPool,
  setCellAt,
  StylePool,
} from "./screen.js";
import {
  captureScrolledRows,
  createSelectionState,
  getSelectedText,
  paintedScrollDelta,
  shiftAnchor,
  startSelection,
  updateSelection,
} from "./selection.js";

// A 10x4 screen whose rows read "row0".."row3".
const screenWithRows = () => {
  const screen = createScreen(
    10,
    4,
    new StylePool(),
    new CharPool(),
    new HyperlinkPool(),
  );

  for (let row = 0; row < 4; row++) {
    const text = `row${row}`;

    for (let col = 0; col < text.length; col++) {
      setCellAt(screen, col, row, {
        char: text[col]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow,
      });
    }
  }

  return screen;
};

/**
 * One drag-to-scroll tick the way Ink.stepSelectionAutoScroll runs it:
 * capture the rows that will leave the painted viewport, then shift the
 * anchor by the same amount. `painted` is what the last frame drew
 * (DOMElement.scrollTopPainted); `next` the scrollTop the tick asks for.
 */
const tick = (
  selection: ReturnType<typeof createSelectionState>,
  screen: ReturnType<typeof screenWithRows>,
  painted: number,
  next: number,
  clamp: { max?: number; min?: number },
) => {
  const top = 0;
  const bottom = 3;
  const moved = paintedScrollDelta(painted, next, clamp.min, clamp.max);

  if (moved > 0) {
    captureScrolledRows(
      selection,
      screen,
      top,
      Math.min(bottom, top + moved - 1),
      "above",
    );
    shiftAnchor(selection, -moved, top, bottom);
  }

  return moved;
};

describe("drag-to-scroll capture under the virtual-scroll clamp", () => {
  it("moves by what the paint will move, not by the requested step", () => {
    // Clamp holds the paint at row 10 while scrollTop asks for 11, 12, 13.
    expect(paintedScrollDelta(10, 11, 0, 10)).toBe(0);
    expect(paintedScrollDelta(10, 13, 0, 10)).toBe(0);
    // Clamp widens: the paint catches up by several rows at once.
    expect(paintedScrollDelta(10, 13, 0, 20)).toBe(3);
    // No clamp: the step is the move.
    expect(paintedScrollDelta(10, 11)).toBe(1);
    expect(paintedScrollDelta(11, 10)).toBe(-1);
  });

  it("captures a row once while the clamp holds the screen still", () => {
    const screen = screenWithRows();
    const selection = createSelectionState();

    startSelection(selection, 0, 0);
    updateSelection(selection, 9, 3);

    // Three ticks with the paint pinned at scrollTop 0 (clampMax 0): the
    // screen does not move, so nothing leaves it and nothing is captured.
    expect(tick(selection, screen, 0, 1, { max: 0, min: 0 })).toBe(0);
    expect(tick(selection, screen, 0, 2, { max: 0, min: 0 })).toBe(0);
    expect(tick(selection, screen, 0, 3, { max: 0, min: 0 })).toBe(0);
    expect(selection.scrolledOffAbove).toEqual([]);

    // The clamp releases: the paint jumps from 0 to 3, three rows leave.
    expect(tick(selection, screen, 0, 3, { max: 3, min: 0 })).toBe(3);
    expect(selection.scrolledOffAbove).toEqual(["row0", "row1", "row2"]);
    expect(selection.anchor?.row).toBe(0);
  });

  it("the old per-tick capture repeated the same row (regression)", () => {
    const screen = screenWithRows();
    const selection = createSelectionState();

    startSelection(selection, 0, 0);
    updateSelection(selection, 9, 3);

    // What stepSelectionAutoScroll did before: one row per tick regardless
    // of whether the screen moved.
    for (let i = 0; i < 3; i++) {
      captureScrolledRows(selection, screen, 0, 0, "above");
      shiftAnchor(selection, -1, 0, 3);
    }

    expect(selection.scrolledOffAbove).toEqual(["row0", "row0", "row0"]);
    expect(getSelectedText(selection, screen).split("\n")).toEqual([
      "row0",
      "row0",
      "row0",
      "row0",
      "row1",
      "row2",
      "row3",
    ]);
  });
});
