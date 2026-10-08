import { describe, expect, it } from "vitest";

import {
  $sessionOutputTokens,
  $turnTokenBaseline,
  addOutputTokens,
  markTurnStart,
} from "../app/outputTokensStore.js";
import {
  $petEnabled,
  $petName,
  applyPetConfig,
  petConfigValue,
} from "../app/petStore.js";

describe("pet config", () => {
  it("off hides the pet, a name pins it, anything else keeps it on", () => {
    applyPetConfig("off");
    expect($petEnabled.get()).toBe(false);

    applyPetConfig("cat");
    expect($petEnabled.get()).toBe(true);
    expect($petName.get()).toBe("cat");

    $petEnabled.set(false);
    applyPetConfig(undefined);
    expect($petEnabled.get()).toBe(true);
    expect($petName.get()).toBe("cat");
  });

  it("saves off, a pinned name, or on", () => {
    expect(petConfigValue(false, "cat")).toBe("off");
    expect(petConfigValue(true, "cat")).toBe("cat");
    expect(petConfigValue(true, null)).toBe("on");
    expect(petConfigValue(true, "not-a-pet")).toBe("on");
  });
});

describe("turn token baseline", () => {
  it("is the session total at turn start and survives later reads", () => {
    $sessionOutputTokens.set(0);
    addOutputTokens(500);
    markTurnStart();
    addOutputTokens(1200);
    expect($turnTokenBaseline.get()).toBe(500);
    expect($sessionOutputTokens.get() - $turnTokenBaseline.get()).toBe(1200);
  });
});

describe("working line ascii and long turns", async () => {
  const { formatWorkingLine, workingDuration, workingGlyph } =
    await import("../lib/workingLine.js");

  it("keeps seconds past an hour", () => {
    expect(workingDuration(3_723_000)).toBe("1h 2m 3s");
    expect(workingDuration(61_000)).toBe("1m 1s");
  });

  it("uses only ASCII when asked", () => {
    const line = formatWorkingLine({
      ascii: true,
      elapsedMs: 5000,
      glyph: workingGlyph(1, false, true),
      message: "Musing",
      outputTokens: 1500,
    });
    expect(line).toMatch(/^[\x20-\x7e]+$/);
    expect(line).toContain("Musing... (5s, 1.5k tokens)");
  });
});
