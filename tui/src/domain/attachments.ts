import type { ComposerToken } from "../app/interfaces.js";
import { PASTE_SNIPPET_RE } from "../protocol/paste.js";

/**
 * Composer tokens are the ONE way deferred content shows up in the input line:
 * a collapsed paste and an attached image both render as `[[ … ]]` sitting in
 * the text the user is editing. They are ordinary characters — arrow keys,
 * backspace, and selection work on them for free — and they carry their real
 * payload out-of-band until submit.
 *
 * Two consequences the rest of the composer relies on:
 *   - Deleting the token is how you drop the thing. Nothing else to click.
 *   - Position in the text is meaningful: the model sees the payload where the
 *     token sat, not stapled to the front of the turn.
 */
export const imageToken = (index: number) => `[[ Image ${index} ]]`;

/** Highest image token index handed out so far, so a new one never collides. */
export const nextImageIndex = (tokens: ComposerToken[]) =>
  tokens.reduce(
    (max, t) => (t.kind === "image" ? Math.max(max, t.index) : max),
    0,
  ) + 1;

/** Tokens whose label is no longer anywhere in the composer text. */
export const droppedTokens = (tokens: ComposerToken[], value: string) => {
  const live = new Set(value.match(PASTE_SNIPPET_RE) ?? []);

  return tokens.filter((t) => !live.has(t.label));
};

/** Expanded composer text and where its pastes went (`[start, end)` UTF-16 offsets into `text`). */
export interface Expanded {
  pasteSpans: [number, number][];
  text: string;
}

/**
 * Resolve every token in `value` to what the agent should actually receive.
 *
 * Repeated identical labels expand in submission order (left to right), which
 * is why this walks matches instead of doing a global replace per token.
 *
 * An image token expands to nothing: the gateway already holds the file in
 * `session.attached_images` and splices the real vision content in at submit.
 * The token's job was to show the user where it landed, so it also eats one
 * adjacent space to avoid leaving a gap in the middle of a sentence.
 */
export const expandTokensWithSpans = (tokens: ComposerToken[]) => {
  const byLabel = new Map<string, ComposerToken[]>();

  for (const token of tokens) {
    const hit = byLabel.get(token.label);
    if (hit) {
      hit.push(token);
    } else {
      byLabel.set(token.label, [token]);
    }
  }

  return (value: string): Expanded => {
    const spans: [number, number][] = [];
    let out = "";
    let pos = 0;

    for (const m of value.matchAll(
      new RegExp(`[ \\t]?(?:${PASTE_SNIPPET_RE.source})`, "g"),
    )) {
      const match = m[0];
      const token = byLabel.get(match.trimStart())?.shift();

      out += value.slice(pos, m.index);
      pos = m.index + match.length;

      if (!token) {
        out += match;
      } else if (token.kind === "paste") {
        out += match.slice(0, match.length - token.label.length);
        spans.push([out.length, out.length + token.text.length]);
        out += token.text;
      }
    }

    out += value.slice(pos);

    const text = out.trim();
    const lead = out.length - out.trimStart().length;
    const clamp = (n: number) => Math.min(text.length, Math.max(0, n - lead));

    return {
      pasteSpans: spans
        .map(([a, b]): [number, number] => [clamp(a), clamp(b)])
        .filter(([a, b]) => a < b),
      text,
    };
  };
};

export const expandTokens = (tokens: ComposerToken[]) => {
  const expand = expandTokensWithSpans(tokens);

  return (value: string) => expand(value).text;
};

/**
 * `spans` (UTF-16 offsets into `text`) as code-point offsets, which is what the gateway (Python) counts in: a paste
 * span in a prompt with an emoji before it would otherwise land one character late.
 */
export const codePointSpans = (
  text: string,
  spans: readonly (readonly [number, number])[],
): [number, number][] => {
  if (!spans.length) {
    return [];
  }

  // one walk over the text for all offsets, however many spans there are
  const points = new Map<number, number>();
  let unit = 0;
  let count = 0;

  for (const want of [...new Set(spans.flat())].sort((x, y) => x - y)) {
    while (unit < want) {
      const c = text.charCodeAt(unit);
      const pair =
        c >= 0xd800 &&
        c <= 0xdbff &&
        unit + 1 < want &&
        (text.charCodeAt(unit + 1) & 0xfc00) === 0xdc00;

      unit += pair ? 2 : 1;
      count += 1;
    }

    points.set(want, count);
  }

  const at = (n: number) => points.get(n) ?? 0;

  return spans.map(([a, b]) => [at(a), at(b)]);
};
