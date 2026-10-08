import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const entry = join(__dirname, "../../../dist/entry.js");

// Needs `npm run build` first; skipped on a fresh checkout.
describe.skipIf(!existsSync(entry))("cut features in built bundle", () => {
  const src = existsSync(entry) ? readFileSync(entry, "utf8") : "";

  it.each([
    "billing\\.",
    "subscription\\.",
    "topup",
    "free_tier",
    "vault\\.",
    "bot_relay",
    "voice\\.(toggle|record|tts)",
    "wake\\.",
    "pet\\.(hatch|generate|set)",
    "hatch",
    "connection\\.(request|update)",
    "nous",
  ])("has no %s", (pattern) => {
    expect(src).not.toMatch(new RegExp(`['"\`]${pattern}`, "i"));
  });
});
