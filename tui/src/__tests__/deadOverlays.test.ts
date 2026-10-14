import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { $overlayState } from "../app/overlayStore.js";

const SRC = join(import.meta.dirname, "..");

const sources = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);

    if (statSync(path).isDirectory()) {
      return name === "__tests__" ? [] : sources(path);
    }

    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });

// The skills hub, plugins hub and journey overlays were never opened by anything
// and called RPCs the gateway has no handler for (K3NOX-278, k3code #170).
describe("dead gateway-less overlays", () => {
  it("are not overlay state any more", () => {
    const state = $overlayState.get() as unknown as Record<string, unknown>;

    for (const key of ["journey", "skillsHub", "pluginsHub"]) {
      expect(key in state).toBe(false);
    }
  });

  it("no source requests an RPC the gateway lacks", () => {
    const dead = [
      "skills.manage",
      "plugins.manage",
      "learning.frames",
      "learning.detail",
      "learning.edit",
      "learning.delete",
    ];
    const hits = sources(SRC).flatMap((file) => {
      const text = readFileSync(file, "utf8");

      return dead
        .filter((m) => text.includes(`"${m}"`))
        .map((m) => `${file}: ${m}`);
    });

    expect(hits).toEqual([]);
  });
});
