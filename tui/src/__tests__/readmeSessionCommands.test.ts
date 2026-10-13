import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { sessionCommands } from "../app/slash/commands/session.js";

const README = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../README.md",
);

// The `/name` tokens in the command list under the README's
// "### Session (`session.ts`)" heading: its first paragraph. Later paragraphs
// may name gateway commands the TUI does not handle.
const readmeSessionCommands = () => {
  const text = readFileSync(README, "utf8");
  const list = /^### Session \(`session\.ts`\)\n\n([\s\S]*?)\n\n/m.exec(text);

  if (!list) {
    throw new Error("tui/README.md has no '### Session (`session.ts`)' list");
  }

  return [...list[1]!.matchAll(/`\/([a-z][\w-]*)/g)].map((m) => m[1]!);
};

describe("tui/README.md Session command list", () => {
  it("names exactly the commands session.ts registers, aliases included", () => {
    const registered = sessionCommands
      .flatMap((cmd) => [cmd.name, ...(cmd.aliases ?? [])])
      .sort();

    expect([...new Set(readmeSessionCommands())].sort()).toEqual(registered);
  });
});
