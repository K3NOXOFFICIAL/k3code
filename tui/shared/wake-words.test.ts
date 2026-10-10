import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { detectWakeWord, WAKE_WORDS } from "./wake-words";

interface Case {
  input: string;
  mode: null | string;
  task: null | string;
}

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");

// The table the Python detector is tested against (core/tests/test_wakewords.py): one definition of "a mention".
const CASES: Case[] = JSON.parse(
  readFileSync(
    resolve(repoRoot, "core/tests/data/wake_word_cases.json"),
    "utf8",
  ),
);

// The word list of the Python side, when this checkout can import it (PYTHON=<core venv python>), like slashParity.
const loadPythonWakeWords = (): { error?: string; words: string[] } => {
  try {
    const out = execFileSync(
      process.env.PYTHON ?? "python3",
      [
        "-c",
        "import json; from k3code.wakewords import WAKE_WORDS; print(json.dumps(sorted(WAKE_WORDS.items())))",
      ],
      { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    );

    return {
      words: (JSON.parse(out) as [string, string][]).map(
        ([word, command]) => `${word}=${command}`,
      ),
    };
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : String(error),
      words: [],
    };
  }
};

const python = loadPythonWakeWords();
const pythonIt = python.error ? it.skip : it;

describe("detectWakeWord against the shared table", () => {
  it("loads the table", () => {
    expect(CASES.length).toBeGreaterThan(40);
  });

  it.each(CASES.map((c) => [c.input.slice(0, 40) || "<empty>", c] as const))(
    "%s",
    (_name, c) => {
      const got = detectWakeWord(c.input);

      expect(got?.mode ?? null).toBe(c.mode);
      expect(got?.task ?? null).toBe(c.task);
    },
  );

  it("covers every mode and a miss", () => {
    const modes = new Set(CASES.map((c) => c.mode));

    for (const word of Object.keys(WAKE_WORDS)) {
      expect(modes.has(word), `no case for ${word}`).toBe(true);
    }

    expect(modes.has(null)).toBe(true);
  });
});

describe("detectWakeWord offsets", () => {
  it("points at the word it found", () => {
    const text = "please fix the build, Ultracode";
    const got = detectWakeWord(text);

    expect(got).not.toBeNull();
    expect(text.slice(got!.start, got!.end)).toBe("Ultracode");
    expect(got!.mode).toBe("ultracode");
  });

  it("does not let the long s fold onto s (no unicode case folding)", () => {
    expect(detectWakeWord("ultraresearch notes")?.mode).toBe("ultraresearch");
    expect(detectWakeWord("ultrareſearch notes")).toBeNull();
  });
});

describe("wake word list parity with the Python core", () => {
  if (python.error) {
    it.skip(`Python k3code unavailable: ${python.error.split("\n")[0]}`, () => {});
  }

  pythonIt("names the same words and commands as k3code.wakewords", () => {
    const ours = Object.entries(WAKE_WORDS)
      .map(([word, command]) => `${word}=${command}`)
      .sort();

    expect(ours).toEqual(python.words);
  });
});
