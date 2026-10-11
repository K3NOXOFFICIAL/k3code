import { describe, expect, it } from "vitest";

import { compactNumber } from "./format";

describe("compactNumber", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1000, "1k"],
    [1500, "1.5k"],
    [128_000, "128k"],
    // Promotion guard: a value that would round to "1000k" promotes to M.
    [999_949, "999.9k"],
    [999_999, "1M"],
    [1_000_000, "1M"],
    [1_500_000, "1.5M"],
    // `M` is the declared top rung (see format.ts): no `B`, so billions stay in M.
    [1_000_000_000, "1000M"],
  ])("%d → %s", (value, expected) => {
    expect(compactNumber(value)).toBe(expected);
  });

  it('never emits a unit-boundary artifact like "1000k" or "1000"', () => {
    for (const value of [999.5, 999.9, 999_950, 999_999.4]) {
      expect(compactNumber(value)).not.toMatch(/^1000(\.\d)?k?$/);
    }
  });

  // Infinity used to read "0" (as if there were nothing), and a figure beyond 1e21
  // came out as "1e+21M" because toFixed switches to exponent notation there.
  it.each([
    [Number.POSITIVE_INFINITY, "∞"],
    [1e27, "1e+27"],
    [1e15, "1e+15"],
    [2.5e18, "2.5e+18"],
    [1e14, "100000000M"],
    [Number.NaN, "0"],
    [Number.NEGATIVE_INFINITY, "0"],
    [-5, "0"],
    [null, "0"],
    [undefined, "0"],
  ])("%s → %s", (value, expected) => {
    expect(compactNumber(value)).toBe(expected);
  });
});
