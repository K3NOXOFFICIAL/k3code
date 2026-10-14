import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

// The TUI is a fork of Hermes, and its env knobs were renamed HERMES_* ->
// K3CODE_*. A knob read only under the old name is invisible to anyone who
// follows the k3code docs (K3CODE_TUI_FORCE_OSC52 was suggested by /copy and
// ignored). Every HERMES_<X> in shipped code must therefore sit next to its
// K3CODE_<X> counterpart in the same file, read first with HERMES_ as the
// fallback.
const TUI_ROOT = join(import.meta.dirname, "..", "..");
const SCAN = ["src", join("packages", "hermes-ink", "src"), "scripts"];
const SOURCE = /\.(?:[cm]?[jt]sx?)$/;
const TEST = /(?:\.test\.[cm]?[jt]sx?|[\\/]__tests__[\\/])/;

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === "dist") {
      continue;
    }

    const full = join(dir, name);

    if (statSync(full).isDirectory()) {
      walk(full, out);
    } else if (SOURCE.test(name) && !TEST.test(full)) {
      out.push(full);
    }
  }

  return out;
}

describe("HERMES_* env reads", () => {
  it("all have a K3CODE_ counterpart in the same file", () => {
    const missing: string[] = [];

    for (const root of SCAN) {
      for (const file of walk(join(TUI_ROOT, root))) {
        const text = readFileSync(file, "utf8");

        for (const name of new Set(text.match(/\bHERMES_[A-Z0-9_]+/g) ?? [])) {
          const k3 = name.replace(/^HERMES_/, "K3CODE_");

          if (!new RegExp(`\\b${k3}\\b`).test(text)) {
            missing.push(
              `${relative(TUI_ROOT, file).split(sep).join("/")}: ${name} -> ${k3}`,
            );
          }
        }
      }
    }

    expect(missing).toEqual([]);
  });
});
