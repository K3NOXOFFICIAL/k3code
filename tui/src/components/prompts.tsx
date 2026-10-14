import { Box, Text, useInput, wrapAnsi } from "@k3code/ink";
import { useEffect, useRef, useState } from "react";

import { isMac } from "../lib/platform.js";
import { clarifyBatchRevisitState } from "../lib/text.js";
import type { Theme } from "../theme.js";
import type { ApprovalReq, ClarifyReq, ConfirmReq } from "../types.js";

import { chipRowProps } from "./overlayPrimitives.js";
import { TextInput } from "./textInput.js";

const APPROVAL_OPTS = ["once", "session", "always", "deny"] as const;
// tirith warning present → backend downgrades "always" to session scope, so drop it.
const APPROVAL_OPTS_NO_ALWAYS = APPROVAL_OPTS.filter((o) => o !== "always");
const APPROVAL_OPTS_SMART_DENY = ["once", "deny"] as const;
const LABELS = {
  always: "Always allow",
  deny: "Deny",
  once: "Allow once",
  session: "Allow this session",
} as const;
const CMD_PREVIEW_LINES = 10;
/** Rows of an edit/write diff shown at once; `[` and `]` page through the rest. */
export const DIFF_VIEW_LINES = 16;

type ApprovalChoice = "always" | "deny" | "once" | "session";

/**
 * The rows of an approval's change preview (a unified diff) to show from
 * `offset`: clamped so the window never runs past the end. Pure, for tests.
 */
export function previewWindow(
  preview: string,
  offset: number,
  height: number = DIFF_VIEW_LINES,
): { lines: string[]; offset: number; total: number } {
  const all = preview.split("\n");
  const start = Math.max(0, Math.min(offset, all.length - height));

  return {
    lines: all.slice(start, start + height),
    offset: start,
    total: all.length,
  };
}

/** How a diff row is drawn: added, removed, hunk header or context. */
export function diffLineKind(
  line: string,
): "add" | "context" | "del" | "file" | "hunk" {
  if (line.startsWith("+++ ") || line.startsWith("--- ")) {
    return "file";
  }

  if (line.startsWith("@@")) {
    return "hunk";
  }

  if (line.startsWith("+")) {
    return "add";
  }

  return line.startsWith("-") ? "del" : "context";
}

export function approvalOptions(req: ApprovalReq): readonly ApprovalChoice[] {
  if (req.choices) {
    return req.choices.filter((choice): choice is ApprovalChoice =>
      APPROVAL_OPTS.includes(choice as ApprovalChoice),
    );
  }

  if (req.smartDenied) {
    return APPROVAL_OPTS_SMART_DENY;
  }

  return req.allowPermanent === false ? APPROVAL_OPTS_NO_ALWAYS : APPROVAL_OPTS;
}

type ApprovalKey = {
  ctrl?: boolean;
  downArrow?: boolean;
  escape?: boolean;
  isPasted?: boolean;
  meta?: boolean;
  return?: boolean;
  upArrow?: boolean;
};

type ApprovalAction =
  | { kind: "arm" }
  | { kind: "choose"; choice: ApprovalChoice }
  | { kind: "disarm" }
  | { kind: "move"; delta: -1 | 1 }
  | { kind: "noop" }
  | { kind: "scroll"; delta: -1 | 1 };

// Any character outside the C0/C1 control ranges.
// eslint-disable-next-line no-control-regex
const TEXT_RE = /[^\x00-\x1f\x7f-\x9f]/;

/**
 * Pure key-dispatch for the approval prompt — exported so the regression
 * matrix (Esc, Ctrl+C-equivalent, number keys, Enter, ↑↓) is testable
 * without mounting React + Ink + a fake stdin.  The component just maps the
 * action onto its own state setters.
 *
 * Esc and number keys both terminate the prompt; Esc maps to deny (parity
 * with the global Ctrl+C handler that already calls cancelOverlayFromCtrlC
 * for approvals).  One typed digit 1..opts.length picks the labelled choice;
 * any other digit is text.
 * Enter confirms the current selection.  ↑/↓ moves the selection within
 * bounds.  `[` / `]` page the change preview of an edit/write when it is
 * `scrollable` (PgUp/PgDn already scroll the transcript while a prompt is
 * open); without one they are typed text.
 *
 * The prompt takes every key while it is open, so text typed for the
 * composer lands here.  Any other text, a paste or a typing burst ("3 tests
 * failed" arrives as one string) disarms the prompt: until ↑/↓ re-arm it,
 * Enter and the allow digits do nothing and only deny answers.  A scroll
 * neither arms nor disarms.
 */
export function approvalAction(
  ch: string,
  key: ApprovalKey,
  sel: number,
  opts: readonly ApprovalChoice[] = APPROVAL_OPTS,
  disarmed = false,
  scrollable = false,
): ApprovalAction {
  if (key.escape) {
    return { kind: "choose", choice: "deny" };
  }

  if (scrollable && !key.isPasted && (ch === "[" || ch === "]")) {
    return { kind: "scroll", delta: ch === "[" ? -1 : 1 };
  }

  const n = !key.isPasted && /^\d$/.test(ch) ? Number(ch) : NaN;

  if (n >= 1 && n <= opts.length) {
    const choice = opts[n - 1]!;

    return disarmed && choice !== "deny"
      ? { kind: "noop" }
      : { kind: "choose", choice };
  }

  if (key.return) {
    return disarmed ? { kind: "noop" } : { kind: "choose", choice: opts[sel]! };
  }

  if (key.upArrow || key.downArrow) {
    const delta = key.upArrow ? -1 : 1;
    const next = sel + delta;

    if (next >= 0 && next < opts.length) {
      return { kind: "move", delta };
    }

    return disarmed ? { kind: "arm" } : { kind: "noop" };
  }

  // A digit outside 1..opts.length is no quick pick: it is text and disarms.
  if (key.isPasted || (!key.ctrl && !key.meta && TEXT_RE.test(ch))) {
    return { kind: "disarm" };
  }

  return { kind: "noop" };
}

export function ApprovalPrompt({
  cols = 80,
  onChoice,
  req,
  t,
}: ApprovalPromptProps) {
  const [view, setView] = useState({ disarmed: false, sel: 0 });
  // Ink handles all keys of one stdin read in one batch, before a re-render,
  // so the handler reads and writes this ref: "x" then Enter in one read must
  // see the prompt already disarmed, ↓ then Enter the moved selection.
  const live = useRef(view);
  const { disarmed, sel } = view;
  const [diffTop, setDiffTop] = useState(0);
  const opts = approvalOptions(req);
  const diff = req.preview
    ? previewWindow(req.preview, diffTop, DIFF_VIEW_LINES)
    : null;

  useInput((ch, key, event) => {
    const cur = live.current;

    const action = approvalAction(
      ch,
      { ...key, isPasted: Boolean(event.keypress.isPasted) },
      cur.sel,
      opts,
      cur.disarmed,
      // the condition that shows the `[ / ] scroll` footer below
      Boolean(diff && diff.total > DIFF_VIEW_LINES),
    );

    if (action.kind === "choose") {
      onChoice(action.choice);

      return;
    }

    if (action.kind === "scroll") {
      if (diff) {
        // a page minus one row of overlap; previewWindow clamps the end
        const step = Math.max(1, DIFF_VIEW_LINES - 1) * action.delta;

        setDiffTop(previewWindow(req.preview!, diff.offset + step).offset);
      }

      return;
    }

    if (action.kind === "noop") {
      return;
    }

    live.current =
      action.kind === "move"
        ? { disarmed: false, sel: cur.sel + action.delta }
        : { ...cur, disarmed: action.kind === "disarm" };
    setView(live.current);
  });

  // Wrap long single-line commands to the panel width instead of clipping the
  // tail (mirrors the CLI approval panel fix — the full command must be
  // reviewable before approving). Border + paddingX + inner padding ≈ 8 cols.
  const innerWidth = Math.max(20, cols - 8);

  const rawLines = req.command
    .split("\n")
    .flatMap((line) =>
      wrapAnsi(line, innerWidth, { hard: true, trim: false }).split("\n"),
    );

  const shown = rawLines.slice(0, CMD_PREVIEW_LINES);
  const overflow = rawLines.length - shown.length;

  return (
    <Box
      borderColor={t.color.warn}
      borderStyle="double"
      flexDirection="column"
      paddingX={1}
    >
      <Text bold color={t.color.warn}>
        ⚠ approval required · {req.description}
      </Text>

      <Box flexDirection="column" paddingLeft={1}>
        {shown.map((line, i) => (
          <Text color={t.color.text} key={i} wrap="truncate-end">
            {line || " "}
          </Text>
        ))}

        {overflow > 0 ? (
          <Text color={t.color.muted}>
            … +{overflow} more line{overflow === 1 ? "" : "s"} (full text above)
          </Text>
        ) : null}
      </Box>

      {diff ? (
        <Box
          borderColor={t.color.muted}
          borderStyle="single"
          flexDirection="column"
          marginTop={1}
          paddingX={1}
        >
          {diff.lines.map((line, i) => {
            const kind = diffLineKind(line);

            return (
              <Text
                backgroundColor={
                  kind === "add"
                    ? t.color.diffAdded
                    : kind === "del"
                      ? t.color.diffRemoved
                      : undefined
                }
                bold={kind === "file"}
                color={
                  kind === "add"
                    ? t.color.diffAddedWord
                    : kind === "del"
                      ? t.color.diffRemovedWord
                      : kind === "hunk"
                        ? t.color.muted
                        : t.color.text
                }
                dimColor={kind === "context"}
                key={diff.offset + i}
                wrap="truncate-end"
              >
                {line || " "}
              </Text>
            );
          })}

          {diff.total > DIFF_VIEW_LINES ? (
            <Text color={t.color.muted}>
              lines {diff.offset + 1}–{diff.offset + diff.lines.length} of{" "}
              {diff.total} · [ / ] scroll
            </Text>
          ) : null}
        </Box>
      ) : null}

      <Text />

      {opts.map((o, i) => (
        <Text key={o}>
          <Text color={t.color.muted} {...chipRowProps(t, sel === i)}>
            {sel === i ? "▸ " : "  "}
            {i + 1}. {req.labels?.[o] ?? LABELS[o]}
          </Text>
        </Text>
      ))}

      {disarmed ? (
        <Text color={t.color.warn}>
          typed text ignored · ↑/↓ to choose again · Esc/Ctrl+C deny
        </Text>
      ) : (
        <Text color={t.color.muted}>
          ↑/↓ select · Enter confirm · 1-{opts.length} quick pick · Esc/Ctrl+C
          deny
        </Text>
      )}
    </Box>
  );
}

export function ClarifyPrompt({
  cols = 80,
  onAnswer,
  onCancel,
  onQuestionAnswer,
  req,
  t,
}: ClarifyPromptProps) {
  const [sel, setSel] = useState(0);
  const [custom, setCustom] = useState("");
  const [typing, setTyping] = useState(false);
  const choices = req.choices ?? [];
  const batch = req.questions ?? [];
  const isBatch = batch.length > 0;

  // ── Batch (A-compact) state: status list + one expanded active question.
  // `active` walks the QUESTION list (Tab/Shift-Tab cycle it, any order);
  // `sel` is reused as the cursor within the active question's choice rows.
  const answers = req.answers ?? {};
  const firstUnanswered = batch.findIndex((q) => answers[q.qid] === undefined);
  const [active, setActive] = useState(Math.max(0, firstUnanswered));

  const moveActive = (delta: number) => {
    const next = (active + delta + batch.length) % batch.length;
    const question = batch[next];
    // Re-visit restore, same model as the CLI panel: a choice answer puts
    // the cursor back on its row; a typed answer lands on Other with the
    // text staged so Enter edits it instead of retyping.
    const restored = clarifyBatchRevisitState(
      question?.choices ?? [],
      question ? answers[question.qid] : undefined,
    );

    setActive(next);
    setSel(restored.sel);
    setCustom(restored.custom);
    setTyping(false);
  };

  // After a lock the overlay is re-patched with the new answers map — jump
  // the cursor to the next unanswered question (stay put when editing).
  useEffect(() => {
    if (!isBatch) {
      return;
    }

    const current = batch[active];

    if (current && answers[current.qid] === undefined) {
      return;
    }

    const next = batch.findIndex((q) => answers[q.qid] === undefined);

    if (next >= 0) {
      setActive(next);
      setSel(0);
      setCustom("");
      setTyping(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on req.answers only: cursor moves must not pull it back
  }, [req.answers]);

  const heading = (
    <Text bold>
      <Text color={t.color.accent}>ask</Text>
      <Text color={t.color.text}>
        {" "}
        {isBatch ? `${batch.length} questions` : req.question}
      </Text>
    </Text>
  );

  const activeQuestion = isBatch ? batch[active] : undefined;
  const activeChoices = activeQuestion
    ? (activeQuestion.choices ?? [])
    : choices;
  const answeredCount = isBatch
    ? batch.filter((q) => answers[q.qid] !== undefined).length
    : 0;
  const remainingCount = isBatch ? batch.length - answeredCount : 0;

  const lockActive = (value: string) => {
    if (activeQuestion) {
      onQuestionAnswer?.(activeQuestion.qid, value);
      setSel(0);
      setCustom("");
      setTyping(false);
    }
  };

  useInput((ch, key) => {
    if (key.escape) {
      if (typing) {
        setTyping(false);

        return;
      }

      onCancel();

      return;
    }

    if (typing) {
      return;
    }

    if (isBatch) {
      // Tab / Shift-Tab cycle the active question (with wrap) — the
      // selected question is always the expanded one, like the CLI panel.
      if (key.tab) {
        moveActive(key.shift ? -1 : 1);

        return;
      }

      if (!activeQuestion) {
        return;
      }

      if (activeChoices.length === 0) {
        // Open-ended question: any keypress starts typing (TextInput below).
        setTyping(true);

        return;
      }

      if (key.upArrow && sel > 0) {
        setSel((s) => s - 1);
      }

      if (key.downArrow && sel < activeChoices.length) {
        setSel((s) => s + 1);
      }

      if (key.return) {
        if (sel === activeChoices.length) {
          setTyping(true);
        } else if (activeChoices[sel]) {
          lockActive(activeChoices[sel]!);
        }

        return;
      }

      const n = parseInt(ch);

      if (n >= 1 && n <= activeChoices.length) {
        lockActive(activeChoices[n - 1]!);
      }

      return;
    }

    if (!choices.length) {
      return;
    }

    if (key.upArrow && sel > 0) {
      setSel((s) => s - 1);
    }

    if (key.downArrow && sel < choices.length) {
      setSel((s) => s + 1);
    }

    if (key.return) {
      if (sel === choices.length) {
        setTyping(true);
      } else if (choices[sel]) {
        onAnswer(choices[sel]!);
      }
    }

    const n = parseInt(ch);

    if (n >= 1 && n <= choices.length) {
      onAnswer(choices[n - 1]!);
    }
  });

  if (isBatch) {
    const hint = typing
      ? `Enter ${remainingCount === 1 ? "confirm and continue" : "lock answer"} · Esc back`
      : `↑/↓ select · Enter ${remainingCount === 1 ? "confirm and continue" : "lock answer"} · Tab/Shift+Tab switch question · Esc/Ctrl+C cancel`;

    return (
      <Box flexDirection="column">
        {heading}

        {batch.map((q, i) => {
          const answer = answers[q.qid];
          const isActive = i === active;
          const marker = answer !== undefined ? "✓" : isActive ? "▸" : "·";

          return (
            <Box flexDirection="column" key={q.qid}>
              <Text>
                <Text
                  bold={isActive}
                  color={isActive ? t.color.text : t.color.muted}
                >
                  {marker} {q.question}
                </Text>
              </Text>

              {answer !== undefined ? (
                // The locked answer on its own line, in the ok color, so the
                // current answers stay readable while Tab walks the list.
                <Box paddingLeft={2}>
                  <Text
                    color={answer ? t.color.ok : t.color.muted}
                    italic={!answer}
                  >
                    {answer || "(skipped)"}
                  </Text>
                </Box>
              ) : null}

              {isActive ? (
                typing || activeChoices.length === 0 ? (
                  <Box paddingLeft={2}>
                    <Text color={t.color.label}>{"> "}</Text>
                    <TextInput
                      color={t.color.text}
                      columns={Math.max(20, cols - 8)}
                      onChange={setCustom}
                      onSubmit={lockActive}
                      value={custom}
                    />
                  </Box>
                ) : (
                  <Box flexDirection="column" paddingLeft={2}>
                    {[...activeChoices, "Other (type your answer)"].map(
                      (c, ci) => (
                        <Text key={ci}>
                          <Text
                            color={t.color.muted}
                            {...chipRowProps(t, sel === ci)}
                          >
                            {sel === ci ? "▸ " : "  "}
                            {ci + 1}. {c}
                          </Text>
                        </Text>
                      ),
                    )}
                  </Box>
                )
              ) : null}
            </Box>
          );
        })}

        <Text color={t.color.muted}>
          {answeredCount}/{batch.length} answered · {hint}
        </Text>
      </Box>
    );
  }

  if (typing || !choices.length) {
    return (
      <Box flexDirection="column">
        {heading}

        <Box>
          <Text color={t.color.label}>{"> "}</Text>
          <TextInput
            color={t.color.text}
            columns={Math.max(20, cols - 6)}
            onChange={setCustom}
            onSubmit={onAnswer}
            value={custom}
          />
        </Box>

        <Text color={t.color.muted}>
          Enter send · Esc {choices.length ? "back" : "cancel"} ·{" "}
          {isMac ? "Cmd+C copy · Cmd+V paste · Ctrl+C cancel" : "Ctrl+C cancel"}
        </Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      {heading}

      {[...choices, "Other (type your answer)"].map((c, i) => (
        <Text key={i}>
          <Text color={t.color.muted} {...chipRowProps(t, sel === i)}>
            {sel === i ? "▸ " : "  "}
            {i + 1}. {c}
          </Text>
        </Text>
      ))}

      <Text color={t.color.muted}>
        ↑/↓ select · Enter confirm · 1-{choices.length} quick pick · Esc/Ctrl+C
        cancel
      </Text>
    </Box>
  );
}

export function ConfirmPrompt({
  onCancel,
  onConfirm,
  req,
  t,
}: ConfirmPromptProps) {
  const [sel, setSel] = useState(0);

  useInput((ch, key) => {
    const lower = ch.toLowerCase();

    if (key.escape || (key.ctrl && lower === "c") || lower === "n") {
      return onCancel();
    }

    if (lower === "y") {
      return onConfirm();
    }

    if (key.upArrow) {
      setSel(0);
    }

    if (key.downArrow) {
      setSel(1);
    }

    if (key.return) {
      if (sel === 0) {
        onCancel();
      } else {
        onConfirm();
      }
    }
  });

  const accent = req.danger ? t.color.error : t.color.warn;

  const rows = [
    { color: t.color.text, label: req.cancelLabel ?? "No" },
    {
      color: req.danger ? t.color.error : t.color.text,
      label: req.confirmLabel ?? "Yes",
    },
  ];

  return (
    <Box
      borderColor={accent}
      borderStyle="double"
      flexDirection="column"
      paddingX={1}
    >
      <Text bold color={accent}>
        {req.danger ? "⚠" : "?"} {req.title}
      </Text>

      {req.detail ? (
        <Box paddingLeft={1}>
          <Text color={t.color.text} wrap="truncate-end">
            {req.detail}
          </Text>
        </Box>
      ) : null}

      <Text />

      {rows.map((row, i) => (
        <Text key={row.label}>
          <Text color={sel === i ? accent : t.color.muted}>
            {sel === i ? "▸ " : "  "}
          </Text>
          <Text color={sel === i ? row.color : t.color.muted}>{row.label}</Text>
        </Text>
      ))}

      <Text color={t.color.muted}>
        ↑/↓ select · Enter confirm · Y/N quick · Esc cancel
      </Text>
    </Box>
  );
}

interface ApprovalPromptProps {
  cols?: number;
  onChoice: (s: string) => void;
  req: ApprovalReq;
  t: Theme;
}

interface ClarifyPromptProps {
  cols?: number;
  onAnswer: (s: string) => void;
  onCancel: () => void;
  /** Batch mode: lock one question's answer (clarify.respond + question_id). */
  onQuestionAnswer?: (qid: string, s: string) => void;
  req: ClarifyReq;
  t: Theme;
}

interface ConfirmPromptProps {
  onCancel: () => void;
  onConfirm: () => void;
  req: ConfirmReq;
  t: Theme;
}
