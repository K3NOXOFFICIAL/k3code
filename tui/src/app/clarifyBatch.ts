import {
  buildToolTrailLine,
  clarifyAnswerDisplay,
  formatAbandonedClarifyBatch,
  sameToolTrailGroup,
  toolTrailLabel,
} from "../lib/text.js";
import type { ClarifyBatchResult, ClarifyReq, Msg } from "../types.js";

import { patchOverlayState } from "./overlayStore.js";
import { respondToServerRequest } from "./serverRequestStore.js";
import { turnController } from "./turnController.js";
import { patchTurnState } from "./turnStore.js";
import { patchUiState } from "./uiStore.js";

/** Model tools that open the clarify overlay: the legacy `clarify` and the batch `ask_user_question`. */
export const CLARIFY_TOOL_NAMES: readonly string[] = [
  "clarify",
  "ask_user_question",
];

/** Drop the live trail line of whichever clarify tool asked: the answer is persisted in its place. */
export const dropClarifyToolTrail = () => {
  const labels = CLARIFY_TOOL_NAMES.map(toolTrailLabel);

  turnController.turnTools = turnController.turnTools.filter(
    (line) => !labels.some((label) => sameToolTrailGroup(label, line)),
  );
  patchTurnState({ turnTrail: turnController.turnTools });
};

/**
 * Answer a batch clarify (`questions[]`) with ONE response frame: `{answers}`
 * on Submit, `{answers, chat: true}` for "Chat about this", `{answer: ""}` for
 * cancel (`result` null). Persists the Q&A (or the abandoned record) to the
 * transcript and clears the overlay.
 */
export const answerClarifyBatchRequest = (
  clarify: ClarifyReq,
  result: ClarifyBatchResult | null,
  appendMessage: (msg: Msg) => void,
) => {
  const questions = clarify.questions ?? [];

  dropClarifyToolTrail();

  const frame =
    result === null
      ? { answer: "" }
      : "chat" in result
        ? { answers: result.answers, chat: true }
        : { answers: result.answers };

  if (!respondToServerRequest(clarify.requestId, frame)) {
    // The request already expired (request.cancel raced the keystroke): nothing to answer.
    patchOverlayState({ clarify: null });

    return;
  }

  if (result === null) {
    // Esc / Ctrl+C cancel: persist the questions as a system line so they
    // survive on screen, matching the timeout path.
    appendMessage({
      role: "system",
      text: formatAbandonedClarifyBatch(
        questions,
        clarify.answers ?? {},
        "cancelled",
      ),
    });
  } else {
    const label = toolTrailLabel("ask_user_question");

    turnController.persistedToolLabels.add(label);
    appendMessage({
      kind: "trail",
      role: "system",
      text: "",
      tools: [
        buildToolTrailLine(
          "ask_user_question",
          `${questions.length} question${questions.length === 1 ? "" : "s"}`,
        ),
      ],
    });
    appendMessage({
      role: "user",
      text: [
        ...questions.map((q) => {
          const display = clarifyAnswerDisplay(
            q,
            result.answers[q.qid] ?? "",
          ).trim();

          return `${q.question} → ${display || "(skipped)"}`;
        }),
        ...("chat" in result ? ["(chat about this)"] : []),
      ].join("\n"),
    });
    patchUiState({ status: "running…" });
  }

  patchOverlayState({ clarify: null });
};
