import {
  detectEnabledWakeWord,
  type WakeConfig,
  wakeConfig,
} from "@k3code/shared/wake-words";

import { PASTE_SNIPPET_RE } from "../protocol/paste.js";

/**
 * Reference spans in composer text: a `/skill` invoked or named in prose, an
 * `@file:` / `@url:` / `@session:` ref, and a `[[ Image 1 ]]` / paste token.
 * The same vocabulary the desktop chips, so the two surfaces agree on what a
 * reference is. One more span is not a reference but a promise: an unambiguous
 * wake word (`ultracode`, `ultraplan`, `ultraresearch`) that will run that
 * mode once when the prompt is sent.
 *
 * Concatenating every `text` reproduces the input exactly — styling only, the
 * text is never rewritten. Regexes are built per call: a shared `/g` instance
 * carries `lastIndex` between callers and silently skips the first match in
 * the next string it is handed.
 */
export type ComposerHighlight = { ref: boolean; text: string };

/**
 * What the wake word accent needs beyond the text itself.
 *
 * `before` / `after`: the composer text above and below this line, newlines
 * included. The prompt that is sent is all lines joined, so a wake word is
 * judged on all of it (an `ultraplan` up there makes an `ultracode` here a
 * second mode) but only the part in `text` is painted.
 *
 * `wake`: the `wake_words` config. A mode (or all of them) switched off is
 * never accented, since the gateway will not run it.
 */
export interface HighlightContext {
  after?: string;
  before?: string;
  wake?: WakeConfig;
}

const DEFAULT_WAKE = wakeConfig(undefined);

// Leading OR mid-prose. `(?![\w-]*\/)` keeps `/usr/local` from lighting up as
// `/usr`, and requiring a letter keeps `a 3 /4 b` plain. A BARE `/` counts only
// at the very end — that is the user opening the command menu, not prose.
const slashRe = () => /(?<=^|\s)(?:\/[a-zA-Z][\w-]*(?![\w-]*\/)|\/$)/g;

// Every `@ref` shape the composer accepts: a typed kind (`@file:src/a.ts`), a
// quoted value with spaces, a bare `@diff` / `@staged`, and the half-typed
// `@fi` the user is still working on. Quoted alternatives come before bare
// `\S+` or a quoted value would end at its first space.
const atRe = () =>
  /(?<=^|\s)@(?:[\w-]+:(?:`[^`\n]*`?|"[^"\n]*"?|'[^'\n]*'?|\S*)|\S*)/g;

const tokenRe = () => new RegExp(PASTE_SNIPPET_RE.source, "g");

type Span = { end: number; start: number };

const matchSpans = (text: string, re: RegExp): Span[] =>
  [...text.matchAll(re)]
    .filter((m) => m[0])
    .map((m) => ({ end: (m.index ?? 0) + m[0].length, start: m.index ?? 0 }));

// The one wake word the gateway will act on (not quoted, not in a path or code,
// not a second mode in the same prompt, not in a paste), so the accent never
// promises a run the gateway would skip. A `[[ … ]]` label stands for a paste
// the gateway is told to skip, so a word in the label's preview is skipped too.
const wakeHit = (whole: string, wake?: WakeConfig): null | Span => {
  const hit = detectEnabledWakeWord(
    wake ?? DEFAULT_WAKE,
    whole,
    matchSpans(whole, tokenRe()).map((span) => [span.start, span.end]),
  );

  return hit && { end: hit.end, start: hit.start };
};

// The wake span inside `[from, from + length)` of the whole prompt, relative to
// that line.
const within = (hit: null | Span, from: number, length: number): Span[] =>
  hit && hit.start >= from && hit.end <= from + length
    ? [{ end: hit.end - from, start: hit.start - from }]
    : [];

export const splitComposerHighlights = (
  text: string,
  ctx: HighlightContext = {},
): ComposerHighlight[] => {
  const before = ctx.before ?? "";

  return split(
    text,
    within(
      wakeHit(before + text + (ctx.after ?? ""), ctx.wake),
      before.length,
      text.length,
    ),
  );
};

const split = (text: string, wake: Span[]): ComposerHighlight[] => {
  // Tokens, then @refs, then slashes, then the wake word: on an overlap the
  // earlier kind wins, so a slash inside a quoted ref value stays part of that
  // ref.
  const tokens = matchSpans(text, tokenRe());
  const spans = [
    ...tokens,
    ...matchSpans(text, atRe()),
    ...matchSpans(text, slashRe()),
    ...wake,
  ]
    .sort((a, b) => a.start - b.start)
    .reduce<Span[]>((kept, span) => {
      if (
        !kept.some((prev) => span.start < prev.end && span.end > prev.start)
      ) {
        kept.push(span);
      }

      return kept;
    }, []);

  const out: ComposerHighlight[] = [];
  let last = 0;

  for (const span of spans) {
    if (span.start > last) {
      out.push({ ref: false, text: text.slice(last, span.start) });
    }

    out.push({ ref: true, text: text.slice(span.start, span.end) });
    last = span.end;
  }

  if (last < text.length || !out.length) {
    out.push({ ref: false, text: text.slice(last) });
  }

  return out;
};

/** Per-character "is this cell accented", indexed to match the input string. */
export const highlightMask = (
  text: string,
  ctx: HighlightContext = {},
): boolean[] => {
  const mask = new Array<boolean>(text.length).fill(false);
  let offset = 0;

  for (const segment of splitComposerHighlights(text, ctx)) {
    if (segment.ref) {
      mask.fill(true, offset, offset + segment.text.length);
    }

    offset += segment.text.length;
  }

  return mask;
};

/**
 * Whether every character that stays on screen keeps the color it had.
 *
 * The fast-echo bypass writes ONLY the new cells, so it may run only when a
 * keystroke leaves the existing ones alone. Typing `]` to close a
 * `[[ token ]]`, or a second `/` demoting `/usr` to a path, recolors text
 * already painted — those have to go through a full Ink repaint instead.
 */
export const highlightsStable = (
  prev: string,
  next: string,
  ctx: HighlightContext = {},
): boolean => {
  const before = highlightMask(prev, ctx);
  const after = highlightMask(next, ctx);

  return before
    .slice(0, Math.min(before.length, after.length))
    .every((on, i) => on === after[i]);
};

/**
 * Highlights for every composer line at once, as one prompt: the lines above
 * the cursor line render outside the text input, and still have their wake
 * word judged (and painted) against the whole prompt. Detection runs once.
 */
export const composerLineHighlights = (
  lines: readonly string[],
  wake?: WakeConfig,
): ComposerHighlight[][] => {
  const hit = wakeHit(lines.join("\n"), wake);
  let from = 0;

  return lines.map((line) => {
    const out = split(line, within(hit, from, line.length));

    from += line.length + 1;

    return out;
  });
};
