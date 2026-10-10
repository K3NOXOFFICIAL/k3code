import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  detectEnabledWakeWord,
  detectWakeWord,
  WAKE_CONFIG_KEYS,
  WAKE_WORDS,
  wakeConfig,
} from "./wake-words";

interface Case {
  input: string;
  mode: null | string;
  skip?: [number, number][];
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
const loadPythonWakeWords = (): {
  configKeys: string[];
  error?: string;
  words: string[];
} => {
  try {
    const out = execFileSync(
      process.env.PYTHON ?? "python3",
      [
        "-c",
        "import json; from k3code.wakewords import CONFIG_KEYS, WAKE_WORDS; print(json.dumps([sorted(WAKE_WORDS.items()), list(CONFIG_KEYS)]))",
      ],
      { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
    );

    const [words, configKeys] = JSON.parse(out) as [
      [string, string][],
      string[],
    ];

    return {
      configKeys,
      words: words.map(([word, command]) => `${word}=${command}`),
    };
  } catch (error) {
    return {
      configKeys: [],
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
      const got = detectWakeWord(c.input, c.skip);

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

describe("detectWakeWord cost", () => {
  // Python has the same test (test_wakewords.py): many words, code spans and pastes stay fast, no per-word rescan.
  const timed = (text: string, skip: [number, number][] = []) => {
    const t0 = performance.now();
    const got = detectWakeWord(text, skip);

    return { got, took: performance.now() - t0 };
  };

  it("is not quadratic in code spans, fences, indented blocks or pastes", () => {
    for (const text of [
      "`ultracode` ".repeat(20000) + "ultracode",
      "```a``` ".repeat(20000) + "ultracode",
      "x\n~~~\n~~~\n".repeat(20000) + "ultracode",
      "x\n\n    y\n\n".repeat(20000) + "\n \n".repeat(20000) + "ultracode",
      "x" + " \n".repeat(40000) + "y ultracode", // long whitespace runs that do not end the text
      "x" + " ".repeat(80000) + "y ultracode" + " ".repeat(80000) + "z",
    ]) {
      const { got, took } = timed(text);

      expect(got?.mode).toBe("ultracode");
      expect(took).toBeLessThan(2000);
    }

    const pastes: [number, number][] = [];

    for (let i = 0; i < 60000; i += 3) {
      pastes.push([i, i + 2]);
    }

    const { got, took } = timed("ab ".repeat(20000) + "ultracode", pastes);

    expect(got?.start).toBe(60000);
    expect(took).toBeLessThan(2000);
  });

  it("reads a glued neighbour outside the BMP as one character", () => {
    expect(detectWakeWord("\u{1D49C}ultracode go")).toBeNull(); // a mathematical script capital A
    expect(detectWakeWord("\u{1F600}ultracode go")?.task).toBe("\u{1F600} go"); // an emoji is no letter
  });
});

describe("wakeConfig and detectEnabledWakeWord (wake_cfg / detect_enabled)", () => {
  it("defaults everything on and keeps only booleans", () => {
    expect(wakeConfig(undefined)).toEqual(
      Object.fromEntries(WAKE_CONFIG_KEYS.map((key) => [key, true])),
    );
    expect(wakeConfig({ enabled: 0, extra: false, ultracode: false })).toEqual({
      ...wakeConfig(null),
      ultracode: false,
    });
  });

  it("finds nothing when switched off, or when the mode found is", () => {
    expect(detectEnabledWakeWord(wakeConfig({}), "ultracode it")?.mode).toBe(
      "ultracode",
    );
    expect(
      detectEnabledWakeWord(wakeConfig({ enabled: false }), "ultracode it"),
    ).toBeNull();
    expect(
      detectEnabledWakeWord(wakeConfig({ ultracode: false }), "ultracode it"),
    ).toBeNull();
    expect(
      detectEnabledWakeWord(wakeConfig({}), "ultracode it", [[0, 9]]),
    ).toBeNull();
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

  pythonIt("names the same wake_words config keys as CONFIG_KEYS", () => {
    expect([...WAKE_CONFIG_KEYS]).toEqual(python.configKeys);
  });
});
