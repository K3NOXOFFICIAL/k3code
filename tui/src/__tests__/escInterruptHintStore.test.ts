import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  $escInterruptHint,
  hideEscInterruptHint,
  showEscInterruptHint,
} from "../app/escInterruptHintStore.js";
import { DOUBLE_ESC_MS } from "../config/timing.js";

describe("escInterruptHintStore: the hint lives for one double-Esc window", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    hideEscInterruptHint();
    vi.useRealTimers();
  });

  it("is off until shown", () => {
    expect($escInterruptHint.get()).toBe(false);
  });

  it("stays up for DOUBLE_ESC_MS, then lapses on its own", () => {
    showEscInterruptHint();

    vi.advanceTimersByTime(DOUBLE_ESC_MS - 1);
    expect($escInterruptHint.get()).toBe(true);

    vi.advanceTimersByTime(1);
    expect($escInterruptHint.get()).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("restarts the window when shown again, leaving one timer", () => {
    showEscInterruptHint();
    vi.advanceTimersByTime(DOUBLE_ESC_MS - 100);
    showEscInterruptHint();

    expect(vi.getTimerCount()).toBe(1);

    vi.advanceTimersByTime(DOUBLE_ESC_MS - 1);
    expect($escInterruptHint.get()).toBe(true);

    vi.advanceTimersByTime(1);
    expect($escInterruptHint.get()).toBe(false);
  });

  it("hides at once and drops its timer", () => {
    showEscInterruptHint();
    hideEscInterruptHint();

    expect($escInterruptHint.get()).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});
