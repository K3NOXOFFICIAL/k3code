import { describe, expect, it, vi } from "vitest";

import { reconnectBackoffDelayMs } from "./reconnect-backoff.js";

describe("reconnectBackoffDelayMs", () => {
  it("increases the delay ceiling across consecutive failed attempts", () => {
    // Pin Math.random so we can read the ceiling directly through the
    // returned value instead of statistically sampling it.
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1);

    try {
      const delays = [0, 1, 2, 3, 4].map((attempt) =>
        reconnectBackoffDelayMs(attempt, { baseDelayMs: 300 }),
      );

      expect(delays).toEqual([300, 600, 1200, 2400, 4800]);

      for (let i = 1; i < delays.length; i++) {
        expect(delays[i]).toBeGreaterThan(delays[i - 1]);
      }
    } finally {
      randomSpy.mockRestore();
    }
  });

  it("caps the delay ceiling instead of growing unbounded", () => {
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1);

    try {
      // Attempt 10 would be 300 * 2**10 = 307_200ms uncapped — must clamp.
      expect(
        reconnectBackoffDelayMs(10, { baseDelayMs: 300, capMs: 15_000 }),
      ).toBe(15_000);
      expect(
        reconnectBackoffDelayMs(50, { baseDelayMs: 300, capMs: 15_000 }),
      ).toBe(15_000);
    } finally {
      randomSpy.mockRestore();
    }
  });

  it("applies full jitter: delay is uniformly within [0, ceiling)", () => {
    const randomSpy = vi.spyOn(Math, "random");

    try {
      randomSpy.mockReturnValue(0);
      expect(reconnectBackoffDelayMs(3, { baseDelayMs: 300 })).toBe(0);

      randomSpy.mockReturnValue(0.5);
      expect(reconnectBackoffDelayMs(3, { baseDelayMs: 300 })).toBe(1200);

      randomSpy.mockReturnValue(0.999);
      expect(reconnectBackoffDelayMs(3, { baseDelayMs: 300 })).toBeCloseTo(
        2400 * 0.999,
        5,
      );
    } finally {
      randomSpy.mockRestore();
    }
  });

  it("treats negative attempt numbers as attempt 0 rather than throwing or returning a negative delay", () => {
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1);

    try {
      expect(reconnectBackoffDelayMs(-5, { baseDelayMs: 300 })).toBe(300);
    } finally {
      randomSpy.mockRestore();
    }
  });

  it("jitter: false returns the exact ceiling — the ladder web prints in its banner", () => {
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0.1);

    try {
      expect(
        [0, 1, 2, 3].map((a) =>
          reconnectBackoffDelayMs(a, {
            baseDelayMs: 1000,
            capMs: 30_000,
            jitter: false,
          }),
        ),
      ).toEqual([1000, 2000, 4000, 8000]);
      expect(
        reconnectBackoffDelayMs(99, {
          baseDelayMs: 1000,
          capMs: 30_000,
          jitter: false,
        }),
      ).toBe(30_000);
      expect(
        reconnectBackoffDelayMs(10_000, { jitter: false }),
      ).toBeLessThanOrEqual(15_000);
    } finally {
      randomSpy.mockRestore();
    }
  });

  // setTimeout(NaN) fires immediately: a NaN delay is a reconnect storm.
  it("never returns NaN or a negative delay, whatever the attempt or the options", () => {
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1);

    try {
      expect(reconnectBackoffDelayMs(Number.NaN, { baseDelayMs: 300 })).toBe(
        300,
      );
      expect(
        reconnectBackoffDelayMs(Number.POSITIVE_INFINITY, {
          baseDelayMs: 300,
          capMs: 15_000,
        }),
      ).toBe(15_000);
      expect(
        reconnectBackoffDelayMs(Number.NEGATIVE_INFINITY, { baseDelayMs: 300 }),
      ).toBe(300);

      for (const bad of [Number.NaN, -1, Number.NEGATIVE_INFINITY]) {
        const delay = reconnectBackoffDelayMs(2, {
          baseDelayMs: bad,
          capMs: bad,
        });

        expect(Number.isFinite(delay) && delay >= 0).toBe(true);
        expect(delay).toBe(1200); // the defaults: 300 * 2**2
      }

      // an infinite cap means "no cap", and is not a bad value
      expect(
        reconnectBackoffDelayMs(20, {
          baseDelayMs: 100,
          capMs: Number.POSITIVE_INFINITY,
        }),
      ).toBe(100 * 2 ** 20);
    } finally {
      randomSpy.mockRestore();
    }
  });
});
