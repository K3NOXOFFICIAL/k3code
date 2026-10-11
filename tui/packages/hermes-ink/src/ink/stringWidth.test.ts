import { describe, expect, it } from "vitest";

import { stringWidth } from "./stringWidth.js";

const VS16 = "️";

describe("stringWidth of symbols that are emoji only on request", () => {
  it.each(["⚠", "✔", "❤", "♥", "✈", "☀", "✂", "⚙", "♠", "☑", "✖", "©", "™"])(
    "%s is one cell, like ✓ ★ ▶",
    (symbol) => {
      expect(stringWidth(symbol)).toBe(1);
      expect(stringWidth(`a${symbol}b`)).toBe(3);
    },
  );

  it("does not depend on what else is in the string", () => {
    expect(stringWidth("©a⚠")).toBe(stringWidth("©a") + stringWidth("⚠"));
    expect(stringWidth("✓ ⚠ ★")).toBe(5);
  });
});

describe("stringWidth of real emoji", () => {
  it.each(["😀", "✅", "❌", "⭐", "⚡", "✨", "⌚", "🇺🇸", "👍🏽", "👨‍👩‍👧"])(
    "%s stays two cells",
    (emoji) => {
      expect(stringWidth(emoji)).toBe(2);
    },
  );

  it.each(["⚠", "❤", "©", "✔"])("%s with U+FE0F is the emoji form", (base) => {
    expect(stringWidth(base + VS16)).toBe(2);
  });

  it("counts a mixed line cell by cell", () => {
    expect(stringWidth(`⚠ ⚠${VS16} 😀`)).toBe(1 + 1 + 2 + 1 + 2);
  });
});
