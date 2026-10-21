import {
  Box,
  stringWidth,
  Text,
  useInput,
  useStdout,
  wrapAnsi,
} from "@k3code/ink";
import { useRef, useState } from "react";

import {
  clarifyAnswerDisplay,
  clarifyBatchRevisitState,
  compactPreview,
  decodeMultiAnswer,
  encodeMultiAnswer,
  visibleControls,
} from "../lib/text.js";
import { todoGlyph } from "../lib/todo.js";
import type { Theme } from "../theme.js";
import type {
  ClarifyBatchQuestion,
  ClarifyBatchResult,
  ClarifyReq,
} from "../types.js";

import { PROMPT_CHROME_ROWS } from "./appOverlays.js";
import { chipRowProps } from "./overlayPrimitives.js";
import { TextInput } from "./textInput.js";

/** One question's draft: the recorded single-select answer, or the checked multi-select entries. */
export interface QuestionDraft {
  /** Single-select: the recorded answer (a choice label or typed text). */
  answer?: string;
  /** Multi-select: checked option labels (choices and replayed extras). */
  checked: string[];
  cursor: number;
  /** Typed text staged for "Type something" (multi-select: an extra checked entry when non-empty). */
  custom: string;
  /** Multi-select: replayed entries that match no choice, kept as their own rows. */
  extras: string[];
}

export interface PickerState {
  done: boolean;
  draft: string;
  qs: QuestionDraft[];
  submitCursor: number;
  tab: number;
  typing: boolean;
}

export type PickerRow =
  | { kind: "chat" }
  | { kind: "next" }
  | { kind: "opt"; label: string }
  | { kind: "type" };

type PickerKey = {
  ctrl?: boolean;
  downArrow?: boolean;
  escape?: boolean;
  leftArrow?: boolean;
  meta?: boolean;
  return?: boolean;
  rightArrow?: boolean;
  shift?: boolean;
  tab?: boolean;
  upArrow?: boolean;
};

/** A key's effect: the next state, plus the frame to send when the picker finishes (null = cancel). */
export interface PickerStep {
  emit?: { result: ClarifyBatchResult | null };
  state: PickerState;
}

const SUBMIT_ROWS = ["Submit answers", "Cancel"] as const;

const choicesOf = (q: ClarifyBatchQuestion) => q.choices ?? [];

/** Exactly one single-select question: answered on pick, no Submit tab. */
export const hasSubmitTab = (questions: readonly ClarifyBatchQuestion[]) =>
  !(questions.length === 1 && !questions[0]!.multiSelect);

const lastTab = (questions: readonly ClarifyBatchQuestion[]) =>
  hasSubmitTab(questions) ? questions.length : questions.length - 1;

export const pickerRows = (
  q: ClarifyBatchQuestion,
  d: QuestionDraft,
): PickerRow[] => [
  ...[...choicesOf(q), ...d.extras].map((label): PickerRow => ({
    kind: "opt",
    label,
  })),
  ...(q.multiSelect ? [{ kind: "next" } as PickerRow] : []),
  { kind: "type" },
  { kind: "chat" },
];

/** The multi-select entries on the wire: checked options in row order, then the typed entry. */
const multiItems = (q: ClarifyBatchQuestion, d: QuestionDraft) => [
  ...[...choicesOf(q), ...d.extras].filter((label) =>
    d.checked.includes(label),
  ),
  ...(d.custom.trim() ? [d.custom] : []),
];

/** Every answered question (qid -> wire string); unanswered ones are omitted. */
export const collectAnswers = (
  questions: readonly ClarifyBatchQuestion[],
  qs: readonly QuestionDraft[],
): Record<string, string> => {
  const out: Record<string, string> = {};

  questions.forEach((q, i) => {
    const d = qs[i]!;

    if (q.multiSelect) {
      const items = multiItems(q, d);

      if (items.length) {
        out[q.qid] = encodeMultiAnswer(items);
      }
    } else if (d.answer) {
      out[q.qid] = d.answer;
    }
  });

  return out;
};

/**
 * Seed every question from the replayed `answers` (qid -> wire string).
 * Decodes ONLY multi-select answers: a single-select answer typed as `["x"]`
 * stays verbatim. Opens on the first unanswered question.
 */
export const initialPickerState = (req: ClarifyReq): PickerState => {
  const questions = req.questions ?? [];

  const qs = questions.map((q): QuestionDraft => {
    const raw = req.answers?.[q.qid];
    const choices = choicesOf(q);

    if (q.multiSelect) {
      const items = raw ? decodeMultiAnswer(raw) : [];
      const unmatched = items.filter((item) => !choices.includes(item));

      return {
        checked: items.filter((item) => item !== unmatched[0]),
        cursor: 0,
        custom: unmatched[0] ?? "",
        extras: unmatched.slice(1),
      };
    }

    const revisit = clarifyBatchRevisitState(choices, raw);

    return {
      answer: raw || undefined,
      checked: [],
      cursor: revisit.sel,
      custom: revisit.custom,
      extras: [],
    };
  });

  const answered = collectAnswers(questions, qs);
  const firstOpen = questions.findIndex((q) => !(q.qid in answered));

  return {
    done: false,
    draft: "",
    qs,
    submitCursor: 0,
    tab: firstOpen >= 0 ? firstOpen : lastTab(questions),
    typing: false,
  };
};

const patchQ = (
  s: PickerState,
  i: number,
  patch: Partial<QuestionDraft>,
): PickerState => ({
  ...s,
  qs: s.qs.map((d, j) => (j === i ? { ...d, ...patch } : d)),
});

const finish = (
  s: PickerState,
  result: ClarifyBatchResult | null,
): PickerStep => ({ emit: { result }, state: { ...s, done: true } });

const moveTab = (
  s: PickerState,
  questions: readonly ClarifyBatchQuestion[],
  delta: number,
): PickerState => ({
  ...s,
  submitCursor: 0,
  tab: Math.max(0, Math.min(lastTab(questions), s.tab + delta)),
  typing: false,
});

/** Advance after a single-select pick (or Next); the lone single-select question sends at once. */
const advance = (
  s: PickerState,
  questions: readonly ClarifyBatchQuestion[],
): PickerStep =>
  hasSubmitTab(questions)
    ? { state: moveTab(s, questions, 1) }
    : finish(s, { answers: collectAnswers(questions, s.qs) });

const pickSingle = (
  s: PickerState,
  questions: readonly ClarifyBatchQuestion[],
  label: string,
  cursor: number,
): PickerStep =>
  advance(patchQ(s, s.tab, { answer: label, cursor }), questions);

const toggle = (s: PickerState, label: string, cursor: number) => {
  const d = s.qs[s.tab]!;

  return patchQ(s, s.tab, {
    checked: d.checked.includes(label)
      ? d.checked.filter((c) => c !== label)
      : [...d.checked, label],
    cursor,
  });
};

/**
 * Commit the "Type something" text (Enter in the field, or Tab out of it).
 * Single-select: non-empty text is the answer (Enter also advances); clearing
 * a typed answer drops it. Multi-select: the text is the extra checked entry.
 */
export const commitTyped = (
  s: PickerState,
  questions: readonly ClarifyBatchQuestion[],
  value: string,
  advanceAfter: boolean,
): PickerStep => {
  const q = questions[s.tab];

  if (!q) {
    return { state: { ...s, typing: false } };
  }

  const d = s.qs[s.tab]!;
  let next: PickerState = { ...s, draft: "", typing: false };

  if (q.multiSelect) {
    return { state: patchQ(next, s.tab, { custom: value }) };
  }

  const typedAnswer = d.answer !== undefined && d.answer === d.custom;

  if (!value.trim()) {
    return {
      state: patchQ(next, s.tab, {
        answer: typedAnswer ? undefined : d.answer,
        custom: "",
      }),
    };
  }

  next = patchQ(next, s.tab, { answer: value, custom: value });

  return advanceAfter ? advance(next, questions) : { state: next };
};

/**
 * Pure key dispatch for everything but the text field's own editing keys
 * (exported for tests). Ctrl/Meta chords are left to the app (Ctrl+C cancels
 * the batch through the global handler); Shift+↑/↓ stay transcript scroll.
 */
export const pickerKey = (
  s: PickerState,
  questions: readonly ClarifyBatchQuestion[],
  ch: string,
  key: PickerKey,
): PickerStep => {
  // A lone Esc arrives with meta set, so only Alt chords are skipped.
  if (s.done || key.ctrl || (key.meta && !key.escape)) {
    return { state: s };
  }

  const tabDelta = key.tab ? (key.shift ? -1 : 1) : 0;

  if (s.typing) {
    // The field keeps every other key; Esc only leaves it (the draft is dropped).
    if (key.escape) {
      return { state: { ...s, draft: "", typing: false } };
    }

    if (tabDelta) {
      const committed = commitTyped(s, questions, s.draft, false);

      return { state: moveTab(committed.state, questions, tabDelta) };
    }

    return { state: s };
  }

  if (key.escape) {
    return finish(s, null);
  }

  if (tabDelta || key.leftArrow || key.rightArrow) {
    return {
      state: moveTab(s, questions, tabDelta || (key.leftArrow ? -1 : 1)),
    };
  }

  const vertical = key.shift ? 0 : key.upArrow ? -1 : key.downArrow ? 1 : 0;
  const q = questions[s.tab];

  if (!q) {
    // Submit tab
    if (vertical) {
      return {
        state: {
          ...s,
          submitCursor: Math.max(
            0,
            Math.min(SUBMIT_ROWS.length - 1, s.submitCursor + vertical),
          ),
        },
      };
    }

    // Enter on the cursor row, or the row's painted number (1 Submit, 2 Cancel).
    const picked = key.return ? s.submitCursor : ["1", "2"].indexOf(ch);

    if (picked >= 0) {
      return picked === 0
        ? finish(s, { answers: collectAnswers(questions, s.qs) })
        : finish(s, null);
    }

    return { state: s };
  }

  const d = s.qs[s.tab]!;
  const rows = pickerRows(q, d);

  if (vertical) {
    return {
      state: patchQ(s, s.tab, {
        cursor: Math.max(0, Math.min(rows.length - 1, d.cursor + vertical)),
      }),
    };
  }

  const digit = /^[1-9]$/.test(ch) ? Number(ch) - 1 : -1;
  const digitRow = rows[digit];

  if (digitRow?.kind === "opt") {
    return q.multiSelect
      ? { state: toggle(s, digitRow.label, digit) }
      : pickSingle(s, questions, digitRow.label, digit);
  }

  const row = rows[Math.min(d.cursor, rows.length - 1)]!;

  if (ch === " " && q.multiSelect && row.kind === "opt") {
    return { state: toggle(s, row.label, d.cursor) };
  }

  if (!key.return) {
    return { state: s };
  }

  switch (row.kind) {
    case "opt":
      return q.multiSelect
        ? { state: toggle(s, row.label, d.cursor) }
        : pickSingle(s, questions, row.label, d.cursor);
    case "next":
      return advance(s, questions);
    case "type":
      return { state: { ...s, draft: d.custom, typing: true } };
    case "chat":
      return finish(s, {
        answers: collectAnswers(questions, s.qs),
        chat: true,
      });
  }
};

/**
 * The option rows to draw in `budget` lines (each option is its label line
 * plus its description lines): keeps the cursor visible, scrolls only as far
 * as needed from `prevTop`, and fills the budget when near the end.
 */
export function optionWindow(
  heights: readonly number[],
  budget: number,
  cursor: number,
  prevTop: number,
): { end: number; start: number } {
  const n = heights.length;

  if (!n) {
    return { end: 0, start: 0 };
  }

  const at = Math.max(0, Math.min(n - 1, cursor));
  const sum = (a: number, b: number) =>
    heights.slice(a, b).reduce((acc, h) => acc + h, 0);
  let start = Math.max(0, Math.min(prevTop, at));

  while (start < at && sum(start, at + 1) > budget) {
    start++;
  }

  let end = at + 1;

  while (end < n && sum(start, end + 1) <= budget) {
    end++;
  }

  while (start > 0 && sum(start - 1, end) <= budget) {
    start--;
  }

  return { end, start };
}

/** The longest prefix of `line` on grapheme boundaries that fits `cells` display columns. */
const cutToWidth = (line: string, cells: number) => {
  let out = "";
  let used = 0;

  for (const { segment } of new Intl.Segmenter().segment(line)) {
    used += stringWidth(segment);

    if (used > cells) {
      break;
    }

    out += segment;
  }

  return out;
};

/** `text` wrapped to `width`, at most `max` lines, the last one marked `…` when cut. */
const clampLines = (text: string, width: number, max: number): string[] => {
  const lines = wrapAnsi(text, width, { hard: true, trim: true })
    .split("\n")
    .filter((line) => line.trim());

  if (lines.length <= max) {
    return lines;
  }

  return [
    ...lines.slice(0, max - 1),
    `${cutToWidth(lines[max - 1]!, Math.max(1, width - 1))}…`,
  ];
};

/** Fixed picker lines besides the question: strip, Type something, rule, Chat, hint (+ Next for multi-select). */
const fixedLines = (q: ClarifyBatchQuestion) => (q.multiSelect ? 6 : 5);

/** The option window never shrinks below one option plus its one-line description. */
const MIN_WINDOW = 2;

/**
 * Tab labels that fit `avail` cells: the non-active labels are ellipsized
 * (longest first, never below 3 cells) so the active tab and Submit stay visible.
 */
const fitTabLabels = (
  full: readonly string[],
  active: number,
  avail: number,
  submitTab: boolean,
) => {
  const labels = full.map((label) => compactPreview(label, 16));
  const caps = labels.map((label) => stringWidth(label));
  // "← " + " →", each tab's " ☐ " + " " chrome, the "  " gaps, and "  ✓ Submit ".
  const chrome =
    4 + full.length * 4 + (full.length - 1) * 2 + (submitTab ? 12 : 0);
  const total = () => chrome + labels.reduce((n, l) => n + stringWidth(l), 0);

  while (total() > avail) {
    let widest = -1;

    caps.forEach((cap, i) => {
      if (i !== active && cap > 3 && (widest < 0 || cap > caps[widest]!)) {
        widest = i;
      }
    });

    if (widest < 0) {
      break;
    }

    caps[widest]!--;
    labels[widest] = compactPreview(full[widest]!, caps[widest]!);
  }

  return labels;
};

export function QuestionPicker({
  cols,
  onClarifyBatch,
  req,
  t,
}: QuestionPickerProps) {
  const questions = req.questions ?? [];
  const [view, setView] = useState(() => initialPickerState(req));
  // Ink handles every key of one stdin read before re-rendering, so the
  // handler reads and writes this ref (↓ ↓ Enter in one read must see the
  // moved cursor); `done` makes sure only one frame is ever sent.
  const live = useRef(view);
  const tops = useRef<number[]>([]);
  const { stdout } = useStdout();

  const apply = (step: PickerStep) => {
    live.current = step.state;
    setView(step.state);

    if (step.emit) {
      onClarifyBatch(step.emit.result);
    }
  };

  useInput((ch, key) => {
    // The field owns the keys while it is (or was, at the last paint) open:
    // its Enter commits through onSubmit, which may already have closed it.
    if (view.typing && !live.current.typing) {
      return;
    }

    apply(pickerKey(live.current, questions, ch, key));
  });

  const width = Math.max(20, cols - 8);
  const answers = collectAnswers(questions, view.qs);
  const submitTab = hasSubmitTab(questions);
  const q = questions[view.tab];
  // The rows left for the question and the option window once the app's chrome and the fixed rows are painted.
  const room =
    (stdout?.rows ?? 24) - PROMPT_CHROME_ROWS - (q ? fixedLines(q) : 0);
  const questionAll = q
    ? clampLines(visibleControls(q.question), width, Infinity)
    : [];
  // The whole question, unless it would squeeze the window below MIN_WINDOW: then it gets the rows that remain.
  const questionLines =
    questionAll.length <= room - MIN_WINDOW
      ? questionAll
      : clampLines(
          visibleControls(q!.question),
          width,
          Math.max(1, room - MIN_WINDOW),
        );
  const budget = Math.max(
    MIN_WINDOW,
    Math.min(16, room - questionLines.length),
  );
  const descMax = budget >= 12 ? 2 : 1;
  const tabLabels = fitTabLabels(
    questions.map((item) => visibleControls(item.header || item.question)),
    view.tab,
    cols - 2,
    submitTab,
  );

  const strip = (
    <Text wrap="truncate-end">
      <Text color={t.color.muted}>← </Text>
      {questions.map((item, i) => {
        const label = tabLabels[i]!;
        const active = view.tab === i;

        return (
          <Text key={item.qid}>
            {i ? "  " : ""}
            <Text
              color={item.qid in answers ? t.color.text : t.color.muted}
              {...chipRowProps(t, active)}
            >
              {` ${item.qid in answers ? "☑" : "☐"} ${label} `}
            </Text>
          </Text>
        );
      })}
      {submitTab ? (
        <Text>
          {"  "}
          <Text
            color={t.color.muted}
            {...chipRowProps(t, view.tab === questions.length)}
          >
            {" ✓ Submit "}
          </Text>
        </Text>
      ) : null}
      <Text color={t.color.muted}> →</Text>
    </Text>
  );

  const hint = (text: string) => (
    <Text color={t.color.muted} wrap="truncate-end">
      {text}
    </Text>
  );

  if (!q) {
    const open = questions.filter((item) => !(item.qid in answers)).length;

    return (
      <Box flexDirection="column">
        {strip}
        <Text bold color={t.color.text}>
          Review your answers
        </Text>
        {questions.map((item) => (
          <Text color={t.color.text} key={item.qid} wrap="truncate-end">
            {item.qid in answers ? (
              <Text color={t.color.ok}> ✓ </Text>
            ) : (
              <Text color={t.color.muted}> · </Text>
            )}
            {visibleControls(item.header || item.question)}
            {item.qid in answers ? (
              <Text color={t.color.label}>
                {" → "}
                {visibleControls(
                  clarifyAnswerDisplay(item, answers[item.qid]!),
                )}
              </Text>
            ) : (
              <Text color={t.color.muted}> (unanswered)</Text>
            )}
          </Text>
        ))}
        {open ? (
          <Text color={t.color.warn}>
            ⚠ {open} unanswered (sent without an answer)
          </Text>
        ) : null}
        {SUBMIT_ROWS.map((label, i) => (
          <Text key={label}>
            <Text
              color={t.color.muted}
              {...chipRowProps(t, view.submitCursor === i)}
            >
              {view.submitCursor === i ? "▸ " : "  "}
              {i + 1}. {label}
            </Text>
          </Text>
        ))}
        {hint(
          "↑/↓ select · Enter confirm · Tab/←/→ switch question · Esc cancel",
        )}
      </Box>
    );
  }

  const d = view.qs[view.tab]!;
  const rows = pickerRows(q, d);
  const options = rows.filter((row) => row.kind === "opt");
  const descriptions = options.map((_row, i) => {
    const desc = q.descriptions?.[i];

    return desc ? clampLines(visibleControls(desc), width - 5, descMax) : [];
  });
  const win = optionWindow(
    descriptions.map((lines) => 1 + lines.length),
    budget,
    Math.min(d.cursor, options.length - 1),
    tops.current[view.tab] ?? 0,
  );

  tops.current[view.tab] = win.start;

  const rowLine = (i: number, body: string, marker = "") => {
    const active = d.cursor === i;

    return (
      <Text key={`row-${i}`} wrap="truncate-end">
        <Text color={t.color.text} {...chipRowProps(t, active)}>
          {active ? "▸ " : "  "}
          {body}
        </Text>
        {marker ? <Text color={t.color.ok}>{marker}</Text> : null}
      </Text>
    );
  };

  const nextIdx = q.multiSelect ? options.length : -1;
  const typeIdx = options.length + (q.multiSelect ? 1 : 0);
  const typed = d.custom.trim() !== "";
  const typedAnswer = !q.multiSelect && typed && d.answer === d.custom;
  const windowed = win.start > 0 || win.end < options.length;

  return (
    <Box flexDirection="column">
      {strip}
      {questionLines.map((line, i) => (
        <Text bold color={t.color.text} key={`q-${i}`}>
          {line}
        </Text>
      ))}

      {options.slice(win.start, win.end).map((row, k) => {
        const i = win.start + k;
        const label = visibleControls((row as { label: string }).label);
        const box = q.multiSelect
          ? `${todoGlyph(d.checked.includes((row as { label: string }).label) ? "completed" : "pending")} `
          : "";

        return (
          <Box flexDirection="column" key={`opt-${i}`}>
            {rowLine(
              i,
              `${i < 9 ? `${i + 1}. ` : "   "}${box}${label}`,
              !q.multiSelect && d.answer === (row as { label: string }).label
                ? " ✓"
                : "",
            )}
            {descriptions[i]!.map((line, j) => (
              <Text color={t.color.muted} key={`desc-${i}-${j}`}>
                {"     "}
                {line}
              </Text>
            ))}
          </Box>
        );
      })}

      {nextIdx >= 0 ? rowLine(nextIdx, "Next →") : null}

      {view.typing ? (
        <Box>
          <Text color={t.color.label}>{"▸ > "}</Text>
          <TextInput
            color={t.color.text}
            columns={Math.max(20, cols - 8)}
            onChange={(value) => {
              live.current = { ...live.current, draft: value };
              setView(live.current);
            }}
            onSubmit={(value) => {
              if (live.current.typing) {
                apply(commitTyped(live.current, questions, value, true));
              }
            }}
            value={view.draft}
          />
        </Box>
      ) : (
        rowLine(
          typeIdx,
          q.multiSelect
            ? `${todoGlyph(typed ? "completed" : "pending")} ${typed ? visibleControls(d.custom) : "Type something"}`
            : typed
              ? visibleControls(d.custom)
              : "Type something",
          typedAnswer ? " ✓" : "",
        )
      )}

      <Text color={t.color.border}>{"─".repeat(width)}</Text>
      {rowLine(typeIdx + 1, "Chat about this")}

      {hint(
        view.typing
          ? "Enter save · Esc back · Tab switch question · Ctrl+C cancel"
          : [
              windowed
                ? `${win.start + 1}-${win.end} of ${options.length}`
                : "",
              "↑/↓ select",
              q.multiSelect ? "Space/Enter toggle" : "Enter confirm",
              submitTab ? "Tab switch question" : "",
              "Esc cancel",
            ]
              .filter(Boolean)
              .join(" · "),
      )}
    </Box>
  );
}

interface QuestionPickerProps {
  cols: number;
  onClarifyBatch: (result: ClarifyBatchResult | null) => void;
  req: ClarifyReq;
  t: Theme;
}
