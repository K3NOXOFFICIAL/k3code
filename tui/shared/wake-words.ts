/**
 * Wake words: a user prompt that names an ultra mode (`ultracode`, `ultraplan`, `ultraresearch`) runs that mode.
 *
 * TypeScript port of `k3code.wakewords.detect` (core/src/k3code/wakewords.py), which stays the source of truth: the
 * gateway decides what actually runs, this copy only lets the composer show what will happen. Both are tested against
 * core/tests/data/wake_word_cases.json, so a change to either must change the table and the other copy.
 *
 * A mention counts only when it is unambiguous:
 *  - the prompt is not a slash command (`/ultracode fix x` already runs the command itself);
 *  - the word stands alone: not part of a longer word (in any script), a path (`src/ultracode.py`), a flag
 *    (`--ultracode`), an `@mention` or a `#tag`;
 *  - it is not quoted or in code: a fenced (``` or ~~~) or indented block, an inline code span, or wrapped in quotes
 *    or backticks, and it is not in pasted text;
 *  - the prompt does not name two different modes.
 */

/** wake word (lower case) -> the slash command it runs. Mirrors `WAKE_WORDS` in wakewords.py. */
export const WAKE_WORDS: Readonly<Record<string, string>> = {
  ultracode: "ultracode",
  ultraplan: "ultraplan",
  ultraresearch: "ultraresearch",
};

export interface WakeMatch {
  /** Offset just past the word in the prompt. */
  end: number;
  /** Lower-case wake word, which is also the name of the command it runs. */
  mode: string;
  /** Offset of the word in the prompt. */
  start: number;
  /** The prompt without the word (may be empty: the word alone). */
  task: string;
}

// ASCII classes and no `u` flag on purpose: with `u` the case folding would map the long s onto "s", which the Python
// copy (re.ASCII) does not. A letter, digit or combining mark of any script next to the word is checked separately
// (`glued`), with Unicode properties that both copies read the same way.
const WORD = new RegExp(
  `(?<![A-Za-z0-9_/.@#$:\\\\-])(${Object.keys(WAKE_WORDS).join("|")})(?![A-Za-z0-9_/\\\\-])(?!\\.[A-Za-z0-9_])`,
  "gi",
);
const GLUED_BEFORE = /[\p{L}\p{N}\p{M}]$/u;
const GLUED_AFTER = /^[\p{L}\p{N}\p{M}]/u;

// Whitespace, spelled out so both copies agree (`_WS_CLASS` in wakewords.py): Python's str.isspace() set plus U+FEFF.
// JavaScript's \s and trim() leave out U+001C-U+001F and U+0085, Python's leave out U+FEFF.
const WS =
  "\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff";
const WS_ONE = new RegExp(`^[${WS}]$`);
const isWs = (c: string): boolean => WS_ONE.test(c);
const isBlank = (c: string): boolean => c === " " || c === "\t";
// Loops, not /[...]+$/: a regex anchored at the end retries from every character of a long run that is not at the end
// (quadratic), where Python's rstrip is linear.
const stripStart = (s: string, drop: (c: string) => boolean): string => {
  let i = 0;

  while (i < s.length && drop(s[i]!)) {
    i += 1;
  }

  return s.slice(i);
};
const stripEnd = (s: string, drop: (c: string) => boolean): string => {
  let i = s.length;

  while (i > 0 && drop(s[i - 1]!)) {
    i -= 1;
  }

  return s.slice(0, i);
};
const strip = (s: string): string => stripEnd(stripStart(s, isWs), isWs);
// End of text, written the same in both copies.
const END = "(?![\\s\\S])";
// ``` fences anywhere, and ~~~ fences that open a line (an unclosed fence runs to the end).
const FENCE = new RegExp(
  `\`\`\`[\\s\\S]*?(?:\`\`\`|${END})|(?<![^\\n])[ ]{0,3}~~~[\\s\\S]*?(?:\\n[ ]{0,3}~~~|${END})`,
  "g",
);
// Indented code: lines indented by four spaces or a tab, after a blank line or at the start (as in CommonMark, an
// indented line does not interrupt a paragraph); group 1 is the block, which ends the match.
const INDENT = "(?:[ ]{0,3}\\t|[ ]{4})";
const INDENTED = new RegExp(
  `(?:^|\\n[ \\t]*\\n)((?:${INDENT}[^\\n]*(?:\\n|${END})|[ \\t]*\\n(?=${INDENT}))+)`,
  "g",
);
const INLINE_CODE = /`[^`\n]*`/g;
const QUOTES = "`\"'“”‘’„‚«»‹›「」『』";
// Brackets and emphasis wrapped straight around the word go with it: "(ultracode) fix it" -> "fix it".
const WRAPPERS: Readonly<Record<string, string>> = {
  "(": ")",
  "*": "*",
  "<": ">",
  "[": "]",
  "{": "}",
  "~": "~",
};
const LEAD_PUNCT = new RegExp(`^[ \\t]*[:,;–—-](?=[${WS}]|${END})`);
const TRAIL_PUNCT = ",;:-–—";
const GLUE_AFTER = ",.;:!?)";
// Nothing to do but punctuation: the word stood alone.
const ONLY_PUNCT = new RegExp(`^[${WS}!-/:-@[-\`{-~]*$`);

export type Span = readonly [number, number];

const merge = (spans: Iterable<Span>): Span[] => {
  const out: [number, number][] = [];

  for (const [a, b] of [...spans].sort((x, y) => x[0] - y[0] || x[1] - y[1])) {
    const last = out.at(-1);

    if (last && a <= last[1]) {
      last[1] = Math.max(last[1], b);
    } else if (a < b) {
      out.push([a, b]);
    }
  }

  return out;
};

/** Sorted, non-overlapping spans and a logarithmic "is this offset in one of them" test. */
const inSpans = (spans: readonly Span[], pos: number): boolean => {
  let lo = 0;
  let hi = spans.length;

  while (lo < hi) {
    const mid = (lo + hi) >> 1;

    if (spans[mid]![0] <= pos) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }

  return lo > 0 && pos < spans[lo - 1]![1];
};

/**
 * Everything a wake word does not count in: `skip` (pasted text), fenced and indented code blocks, and the inline code
 * spans that do not start inside one of those. Pasted text is blanked out before the code is looked for, so a backtick
 * in a paste never opens a fence over what was typed after it.
 */
const maskedSpans = (text: string, skip: readonly Span[]): Span[] => {
  let scan = text;

  if (skip.length) {
    let pos = 0;
    const parts: string[] = [];

    for (const [a, b] of skip) {
      parts.push(text.slice(pos, a), "\0".repeat(b - a));
      pos = b;
    }

    scan = parts.join("") + text.slice(pos);
  }

  const blocks = merge([
    ...skip,
    ...[...scan.matchAll(FENCE)].map(
      (m) => [m.index, m.index + m[0].length] as const,
    ),
    ...[...scan.matchAll(INDENTED)].map(
      (m) =>
        [m.index + m[0].length - m[1]!.length, m.index + m[0].length] as const,
    ),
  ]);
  const inline = [...scan.matchAll(INLINE_CODE)]
    .filter((m) => !inSpans(blocks, m.index))
    .map((m) => [m.index, m.index + m[0].length] as const);

  return merge([...blocks, ...inline]);
};

const taskWithout = (text: string, from: number, to: number): string => {
  let start = from;
  let end = to;

  while (
    start > 0 &&
    end < text.length &&
    WRAPPERS[text[start - 1]!] === text[end]
  ) {
    start -= 1;
    end += 1;
  }

  const before = text.slice(0, start);
  const after = text.slice(end);
  let task: string;

  if (!strip(before)) {
    task = after.replace(LEAD_PUNCT, ""); // "ultracode: fix it" -> "fix it"
  } else if (ONLY_PUNCT.test(after)) {
    // "fix it, ultracode." -> "fix it."
    const head = stripEnd(
      stripEnd(stripEnd(before, isWs), (c) => TRAIL_PUNCT.includes(c)),
      isWs,
    );

    task = head + strip(after);
  } else {
    const joined = stripEnd(before, isBlank);
    let rest = stripStart(after, isBlank);

    if (joined.endsWith("\n") || TRAIL_PUNCT.includes(joined.at(-1)!)) {
      rest = stripStart(rest.replace(LEAD_PUNCT, ""), isBlank); // "Hey, ultracode: fix it" -> "Hey, fix it"
    }

    const glue =
      joined.endsWith("\n") ||
      "([{<".includes(joined.at(-1)!) ||
      GLUE_AFTER.includes(rest[0] ?? "\0");

    task = joined + (glue ? "" : " ") + rest;
  }

  task = strip(task);

  return ONLY_PUNCT.test(task) ? "" : task;
};

/**
 * The wake word in `text` and the task that is left, or null (see the module comment for the rules).
 *
 * `skip` holds `[start, end)` offsets of pasted text: a wake word in a paste (a log, a file) is not the user asking for
 * a mode.
 */
export function detectWakeWord(
  text: string,
  skip: Iterable<Span> = [],
): null | WakeMatch {
  if (strip(text).startsWith("/")) {
    return null;
  }

  const words = [...text.matchAll(WORD)];

  if (!words.length) {
    return null; // nearly every prompt: no need to look for code
  }

  const clamped = [...skip].map(
    ([a, b]) => [Math.max(0, a), Math.min(text.length, b)] as const,
  );
  const masked = maskedSpans(text, merge(clamped));
  const found: { end: number; start: number; word: string }[] = [];

  for (const m of words) {
    const start = m.index;
    const end = start + m[0].length;

    if (inSpans(masked, start)) {
      continue;
    }

    if (
      (start > 0 && QUOTES.includes(text[start - 1]!)) ||
      (end < text.length && QUOTES.includes(text[end]!))
    ) {
      continue;
    }

    if (
      GLUED_BEFORE.test(text.slice(Math.max(0, start - 2), start)) ||
      GLUED_AFTER.test(text.slice(end, end + 2))
    ) {
      continue;
    }

    found.push({ end, start, word: m[1]!.toLowerCase() });
  }

  const first = found[0];

  if (!first || new Set(found.map((f) => f.word)).size > 1) {
    return null;
  }

  return {
    end: first.end,
    mode: first.word,
    start: first.start,
    task: taskWithout(text, first.start, first.end),
  };
}

/** The `wake_words` config keys: `enabled` plus one per mode. Mirrors `CONFIG_KEYS` in wakewords.py. */
export const WAKE_CONFIG_KEYS: readonly string[] = [
  "enabled",
  ...Object.keys(WAKE_WORDS),
];

export type WakeConfig = Readonly<Record<string, boolean>>;

/** `config.wake_words` over the defaults (everything on); a value that is not a boolean keeps its default. Mirrors
 * `wake_cfg`. */
export function wakeConfig(raw: unknown): WakeConfig {
  const given =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};

  return Object.fromEntries(
    WAKE_CONFIG_KEYS.map((key) => {
      const value = given[key];

      return [key, typeof value === "boolean" ? value : true];
    }),
  );
}

/** {@link detectWakeWord}, unless the config switched wake words (or the mode it found) off. Mirrors
 * `detect_enabled`. */
export function detectEnabledWakeWord(
  config: WakeConfig,
  text: string,
  skip: Iterable<Span> = [],
): null | WakeMatch {
  if (config.enabled === false) {
    return null;
  }

  const hit = detectWakeWord(text, skip);

  return hit && config[hit.mode] !== false ? hit : null;
}
