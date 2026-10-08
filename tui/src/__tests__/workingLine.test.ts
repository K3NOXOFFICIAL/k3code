import { describe, expect, it } from "vitest";

import { WORKING_MESSAGES } from "../content/workingMessages.js";
import {
  isReducedMotion,
  MIN_ANIMATION_TICK_MS,
  REDUCED_MOTION_ENV,
} from "../lib/animation.js";
import {
  advanceRotation,
  formatWorkingLine,
  pickMessageIndex,
  type Rng,
  SPINNER_FRAMES,
  startRotation,
  STATIC_GLYPH,
  WORKING_ROTATE_MS,
  WORKING_TICK_MS,
  workingEffort,
  workingGlyph,
  workingMessage,
} from "../lib/workingLine.js";

// A deterministic RNG that walks a fixed sequence of values in [0, 1).
const sequenceRng = (values: number[]): Rng => {
  let i = 0;

  return () => {
    const value = values[i % values.length] ?? 0;

    i += 1;

    return value;
  };
};

// Seeded LCG so the no-repeat walk is long and reproducible.
const lcg = (seed: number): Rng => {
  let state = seed >>> 0;

  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;

    return state / 2 ** 32;
  };
};

describe("working message pool", () => {
  it("has at least 100 entries", () => {
    expect(WORKING_MESSAGES.length).toBeGreaterThanOrEqual(100);
  });

  it("has no duplicate entries (case-insensitive)", () => {
    const normalised = WORKING_MESSAGES.map((message) =>
      message.trim().toLowerCase(),
    );

    expect(new Set(normalised).size).toBe(normalised.length);
  });

  it("keeps every entry short enough for a status line", () => {
    for (const message of WORKING_MESSAGES) {
      expect(message.length).toBeLessThanOrEqual(32);
      expect(message.trim()).toBe(message);
    }
  });

  it("includes the playful verbs from the issue", () => {
    for (const verb of [
      "Deciphering",
      "Musing",
      "Weaving",
      "Tinkering",
      "Pondering",
    ]) {
      expect(WORKING_MESSAGES).toContain(verb);
    }
  });
});

describe("message rotation", () => {
  it("never shows the same message twice in a row", () => {
    const rng = lcg(42);
    let state = startRotation(rng);
    let previous = workingMessage(state.index);
    let changes = 0;

    for (let segment = 1; segment <= 2000; segment += 1) {
      const next = advanceRotation(state, segment * WORKING_ROTATE_MS, rng);
      const message = workingMessage(next.index);

      expect(message).not.toBe(previous);
      if (next !== state) {
        changes += 1;
      }

      previous = message;
      state = next;
    }

    expect(changes).toBe(2000);
  });

  it("changes only when a new rotation segment starts", () => {
    const rng = sequenceRng([0.5]);
    const start = startRotation(rng);

    expect(advanceRotation(start, WORKING_ROTATE_MS - 1, rng)).toBe(start);
    expect(advanceRotation(start, WORKING_ROTATE_MS * 0.99, rng)).toBe(start);

    const moved = advanceRotation(start, WORKING_ROTATE_MS, rng);

    expect(moved.segment).toBe(1);
    expect(moved.index).not.toBe(start.index);
  });

  it("pickMessageIndex excludes the previous index and stays in range", () => {
    for (let exclude = 0; exclude < 10; exclude += 1) {
      for (const value of [0, 0.1, 0.5, 0.9, 0.999999]) {
        const index = pickMessageIndex(() => value, 10, exclude);

        expect(index).not.toBe(exclude);
        expect(index).toBeGreaterThanOrEqual(0);
        expect(index).toBeLessThan(10);
      }
    }
  });

  it("pickMessageIndex with an injected RNG is deterministic", () => {
    const picks = (seed: number) => {
      const rng = lcg(seed);

      return Array.from({ length: 20 }, () =>
        pickMessageIndex(rng, WORKING_MESSAGES.length),
      );
    };

    expect(picks(7)).toEqual(picks(7));
  });
});

describe("working line format", () => {
  const base = {
    elapsedMs: 41 * 60_000 + 1000,
    glyph: "✢",
    message: "Deciphering",
  };

  it("matches the issue shape with tokens and effort", () => {
    expect(
      formatWorkingLine({ ...base, effort: "xhigh", outputTokens: 135_200 }),
    ).toBe(
      "✢ Deciphering… (41m 1s · ↓ 135.2k tokens · thinking with xhigh effort)",
    );
  });

  it("omits the effort part when the gateway sent none", () => {
    expect(formatWorkingLine({ ...base, outputTokens: 138_900 })).toBe(
      "✢ Deciphering… (41m 1s · ↓ 138.9k tokens)",
    );
  });

  it("shows only the clock when the gateway has sent no tokens or effort", () => {
    expect(formatWorkingLine({ ...base, effort: "", outputTokens: 0 })).toBe(
      "✢ Deciphering… (41m 1s)",
    );
  });

  it("reads the effort only from a real level", () => {
    expect(workingEffort("xhigh")).toBe("xhigh");
    expect(workingEffort(" High ")).toBe("high");
    expect(workingEffort("none")).toBe("");
    expect(workingEffort("default")).toBe("");
    expect(workingEffort("")).toBe("");
    expect(workingEffort(null)).toBe("");
  });
});

describe("spinner and reduced motion", () => {
  it("cycles the spinner glyphs when animated", () => {
    const seen = new Set(
      Array.from({ length: SPINNER_FRAMES.length }, (_, tick) =>
        workingGlyph(tick, false),
      ),
    );

    expect(seen).toEqual(new Set(SPINNER_FRAMES));
  });

  it("shows one static glyph under reduced motion", () => {
    for (let tick = 0; tick < 10; tick += 1) {
      expect(workingGlyph(tick, true)).toBe(STATIC_GLYPH);
    }
  });

  it("keeps the working timer at or above the animation floor", () => {
    expect(WORKING_TICK_MS).toBeGreaterThanOrEqual(250);
    expect(WORKING_TICK_MS).toBeGreaterThanOrEqual(MIN_ANIMATION_TICK_MS);
  });

  it("reads K3_NO_ANIMATION as the reduced-motion switch", () => {
    expect(REDUCED_MOTION_ENV).toBe("K3_NO_ANIMATION");
    expect(isReducedMotion({ K3_NO_ANIMATION: "1" })).toBe(true);
    expect(isReducedMotion({ K3_NO_ANIMATION: "true" })).toBe(true);
    expect(isReducedMotion({ K3_NO_ANIMATION: " YES " })).toBe(true);
    expect(isReducedMotion({ K3_NO_ANIMATION: "0" })).toBe(false);
    expect(isReducedMotion({ K3_NO_ANIMATION: "" })).toBe(false);
    expect(isReducedMotion({})).toBe(false);
  });
});
