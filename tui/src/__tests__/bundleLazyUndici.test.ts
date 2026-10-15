/**
 * undici is not parsed at start-up (#164).
 *
 * The TUI loads undici only for a WebSocket on Node 20 (Node 22+ has a
 * global one), through a dynamic `import("undici")`. Bundled into the single
 * `dist/entry.js` it was still parsed on every start: ~415 KB, about 30% of
 * the bundle, and about 8 MB of the start-up peak. The build splits code that
 * only an `import()` reaches into `dist/chunks/`, so undici's file is read
 * only when that import runs.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, expect, it } from "vitest";

const tuiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const dist = resolve(tuiRoot, "dist");
const buildScript = resolve(tuiRoot, "scripts/build/tui.mjs");

const jsFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory()
      ? jsFiles(join(dir, e.name))
      : e.name.endsWith(".js")
        ? [join(dir, e.name)]
        : [],
  );

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

it("keeps undici in its own file that only a dynamic import loads", () => {
  const files = jsFiles(dist);
  const holders = files.filter((f) =>
    readFileSync(f, "utf8").includes("UndiciError"),
  );

  expect(holders.map((f) => basename(f))).toEqual([
    expect.stringMatching(/^undici-[\w-]+\.js$/),
  ]);

  const name = basename(holders[0]!);

  for (const file of files) {
    const src = readFileSync(file, "utf8");
    const refs = src.split(name).length - 1;
    const dynamic =
      src.match(new RegExp(`import\\(\\s*"[^"]*${name}"\\s*\\)`, "g")) ?? [];

    expect(refs, `${basename(file)} imports ${name} statically`).toBe(
      dynamic.length,
    );
  }
});
