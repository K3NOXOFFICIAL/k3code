/**
 * Wake words: a user prompt that names an ultra mode (`ultracode`, `ultraplan`, `ultraresearch`) runs that mode.
 *
 * TypeScript port of `k3code.wakewords.detect` (core/src/k3code/wakewords.py), which stays the source of truth: the
 * gateway decides what actually runs, this copy only lets the composer show what will happen. Both are tested against
 * core/tests/data/wake_word_cases.json, so a change to either must change the table and the other copy.
 *
 * A mention counts only when it is unambiguous:
 *  - the prompt is not a slash command (`/ultracode fix x` already runs the command itself);
 *  - the word stands alone: not part of a longer word, a path (`src/ultracode.py`), a flag (`--ultracode`), an
 *    `@mention` or a `#tag`;
 *  - it is not quoted or in code: a fenced block, an inline code span, or wrapped in quotes or backticks;
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
// copy (re.ASCII) does not.
const WORD = new RegExp(
  `(?<![A-Za-z0-9_/.@#$:\\\\-])(${Object.keys(WAKE_WORDS).join("|")})(?![A-Za-z0-9_/\\\\-])(?!\\.[A-Za-z0-9_])`,
  "gi",
);

const FENCE = /```[\s\S]*?(?:```|$)/g;
const INLINE_CODE = /`[^`\n]*`/g;
const QUOTES = "`\"'“”‘’„‚«»‹›「」『』";
const LEAD_PUNCT = /^[ \t]*[:,;–—-](?=\s|$)/;
const TRAIL_PUNCT = /[,;:–—-]+$/;
const GLUE_AFTER = ",.;:!?)";
// Nothing to do but punctuation: the word stood alone.
const ONLY_PUNCT = /^[\s!-/:-@[-`{-~]*$/;

type Span = readonly [number, number];

const maskedSpans = (text: string): Span[] => {
  const spans: Span[] = [...text.matchAll(FENCE)].map(
    (m) => [m.index, m.index + m[0].length] as const,
  );

  for (const m of text.matchAll(INLINE_CODE)) {
    if (!spans.some(([a, b]) => a <= m.index && m.index < b)) {
      spans.push([m.index, m.index + m[0].length]);
    }
  }

  return spans;
};

const taskWithout = (text: string, start: number, end: number): string => {
  const before = text.slice(0, start);
  const after = text.slice(end);
  let task: string;

  if (!before.trim()) {
    task = after.replace(LEAD_PUNCT, ""); // "ultracode: fix it" -> "fix it"
  } else if (!after.trim()) {
    task = before.trimEnd().replace(TRAIL_PUNCT, "");
  } else {
    const joined = before.replace(/[ \t]+$/, "");
    const glued = joined.endsWith("\n") || GLUE_AFTER.includes(after[0]!);

    task = joined + (glued ? "" : " ") + after.replace(/^[ \t]+/, "");
  }

  task = task.trim();

  return ONLY_PUNCT.test(task) ? "" : task;
};

/** The wake word in `text` and the task that is left, or null (see the module comment for the rules). */
export function detectWakeWord(text: string): null | WakeMatch {
  if (text.trimStart().startsWith("/")) {
    return null;
  }

  const masked = maskedSpans(text);
  const found: { end: number; start: number; word: string }[] = [];

  for (const m of text.matchAll(WORD)) {
    const start = m.index;
    const end = start + m[0].length;

    if (masked.some(([a, b]) => a <= start && start < b)) {
      continue;
    }

    if (
      (start > 0 && QUOTES.includes(text[start - 1]!)) ||
      (end < text.length && QUOTES.includes(text[end]!))
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
