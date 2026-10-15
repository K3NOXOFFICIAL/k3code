import { stripAnsi } from "@k3code/shared/ansi";
import { compactNumber } from "@k3code/shared/format";
import type { ToolLabel } from "@k3code/shared/gateway-events";

import {
  LIVE_RENDER_MAX_CHARS,
  LIVE_RENDER_MAX_LINES,
  THINKING_COT_MAX,
  VERBOSE_TRAIL_MAX_CHARS,
  VERBOSE_TRAIL_MAX_LINES,
} from "../config/limits.js";
import { VERBS } from "../content/verbs.js";
import type { ThinkingMode } from "../types.js";

const WS_RE = /\s+/g;

const renderEstimateLine = (line: string) => {
  const trimmed = line.trim();

  if (trimmed.startsWith("|")) {
    return trimmed
      .split("|")
      .filter(Boolean)
      .map((cell) => cell.trim())
      .join("  ");
  }

  return line
    .replace(/!\[(.*?)\]\(([^)\s]+)\)/g, "[image: $1]")
    .replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/(?<!\w)__(.+?)__(?!\w)/g, "$1")
    .replace(/\*(.+?)\*/g, "$1")
    .replace(/(?<!\w)_(.+?)_(?!\w)/g, "$1")
    .replace(/~~(.+?)~~/g, "$1")
    .replace(/==(.+?)==/g, "$1")
    .replace(/\[\^([^\]]+)\]/g, "[$1]")
    .replace(/^#{1,6}\s+/, "")
    .replace(
      /^\s*[-*+]\s+\[( |x|X)\]\s+/,
      (_m, checked: string) =>
        `• [${checked.toLowerCase() === "x" ? "x" : " "}] `,
    )
    .replace(/^\s*[-*+]\s+/, "• ")
    .replace(/^\s*(\d+)\.\s+/, "$1. ")
    .replace(/^\s*(?:>\s*)+/, "│ ");
};

export const compactPreview = (s: string, max: number) => {
  const one = s.replace(WS_RE, " ").trim();

  return !one ? "" : one.length > max ? one.slice(0, max - 1) + "…" : one;
};

export const estimateTokensRough = (text: string) =>
  !text ? 0 : (text.length + 3) >> 2;

export const edgePreview = (s: string, head = 16, tail = 28) => {
  const one = s.replace(WS_RE, " ").trim().replace(/\]\]/g, "] ]");

  return !one
    ? ""
    : one.length <= head + tail + 4
      ? one
      : `${one.slice(0, head).trimEnd()}.. ${one.slice(-tail).trimStart()}`;
};

export const pasteTokenLabel = (text: string, lineCount: number) => {
  const preview = edgePreview(text);

  if (!preview) {
    return `[[ [${compactNumber(lineCount)} lines] ]]`;
  }

  const [head = preview, tail = ""] = preview.split(".. ", 2);

  return tail
    ? `[[ ${head.trimEnd()}.. [${compactNumber(lineCount)} lines] .. ${tail.trimStart()} ]]`
    : `[[ ${preview} [${compactNumber(lineCount)} lines] ]]`;
};

const THINKING_STATUS_RE = new RegExp(`^(?:${VERBS.join("|")})\\.{0,3}$`, "i");
const THINKING_STATUS_CHUNK_RE = new RegExp(
  `[^A-Za-z\n]+\\s*(?:${VERBS.join("|")})\\.{0,3}\\s*`,
  "giu",
);

export const cleanThinkingText = (reasoning: string) =>
  reasoning
    .split("\n")
    .map((line) => line.replace(THINKING_STATUS_CHUNK_RE, "").trim())
    .filter(
      (line) =>
        line && !THINKING_STATUS_RE.test(line.replace(/\.\.\.$/, "").trim()),
    )
    .join("\n")
    .replace(/([^\n])(?=\*\*[^*\n][^\n]*?\*\*)/g, "$1\n\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

// cleanThinkingText runs several full-string regex passes (split/map/filter/join/replace).
// reasoning grows on every streamed token, so without a pre-bound this re-cleans the whole
// accumulated string on every chunk — O(n) work per token, O(n^2) over a stream. Only the
// tail is ever displayed (boundedLiveRenderText caps it further downstream), so bound the
// input here first. Headroom over LIVE_RENDER_MAX_CHARS keeps line-boundary trimming inside
// cleanThinkingText accurate even after slicing mid-line.
const THINKING_CLEAN_TAIL_BOUND = LIVE_RENDER_MAX_CHARS * 1.5;

export const thinkingPreview = (
  reasoning: string,
  mode: ThinkingMode,
  max: number = THINKING_COT_MAX,
) => {
  const bounded =
    reasoning.length > THINKING_CLEAN_TAIL_BOUND
      ? reasoning.slice(-THINKING_CLEAN_TAIL_BOUND)
      : reasoning;
  const raw = cleanThinkingText(bounded);

  return !raw || mode === "collapsed"
    ? ""
    : mode === "full"
      ? raw
      : compactPreview(raw.replace(WS_RE, " "), max);
};

export const boundedLiveRenderText = (
  text: string,
  { maxChars = LIVE_RENDER_MAX_CHARS, maxLines = LIVE_RENDER_MAX_LINES } = {},
) => boundedRenderText(text, "showing live tail", { maxChars, maxLines });

const boundedRenderText = (
  text: string,
  labelPrefix: string,
  { maxChars, maxLines }: { maxChars: number; maxLines: number },
) => {
  if (
    text.length <= maxChars &&
    text.split("\n", maxLines + 1).length <= maxLines
  ) {
    return text;
  }

  let start = 0;
  let idx = text.length;

  for (let seen = 0; seen < maxLines && idx > 0; seen++) {
    idx = text.lastIndexOf("\n", idx - 1);
    start = idx < 0 ? 0 : idx + 1;

    if (idx < 0) {
      break;
    }
  }

  const lineStart = start;
  start = Math.max(lineStart, text.length - maxChars);

  if (start > lineStart) {
    const nextBreak = text.indexOf("\n", start);

    if (nextBreak >= 0 && nextBreak < text.length - 1) {
      start = nextBreak + 1;
    }
  }

  const tail = text.slice(start).trimStart();
  const omittedLines = countNewlines(text, start);
  const omittedChars = Math.max(0, text.length - tail.length);

  const label =
    omittedLines > 0
      ? `[${labelPrefix}; omitted ${compactNumber(omittedLines)} lines / ${compactNumber(omittedChars)} chars]\n`
      : `[${labelPrefix}; omitted ${compactNumber(omittedChars)} chars]\n`;

  return `${label}${tail}`;
};

const countNewlines = (text: string, end: number) => {
  let count = 0;

  for (let i = 0; i < end; i++) {
    if (text.charCodeAt(i) === 10) {
      count++;
    }
  }

  return count;
};

export const stripTrailingPasteNewlines = (text: string) =>
  /[^\n]/.test(text) ? text.replace(/\n+$/, "") : text;

export const toolTrailLabel = (name: string) =>
  name
    .split("_")
    .filter(Boolean)
    .map((p) => p[0]!.toUpperCase() + p.slice(1))
    .join(" ") || name;

/** The argument a reader wants first, per tool: the command, the path, the pattern, the url. */
const KEY_ARGS = [
  "command",
  "path",
  "file_path",
  "pattern",
  "url",
  "query",
  "prompt",
  "description",
  "question",
] as const;

const ARG_VALUE_MAX = 48;

const argValueText = (value: unknown): string => {
  if (typeof value === "string") {
    return value;
  }

  if (
    value === null ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return String(value);
  }

  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
};

const firstLine = (text: string) => text.split("\n", 1)[0] ?? "";

// One line a reader can scan: `Bash  pwd; ls -la`, `Read  src/x.py`,
// `Glob  *.ts  in src`, `Grep  pattern  in path`, `Edit  path`. Unknown
// tools get `Name  key=value, …`. Never a dict repr, never a line break; the
// caller truncates to the column width.
export const toolCallSummary = (
  name: string,
  args: Record<string, unknown> | null | undefined,
  width = 120,
): string => {
  const label = toolTrailLabel(name);
  const rest: Record<string, unknown> = { ...(args ?? {}) };

  const take = (key: string): string => {
    const value = rest[key];
    delete rest[key];

    return value === undefined || value === "" ? "" : argValueText(value);
  };

  const parts: string[] = [];

  switch (name) {
    case "bash": {
      const command = take("command").trim();

      if (command) {
        parts.push(firstLine(command));
      }

      break;
    }

    case "read":
    case "write":
    case "edit": {
      const path = take("path") || take("file_path");

      if (path) {
        parts.push(path);
      }

      if (name === "read") {
        const offset = take("offset");
        const limit = take("limit");

        if (offset && offset !== "1") {
          parts.push(`from ${offset}${limit ? ` (${limit} lines)` : ""}`);
        }
      }

      // The payload argument of an edit/write is never worth a summary row.
      delete rest.content;
      delete rest.old_string;
      delete rest.new_string;
      delete rest.replace_all;
      delete rest.limit;
      break;
    }

    case "glob":
    case "grep": {
      const pattern = take("pattern");
      const path = take("path");

      if (pattern) {
        parts.push(pattern);
      }

      if (path && path !== ".") {
        parts.push(`in ${path}`);
      }

      delete rest.include;
      delete rest.exclude;
      break;
    }

    default: {
      for (const key of KEY_ARGS) {
        const value = take(key);

        if (value) {
          parts.push(firstLine(value));
          break;
        }
      }

      const pairs = Object.entries(rest)
        .filter(([, value]) => value !== undefined && value !== null)
        .map(
          ([key, value]) =>
            `${key}=${compactPreview(argValueText(value), ARG_VALUE_MAX)}`,
        );

      if (pairs.length) {
        parts.push(pairs.join(", "));
      }
    }
  }

  // Two spaces separate label and arguments (one space can be inside a
  // command), so the parts are collapsed one by one and the summary itself
  // is only cut, never re-collapsed.
  const summary = parts.length
    ? `${label}  ${parts
        .map((p) => p.replace(WS_RE, " ").trim())
        .filter(Boolean)
        .join("  ")}`
    : label;
  const max = Math.max(8, width);

  return summary.length > max ? `${summary.slice(0, max - 1)}…` : summary;
};

/**
 * Trail-row head for a call. `args` (the structured arguments) wins; the
 * old free-text `context` is the fallback for gateways that send only it.
 */
export const formatToolCall = (
  name: string,
  context = "",
  args?: Record<string, unknown> | null,
) => {
  if (args && Object.keys(args).length) {
    return toolCallSummary(name, args);
  }

  const label = toolTrailLabel(name);
  const preview = compactPreview(context, 64);

  // A context that is itself a dict/JSON repr (an older gateway's `str(arguments)`)
  // reads as `Glob  {'pattern': ...}` otherwise.
  const parsed = preview ? parseArgsText(context) : null;

  if (parsed) {
    return toolCallSummary(name, parsed);
  }

  return preview ? `${label}  ${preview}` : label;
};

/** `{"a": 1}` or `{'a': 1}` (a Python repr; strings only) back to an object, else null. */
export const parseArgsText = (text: string): Record<string, unknown> | null => {
  const body = text.trim();

  if (!body.startsWith("{") || body.length > 4096) {
    return null;
  }

  const candidates = [body];

  if (body.includes("'")) {
    // A Python repr quotes with '…'. Swap quote styles when the text holds no
    // double quotes at all; a repr with both is left alone.
    if (!body.includes('"')) {
      candidates.push(
        body
          .replace(/'/g, '"')
          .replace(/\bTrue\b/g, "true")
          .replace(/\bFalse\b/g, "false")
          .replace(/\bNone\b/g, "null"),
      );
    }
  }

  for (const candidate of candidates) {
    try {
      const value: unknown = JSON.parse(candidate);

      if (value && typeof value === "object" && !Array.isArray(value)) {
        return value as Record<string, unknown>;
      }
    } catch {
      // not JSON; try the next shape
    }
  }

  // A repr cut mid-way (an 80-char resume preview): keep the first key and
  // as much of its string value as survived.
  const head = body.match(/^\{\s*(['"])(\w+)\1\s*:\s*(['"])((?:(?!\3).)*)/);

  return head ? { [head[2]!]: head[4]! } : null;
};

/** One row's worth of a bridged call: the gateway already phrased it, preview included. */
export const formatToolLabel = (label: ToolLabel) =>
  `${label.emoji} ${compactPreview(label.preview ? `${label.text}  ${label.preview}` : label.text, 72)}`;

export const formatToolLabels = (labels: readonly ToolLabel[]) => {
  const [first, ...rest] = labels;

  if (first === undefined) {
    return "";
  }

  return rest.length > 0
    ? `${formatToolLabel(first)} +${rest.length}`
    : formatToolLabel(first);
};

/** A finished trail row for an already-phrased call. */
export const toolTrailLine = (
  call: string,
  error?: boolean,
  note?: string,
  duration?: number,
) => {
  const detail = compactPreview(note ?? "", 72);
  const took = duration !== undefined ? ` (${duration.toFixed(1)}s)` : "";

  return `${call}${took}${detail ? ` :: ${detail}` : ""} ${error ? "✗" : "✓"}`;
};

export const buildToolTrailLine = (
  name: string,
  context: string,
  error?: boolean,
  note?: string,
  duration?: number,
) => toolTrailLine(formatToolCall(name, context), error, note, duration);

const verboseToolBlock = (label: string, text?: string) => {
  const body = (text ?? "").trim();

  // Persisted trail blocks are kept all session and rendered expanded by
  // default — cap to a small readable preview (NOT the 16KB live-render
  // budget) so a large tool output can't balloon the Ink render tree and
  // silently OOM-kill the TUI. See VERBOSE_TRAIL_MAX_CHARS (#34095).
  return body
    ? `${label}:\n${boundedLiveRenderText(body, {
        maxChars: VERBOSE_TRAIL_MAX_CHARS,
        maxLines: VERBOSE_TRAIL_MAX_LINES,
      })}`
    : "";
};

/** The Args and Result blocks for an already-phrased call. */
export const verboseToolTrailLine = (
  call: string,
  error?: boolean,
  duration?: number,
  argsText?: string,
  resultText?: string,
) => {
  const detail = [
    verboseToolBlock("Args", argsText),
    verboseToolBlock(error ? "Error" : "Result", resultText),
  ]
    .filter(Boolean)
    .join("\n");

  const took = duration !== undefined ? ` (${duration.toFixed(1)}s)` : "";

  return `${call}${took}${detail ? ` :: ${detail}` : ""} ${error ? "✗" : "✓"}`;
};

export const buildVerboseToolTrailLine = (
  name: string,
  context: string,
  error?: boolean,
  duration?: number,
  argsText?: string,
  resultText?: string,
) =>
  verboseToolTrailLine(
    formatToolCall(name, context),
    error,
    duration,
    argsText,
    resultText,
  );

export const isToolTrailResultLine = (line: string) =>
  line.endsWith(" ✓") || line.endsWith(" ✗");

export const parseToolTrailResultLine = (line: string) => {
  if (!isToolTrailResultLine(line)) {
    return null;
  }

  const mark = line.endsWith(" ✗") ? "✗" : "✓";
  const body = line.slice(0, -2);
  const sep = body.indexOf(" :: ");

  if (sep >= 0) {
    return { call: body.slice(0, sep), detail: body.slice(sep + 4), mark };
  }

  // `Label: note` is the older row shape. A phrased summary (`Bash  git
  // commit -m "x: y"`) or a quoted preview can carry ": " inside its
  // argument, so the split only applies when the head is a bare label.
  const legacy = body.indexOf(": ");
  const head = legacy > 0 ? body.slice(0, legacy) : "";

  if (legacy > 0 && !head.includes("  ") && !head.includes("(")) {
    return {
      call: head,
      detail: body.slice(legacy + 2),
      mark,
    };
  }

  return { call: body, detail: "", mark };
};

export const splitToolDuration = (call: string) => {
  const match = call.match(/^(.*?)( \(\d+(?:\.\d)?s\))$/);

  return match
    ? { label: match[1]!, duration: match[2]! }
    : { label: call, duration: "" };
};

export const isTransientTrailLine = (line: string) =>
  line.startsWith("drafting ") || line === "analyzing tool output…";

export const sameToolTrailGroup = (label: string, entry: string) =>
  entry === `${label} ✓` ||
  entry === `${label} ✗` ||
  entry.startsWith(`${label}(`) ||
  entry.startsWith(`${label}  `) ||
  entry.startsWith(`${label} ::`) ||
  entry.startsWith(`${label}:`);

export const lastCotTrailIndex = (trail: readonly string[]) => {
  for (let i = trail.length - 1; i >= 0; i--) {
    if (!isToolTrailResultLine(trail[i]!)) {
      return i;
    }
  }

  return -1;
};

export const estimateRows = (text: string, w: number, compact = false) => {
  let fence: { char: "`" | "~"; len: number } | null = null;
  let rows = 0;

  for (const raw of text.split("\n")) {
    const line = stripAnsi(raw);
    const maybeFence = line.match(/^\s*(`{3,}|~{3,})(.*)$/);

    if (maybeFence) {
      const marker = maybeFence[1]!;
      const lang = maybeFence[2]!.trim();

      if (!fence) {
        fence = { char: marker[0] as "`" | "~", len: marker.length };

        if (lang) {
          rows += Math.ceil((`─ ${lang}`.length || 1) / w);
        }
      } else if (marker[0] === fence.char && marker.length >= fence.len) {
        fence = null;
      }

      continue;
    }

    const inCode = Boolean(fence);
    const trimmed = line.trim();

    if (!inCode && trimmed.startsWith("|") && /^[|\s:-]+$/.test(trimmed)) {
      continue;
    }

    const rendered = inCode ? line : renderEstimateLine(line);

    if (compact && !rendered.trim()) {
      continue;
    }

    rows += Math.ceil((rendered.length || 1) / w);
  }

  return Math.max(1, rows);
};

/**
 * Render an unanswered clarify prompt (timed out, or cancelled with Esc/Ctrl+C)
 * as a persistent transcript block.  The live `ClarifyPrompt` overlay is torn
 * down the moment the turn settles, so without this the question + options
 * vanish from the screen while the agent's follow-up still refers to "the
 * options above".  Mirrors the option formatting in ClarifyPrompt (the same
 * 1-based numbered list) so the persisted record reads identically to what was
 * on screen.  `reason` states why the prompt ended ("timed out", "cancelled").
 */
export const formatAbandonedClarify = (
  question: string,
  choices: string[] | null,
  reason: string,
) => {
  const head = `ask ${question.trim()}`;
  const opts = (choices ?? []).map((c, i) => `  ${i + 1}. ${c}`);

  return [head, ...opts, `  (${reason} — no selection)`].join("\n");
};

/**
 * Batch counterpart of `formatAbandonedClarify`: every question on its own
 * line, answered ones keeping their locked answer (partials survive a
 * timeout server-side, so the record must show what was actually sent).
 */
export const formatAbandonedClarifyBatch = (
  questions: { qid: string; question: string }[],
  answers: Record<string, string>,
  reason: string,
) => {
  const lines = questions.map((q) => {
    const answer = answers[q.qid];

    return answer
      ? `  ✓ ${q.question} → ${answer}`
      : `  · ${q.question} (no answer)`;
  });

  return [
    `ask (${questions.length} questions)`,
    ...lines,
    `  (${reason})`,
  ].join("\n");
};

/**
 * Cursor/draft restore for re-visiting an answered batch clarify question
 * (Tab/Shift-Tab): a choice answer puts the cursor back on its row; an
 * answer that matches no choice was typed via Other, so the cursor lands on
 * the Other row (index = choices.length) with the text staged for editing.
 * Unanswered questions restore to a clean cursor.
 */
export const clarifyBatchRevisitState = (
  choices: readonly string[],
  answer: string | undefined,
): { custom: string; sel: number } => {
  if (answer === undefined || answer === "") {
    return { custom: "", sel: 0 };
  }

  const choiceIndex = choices.indexOf(answer);

  if (choiceIndex >= 0) {
    return { custom: "", sel: choiceIndex };
  }

  return { custom: answer, sel: choices.length > 0 ? choices.length : 0 };
};

export const flat = (r: Record<string, string[]>) => Object.values(r).flat();

export const pick = <T>(a: T[]) => a[Math.floor(Math.random() * a.length)]!;

export const isPasteBackedText = (text: string) =>
  /\[\[paste:\d+(?:[^\n]*?)\]\]|\[paste #\d+ (?:attached|excerpt)(?:[^\n]*?)\]/.test(
    text,
  );
