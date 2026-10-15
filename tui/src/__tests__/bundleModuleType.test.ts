/**
 * The built TUI declares itself an ES module next to its own files (#164).
 *
 * The installer copies only `tui/dist/` (and the release tarball ships only
 * `dist/`), so an installed `dist/entry.js` had no `package.json` with
 * `"type": "module"` above it. Node then took the 1.3 MB bundle for CommonJS,
 * hit its `import` syntax and parsed it a second time as ESM: the start-up
 * peak was about 100 MB instead of 88 MB (and Node 20 without module-syntax
 * detection cannot start it at all). `dist/package.json` travels with the
 * bundle.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, expect, it } from "vitest";

const tuiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const dist = resolve(tuiRoot, "dist");
const buildScript = resolve(tuiRoot, "scripts/build/tui.mjs");

beforeAll(() => {
  const entry = resolve(dist, "entry.js");
  const stale =
    !existsSync(entry) ||
    statSync(entry).mtimeMs < statSync(buildScript).mtimeMs;

  if (stale) {
    execFileSync(process.execPath, [buildScript], {
      cwd: tuiRoot,
      stdio: ["ignore", "ignore", "inherit"],
      timeout: 120_000,
    });
  }
}, 180_000);

it("ships a package.json that makes dist/*.js ES modules", () => {
  const pkg = resolve(dist, "package.json");

  expect(existsSync(pkg), "dist/package.json is missing").toBe(true);
  expect(JSON.parse(readFileSync(pkg, "utf8"))).toEqual({ type: "module" });
});
